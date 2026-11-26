"""Blind paired FAQ moderation experiment, grouped by independent source event."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as bench

MODEL = "google/gemma-4-26b-a4b-it"
PROVIDER = "Makora"
ARMS = ("raw", "registry")
LIMIT = 2
EXTRA = """
Уточнения к маршрутизации. Если письмо относится к другой ситуации, но применимый пункт сам однозначно отвечает на исходный вопрос, выбери link_rule и rule. Для полного дубликата ответа из действующего пункта также выбери link_rule и rule при получении письма. Если для решения о публикации требуется установить полномочия автора, адресата или условия, выбери clarify; отдельно определи, даёт ли регламент ответ сотруднику. Для события wiki сравни прежнюю и новую редакции и оцени эффект правки для каждого FAQ отдельно. Более свежая редакция сама по себе не отзывает опубликованное разъяснение службы качества. При расхождении сохрани активный экспертный ответ и отметь его для проверки. При полном покрытии вопроса действующим пунктом архивируй дубликат. Учитывай явный отзыв экспертного ответа.
"""


def read_log(folder):
    path = folder / "usage.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def routing_issues(row, args):
    issues = []
    for field, wanted in (("provider", args.provider), ("served_model", args.model)):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"missing {field}")
        elif value.casefold() != wanted.casefold():
            issues.append(f"{field}: {value} (requested {wanted})")
    return issues


def snapshot(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as output:
        output.write(bench.encoded(value))
    os.replace(output.name, path)


def stage(args, key, name, prompt, validator):
    folder = args.out / "calls" / name
    folder.mkdir(parents=True, exist_ok=True)
    bench.save(folder / "prompt.json", prompt)
    opts = SimpleNamespace(**{**vars(args), "out": folder})
    while True:
        rows = read_log(folder)
        # A completed call with an unverifiable route cannot be used in either arm.
        for row in rows:
            if row.get("ok"):
                issues = routing_issues(row, args)
                if issues:
                    raise ValueError(f"{name}: {', '.join(issues)}")
                result = bench.response_json(row["text"])
                validator(result)
                return result
        if len(rows) >= LIMIT:
            raise ValueError(f"{name}: {LIMIT} attempts exhausted: {rows[-1].get('error')}")
        try:
            bench.request_stage(opts, name, prompt, validator, key)
        except ValueError:
            if len(read_log(folder)) == len(rows):
                raise


def plan_events(cases):
    groups = {}
    for cid, case in cases.items():
        eid = case["event_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", eid):
            raise ValueError(f"Invalid event id {eid!r}")
        groups.setdefault(eid, {})[cid] = case
    counts = Counter()
    plan = []
    for eid, selected in groups.items():
        kind = next(iter(selected.values()))["event_kind"]
        if len(selected) != (2 if kind == "wiki" else 1) or any(c["event_kind"] != kind for c in selected.values()):
            raise ValueError(f"Unexpected event group {eid}")
        arms = ARMS if counts[kind] % 2 == 0 else ARMS[::-1]
        plan.append({"event_id": eid, "kind": kind, "case_ids": list(selected), "order": list(arms)})
        counts[kind] += 1
    if counts != {"wiki": 6, "mail": 12}:
        raise ValueError(f"Expected 6 wiki and 12 mail events, found {dict(counts)}")
    return plan


def preprocess(args, key, clauses):
    originals = list(clauses.values())
    built = stage(args, key, "cards", bench.packet(bench.CARD_PROMPT, {"clauses": originals}),
                  lambda r: bench.screen_cards(r, clauses))
    bench.save(args.out / "cards.json", built)
    screened, flags = bench.screen_cards(built, clauses)
    audit = stage(args, key, "audit", bench.packet(bench.AUDIT_PROMPT, {"clauses": originals, "cards": list(screened.values())}),
                  lambda r: bench.vetted_cards(r, screened, {}, clauses))
    bench.save(args.out / "audit.json", audit)
    cards = bench.vetted_cards(audit, screened, flags, clauses)
    return cards, {"total": len(clauses), "accepted": list(cards), "fallback_to_original": flags}


def check_ids(result, selected):
    bench.validate_decision_batch(result)
    actual = [row.get("id") if isinstance(row, dict) else None for row in result["cases"]]
    if Counter(actual) != Counter(selected.keys()):
        raise ValueError(f"Expected exactly {list(selected)}, received {actual}")


def totals(rows):
    def values(field):
        return [r["usage"].get(field) for r in rows]
    def add(field):
        data = values(field)
        return sum(data) if all(isinstance(x, (float, int)) and not isinstance(x, bool) for x in data) else None
    return {"attempts": len(rows), "successful": sum(bool(r.get("ok")) for r in rows),
            "latency_s_sum": round(sum(r["latency_s"] for r in rows), 3),
            "input_tokens": add("prompt_tokens"), "output_tokens": add("completion_tokens"),
            "provider_reported_usd": add("cost"), "known_usd_subtotal": sum(x for x in values("cost") if x is not None),
            "missing_cost_attempts": values("cost").count(None)}


def summarize(args, mapping):
    rows = []
    for name, group in mapping.items():
        for attempt, row in enumerate(read_log(args.out / "calls" / name), 1):
            rows.append({**{k: v for k, v in row.items() if k != "text"}, "group": group, "attempt": attempt,
                         "route_issues": routing_issues(row, args)})
    issues = [{"stage": r["stage"], "attempt": r["attempt"], "issues": r["route_issues"]} for r in rows if r["route_issues"]]
    groups = {g: totals([r for r in rows if r["group"] == g]) for g in ("cards", "audit", *ARMS)}
    return {"total": totals(rows), "groups": groups, "route_issues": issues, "attempts": rows}


def execute(args, key):
    data = bench.read(args.cases)
    clauses, cases = bench.source_set(data)
    plan = plan_events(cases)
    protocol = {"corpus_sha256": bench.digest(data), "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "transport_sha256": hashlib.sha256(Path(bench.__file__).read_bytes()).hexdigest(),
                "requested_model": args.model, "requested_route": {"order": [args.provider], "allow_fallbacks": False},
                "max_tokens": args.max_tokens, "temperature": 0, "attempt_limit_per_stage": LIMIT,
                "max_concurrent_events": 3, "planned_calls": 2 + 2 * len(plan),
                "decision_prompt_sha256": bench.digest(bench.DECIDE_PROMPT + EXTRA), "events": plan}
    if not (args.out / "protocol.json").exists() and any(p.name != ".lock" for p in args.out.iterdir()):
        raise ValueError("Use a fresh output directory")
    bench.save(args.out / "protocol.json", protocol)
    errors = {}
    try:
        cards, checks = preprocess(args, key, clauses)
    except ValueError as exc:
        errors["preprocessing"] = str(exc)
        cards, checks = None, {"total": len(clauses), "error": str(exc)}
    snapshot(args.out / "card-checks.json", checks)

    def decide(event):
        eid = event["event_id"]
        selected = {cid: cases[cid] for cid in event["case_ids"]}
        predictions, failures = {}, {}
        for arm in event["order"]:
            name = arm + "-" + eid
            if arm == "registry" and cards is None:
                failures[name] = "Blocked by preprocessing"
                continue
            payload = bench.decisions(selected, clauses, cards if arm == "registry" else None)
            payload["messages"][0]["content"] += EXTRA
            try:
                result = stage(args, key, name, payload, lambda r: check_ids(r, selected))
                predictions[arm] = result["cases"]
            except ValueError as exc:
                failures[name] = str(exc)
        return predictions, failures

    combined = {arm: {} for arm in ARMS}
    with ThreadPoolExecutor(max_workers=3) as workers:
        for predictions, failures in workers.map(decide, plan):
            errors.update(failures)
            for arm, records in predictions.items():
                combined[arm].update({r["id"]: r for r in records})
    for arm in ARMS:
        snapshot(args.out / (arm + ".json"), {"cases": [combined[arm][cid] for cid in cases if cid in combined[arm]]})
    mapping = {"cards": "cards", "audit": "audit"}
    mapping.update({arm + "-" + event["event_id"]: arm for event in plan for arm in ARMS})
    usage = summarize(args, mapping)
    snapshot(args.out / "usage-summary.json", usage)
    manifest = {**protocol, "received": {arm: len(results) for arm, results in combined.items()},
                "stage_errors": errors, "observed_attempts": usage["total"]["attempts"],
                "observed_routes": [{"provider": p, "model": m, "attempts": n} for (p, m), n in
                                    Counter((r.get("provider"), r.get("served_model")) for r in usage["attempts"]).items()],
                "route_issues": usage["route_issues"], "complete": not errors,
                "all_attempt_routes_verified": bool(usage["attempts"]) and not usage["route_issues"]}
    snapshot(args.out / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provider", default=PROVIDER, choices=[PROVIDER])
    parser.add_argument("--model", default=MODEL, choices=[MODEL])
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key or args.max_tokens <= 0 or args.timeout <= 0:
        parser.error("Require OPENROUTER_API_KEY and positive timeout and token limit")
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = execute(args, key)
    print(json.dumps({k: manifest[k] for k in ("received", "complete", "all_attempt_routes_verified", "stage_errors")}, ensure_ascii=False))
    return 0 if manifest["complete"] and manifest["all_attempt_routes_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
