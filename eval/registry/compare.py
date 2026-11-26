"""Paired Gemma run on the frozen BM25 and registry retrieval outputs (Python 3.9+)."""

import argparse
import csv
import fcntl
import json
import math
import os
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run_12x3 as baseline

ROUTES = ("bm25", "registry")
MODEL = "google/gemma-4-26b-a4b-it"
SOURCES = {"doc", "title", "section", "ref", "text"}


def load_contexts(path, questions):
    cases = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        qid = c["id"]
        if (qid in cases or qid not in questions or questions[qid] != c["question"]
                or set(c) != {"id", "question", "sources"}):
            raise ValueError(f"{path}: duplicate, unexpected or mismatched question {qid}")
        sources = c["sources"]
        if not isinstance(sources, list) or len(sources) > 5:
            raise ValueError(f"{path}: expected up to five sources for {qid}")
        for source in sources:
            if set(source) != SOURCES or not all(isinstance(v, str) for v in source.values()):
                raise ValueError(f"{path}: invalid source in {qid}")
        if len({(s["doc"], s["ref"]) for s in sources}) != len(sources):
            raise ValueError(f"{path}: duplicate source in {qid}")
        cases[qid] = c
    if set(cases) != set(questions):
        raise ValueError(f"{path}: missing questions {sorted(set(questions) - set(cases))}")
    return cases


def prepare(baseline_contexts=HERE.parent / "retrieval/contexts.jsonl", registry_contexts=HERE / "contexts.jsonl"):
    data = baseline.load_dataset(HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")
    questions = {c["id"]: c["question"] for c in data["cases"]}
    gold = {c["id"]: c for c in data["gold"]}
    if len(questions) != 12 or gold.keys() != questions.keys():
        raise ValueError("Expected the original twelve work questions and labels")
    contexts = {
        "bm25": load_contexts(baseline_contexts, questions),
        "registry": load_contexts(registry_contexts, questions),
    }
    requests, identity = paired_requests(contexts, data["system"])
    return requests, contexts, gold, identity


def paired_requests(contexts, system):
    requests = {}
    for index, qid in enumerate(sorted(contexts["bm25"])):
        # Switch first route in every pair to balance warm-up and provider changes.
        for route in (ROUTES if index % 2 == 0 else reversed(ROUTES)):
            requests[qid, route] = baseline.make_request(contexts[route][qid], MODEL, system)
    identity = baseline.fingerprint([[qid, route, body] for (qid, route), body in requests.items()])
    return requests, identity


def read_log(path, requests, identity):
    if not path.exists():
        return []
    history = []
    with path.open("rb+") as stream:
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # Only a complete, newline-terminated write is a committed attempt.
                stream.truncate(start)
                break
            row = json.loads(line)
            body = requests.get((row["id"], row["route"]))
            if (row["dataset"] != identity or body is None
                    or row["request_hash"] != baseline.fingerprint(body)):
                raise ValueError("Saved inputs differ. Use a new --out directory.")
            history.append(row)
    return history


def collect(requests, identity, log, history):
    done = {(r["id"], r["route"]) for r in history if r["ok"]}
    for (qid, route), body in requests.items():
        if (qid, route) in done:
            continue
        row = baseline.complete(qid, body, identity)
        row["route"] = route
        with log.open("ab") as f:
            f.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        history.append(row)
        print(qid, route, row["http"], "ok" if row["ok"] else row.get("error"), flush=True)
        if row["http"] in (400, 401, 402, 403, 404, 429) or row["http"] is None:
            # An account/transport/rate-limit problem can invalidate both arms.
            # Running the same command later resumes from the first incomplete pair.
            break


def parse_answer(text):
    raw = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def refs(sources):
    if not isinstance(sources, list):
        return set()
    return {(str(s.get("doc", "")), str(s.get("ref", "")).strip().rstrip("."))
            for s in sources if isinstance(s, dict)}


def load_timings(trace, contexts):
    if not trace.is_file():
        return {}
    values = {}
    for line in trace.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        qid = entry["id"]
        for route, refs_field, time_field in (("bm25", "baseline_source_refs", "baseline_lookup_ms"),
                                              ("registry", "registry_source_refs", "lookup_ms")):
            case = contexts[route].get(qid)
            if case is None or (qid, route) in values:
                raise ValueError(f"{trace}: unexpected or duplicate timing row {qid}")
            names = [f"{s['doc']} {s['ref']}" for s in case["sources"]]
            if entry["question"] != case["question"] or entry[refs_field] != names:
                raise ValueError(f"{trace}: timing belongs to other retrieved sources for {qid} {route}")
            ms = entry[time_field]
            if ms is not None and (not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms < 0):
                raise ValueError(f"{trace}: invalid duration for {qid} {route}")
            values[qid, route] = ms
    return values


def scored(row, case, gold, lookup_ms):
    result = baseline.score(row, gold)
    obj = parse_answer(row["text"])
    cited, available, required = refs(obj.get("sources")), refs(case["sources"]), refs(gold["evidence"])
    valid = (isinstance(obj.get("message"), str) and bool(obj["message"].strip())
             and obj.get("action") in ("answer", "clarify", "escalate", "out_of_scope")
             and isinstance(obj.get("sources"), list)
             and all(isinstance(s, dict) and s.get("doc") and s.get("ref") for s in obj["sources"]))
    result.update(lookup_ms=lookup_ms,
                  estimated_lookup_plus_generation_s=(row["latency_s"] + lookup_ms / 1000)
                  if row["ok"] and lookup_ms is not None else None,
                  route=row["route"], provider=row.get("provider"), served_model=row.get("served_model"),
                  valid_answer=bool(row["ok"] and valid),
                  all_expected_retrieved=required <= available,
                  all_expected_cited=(required <= cited) if gold["expected_action"] == "answer" else None,
                  citations_outside_context=sorted(f"{d} {r}" for d, r in cited - available))
    return result


def save_text(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    tmp.replace(path)


def write_reports(out, requests, contexts, gold, history, timings):
    latest = {(r["id"], r["route"]): r for r in history}
    scores, packet = [], []
    for qid, expected in sorted(gold.items()):
        pair = {"id": qid, "question": expected["question"], "expected_action": expected["expected_action"],
                "reference_answer": expected["reference_answer"], "gold_evidence": expected["evidence"]}
        for route in ROUTES:
            row, case = latest.get((qid, route)), contexts[route][qid]
            if row is None:
                continue
            score = scored(row, case, expected, timings.get((qid, route)))
            scores.append(score)
            pair[route] = {"complete": row["ok"], "text": row["text"], "evidence": case["sources"],
                           "citations_outside_context": score["citations_outside_context"]}
        packet.append(pair)
    if scores:
        tmp = out / "scores.csv.tmp"
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            table = csv.DictWriter(f, fieldnames=scores[0])
            table.writeheader()
            table.writerows(scores)
        tmp.replace(out / "scores.csv")
    save_text(out / "review.jsonl", "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in packet))
    lines = ["# Ручная проверка ответов",
             "Проверь каждое указание, дирекцию, срок и исключения по тексту исходных пунктов. "
             "Для уточнения проверь, что сотруднику не предложили неподтверждённое действие до его ответа. "
             "Для эскалации проверь, хватало ли оснований для прямого ответа. Фиксируй результаты в отдельном "
             "файле с ID вопроса и вариантом поиска. Этот отчёт пересоздаётся при повторном запуске."]
    for pair in packet:
        lines.extend([f"## {pair['id']} · {pair['question']}",
                      f"Ожидаемое действие — {pair['expected_action']}. {pair['reference_answer']}"])
        for route in ROUTES:
            if route not in pair:
                continue
            item = pair[route]
            obj = parse_answer(item["text"])
            message = obj.get("message") or item["text"]
            cites = ", ".join(f"{d} {r}" for d, r in sorted(refs(obj.get("sources"))))
            lines.extend([f"### {route}", str(message), f"Ссылки в ответе — {cites or 'нет'}.",
                          "Пункты, предоставленные модели"])
            for src in item["evidence"]:
                lines.append(f"**{src['doc']} {src['ref']}** {src['text']}")
        lines.append("Эталонные основания")
        for src in pair["gold_evidence"]:
            lines.append(f"**{src['doc']} {src['ref']}** {src['quote']}")
    save_text(out / "review.md", "\n\n".join(lines) + "\n")
    summary = {"model": MODEL, "requested_pairs": len(requests) // 2,
               "timing_scope": "ttft_s and latency_s measure generator HTTP; estimated_lookup_plus_generation_s "
                               "adds a separate warm lookup measurement when present, excludes interface and network to bot",
               "paired_complete": sum(all(p.get(route, {}).get("complete") for route in ROUTES) for p in packet)}
    for route in ROUTES:
        group = [r for r in scores if r["route"] == route and r["complete"]]
        calls = [r for r in history if r["route"] == route]
        costs = [r["usage"]["cost"] for r in calls if r["usage"].get("cost") is not None]
        totals = {"completed": len(group), "attempts": len(calls),
                  "valid_answers": sum(r["valid_answer"] for r in group),
                  "correct_actions": sum(r["action_ok"] for r in group),
                  "direct_answers_expected": sum(r["expected_action"] == "answer" for r in group),
                  "direct_answers_citing_all_expected": sum(r["all_expected_cited"] is True for r in group),
                  "answers_citing_outside_context": sum(bool(r["citations_outside_context"]) for r in group),
                  "reported_cost_usd_including_retries": round(sum(costs), 9) if costs else None,
                  "attempts_without_reported_cost": len(calls) - len(costs)}
        for field in ("ttft_s", "latency_s", "lookup_ms", "estimated_lookup_plus_generation_s", "input_tokens", "output_tokens"):
            values = sorted(r[field] for r in group if r[field] is not None)
            totals[field] = {"observations": len(values), "median": statistics.median(values) if values else None,
                             "p95_nearest_rank": values[math.ceil(.95 * len(values)) - 1] if values else None}
        summary[route] = totals
    by_case = {(r["id"], r["route"]): r for r in scores if r["complete"]}
    pairs = [(by_case[qid, "bm25"], by_case[qid, "registry"]) for qid in gold
             if (qid, "bm25") in by_case and (qid, "registry") in by_case]
    changes = {"correct_action_only_bm25": sum(a["action_ok"] and not b["action_ok"] for a, b in pairs),
               "correct_action_only_registry": sum(b["action_ok"] and not a["action_ok"] for a, b in pairs),
               "pairs_with_different_serving_provider": sum(a["provider"] != b["provider"] for a, b in pairs
                                                           if a["provider"] and b["provider"])}
    for field in ("latency_s", "estimated_lookup_plus_generation_s", "cost_usd_openrouter"):
        differences = [b[field] - a[field] for a, b in pairs if a[field] is not None and b[field] is not None]
        changes[field] = {"pairs": len(differences),
                          "median_registry_minus_bm25": statistics.median(differences) if differences else None}
    summary["paired_changes"] = changes
    save_text(out / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(f"Complete pairs {summary['paired_complete']}/{summary['requested_pairs']}. Reports in {out}")
    return summary["paired_complete"] == summary["requested_pairs"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Offline validation of both inputs and request plan")
    mode.add_argument("--run", action="store_true", help="Run or resume 24 OpenRouter requests")
    mode.add_argument("--report", action="store_true", help="Rebuild reports from saved attempts offline")
    parser.add_argument("--baseline-contexts", type=Path, default=HERE.parent / "retrieval/contexts.jsonl")
    parser.add_argument("--registry-contexts", type=Path, default=HERE / "contexts.jsonl")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--trace", type=Path, default=HERE / "results/retrieval_trace.jsonl",
                        help="Per-route warm lookup times if available")
    args = parser.parse_args()
    requests, contexts, gold, identity = prepare(args.baseline_contexts, args.registry_contexts)
    timings = load_timings(args.trace, contexts)
    if args.check:
        print(f"Validated {len(requests)} requests, 12 pairs, same generator and instructions. No network calls.")
        return 0
    if args.run and not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("OPENROUTER_API_KEY is required for --run")
    if args.report and not (args.out / "attempts.jsonl").is_file():
        parser.error("No attempts in this --out directory")
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another process is writing this output directory")
        log = args.out / "attempts.jsonl"
        history = read_log(log, requests, identity)
        if args.run:
            collect(requests, identity, log, history)
        return 0 if write_reports(args.out, requests, contexts, gold, history, timings) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
