"""Paired linked-source, condition-audit and route-confidence experiment."""
import argparse
import fcntl
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib import error

from variants import ROOT, condition_arm, generation, index, linked, threshold
from policy import HERE, fingerprint, full_corpus, inputs, load_dataset, reference, rows
from report import SAFE, stats
from run import encode, evaluate, save


def data():
    cases, originals, ranks = inputs()
    corpus, norms = full_corpus(), index()
    previous = {r["id"]: r for r in rows(HERE / "results/outcomes.jsonl")}
    if set(previous) != set(originals):
        raise ValueError("Historical verdicts do not cover the paired questions")
    return cases, originals, ranks, corpus, norms, previous


def last_decision(out, historic):
    entry = next(c for c in reversed(historic["calls"]) if c["stage"].startswith("decision"))
    path = out / "calls" / f"{historic['id']}-{entry['stage']}-{entry['request_hash']}.json"
    row = json.loads(path.read_text())
    if row["http"] != 200 or row["request_hash"] != entry["request_hash"]:
        raise ValueError(f"Invalid decision checkpoint: {path.name}")
    return row["response"]


def replay(cases, originals, corpus, norms, previous, gold):
    cases_out = []
    for case in cases:
        cid = case["id"]
        orig = previous[cid]
        if orig["baseline_request_hash"] != originals[cid]["request_hash"]:
            raise ValueError(f"Historical baseline changed: {cid}")
        base = case["sources"]
        extra = linked(base, corpus, norms)
        keys = [reference(s) for s in extra]
        if len(keys) != len(set(keys)) or keys[:len(base)] != [reference(s) for s in base]:
            raise ValueError(f"Invalid linked sources: {cid}")
        target = {reference(s) for s in gold[cid]["evidence"]}
        after = threshold(orig["final"], last_decision(HERE / "results", orig))
        cases_out.append({"id": cid, "expected": gold[cid]["expected_action"],
                          "baseline_action": orig["final"]["action"], "threshold_final": after,
                          "baseline_ref_hits": len(target.intersection(keys[:len(base)])),
                          "linked_ref_hits": len(target.intersection(keys)), "expected_refs": len(target),
                          "source_refs": keys})
    direct = [r for r in cases_out if r["expected"] == "answer"]
    return {"n": len(cases_out),
            "correct_baseline_actions": sum(r["baseline_action"] == r["expected"] for r in cases_out),
            "correct_threshold_actions": sum(r["threshold_final"]["action"] == r["expected"] for r in cases_out),
            "direct_questions": len(direct),
            "full_direct_refs_before": sum(r["baseline_ref_hits"] == r["expected_refs"] for r in direct),
            "full_direct_refs_after": sum(r["linked_ref_hits"] == r["expected_refs"] for r in direct),
            "all_refs_before": sum(r["baseline_ref_hits"] == r["expected_refs"] for r in cases_out),
            "all_refs_after": sum(r["linked_ref_hits"] == r["expected_refs"] for r in cases_out),
            "cases": cases_out}


def usage(calls):
    costs = [r["usage"].get("cost") for r in calls]
    return {"api_s": round(sum(r["latency_s"] for r in calls), 3),
            "usd": round(sum(costs), 12) if None not in costs else None,
            "calls": [{k: r[k] for k in ("stage", "request_hash")} for r in calls]}


def run_case(case, hist, baseline, ranks, norms, corpus, system, out):
    cid = case["id"]
    linked_case = dict(case, sources=linked(case["sources"], corpus, norms))
    gen = generation(out, cid, "linked_draft", linked_case, system)
    path = evaluate(linked_case, gen["response"], ranks[cid], corpus, out)
    last = last_decision(out, path)
    served_sources = [corpus[tuple(key)] for key in path["source_refs"]]
    audited, last_audit, extra, flags = condition_arm(out, dict(case, sources=served_sources), path["final"],
                                                     last, norms, path["expanded"])
    baseline_calls = [{"stage": "historical_draft", "request_hash": baseline["request_hash"],
                       "usage": baseline["usage"], "latency_s": baseline["latency_s"]}] + hist["calls"]
    common_calls = [gen] + path["calls"]
    answers = {"baseline": hist["final"], "links": path["final"], "conditions": audited}
    terminal = {"baseline": last_decision(HERE / "results", hist), "links": last, "conditions": last_audit}
    for name in terminal:
        answers[name + "_threshold"] = threshold(answers[name], terminal[name])
    return {"id": cid, "question": case["question"], "initial_refs": [reference(s) for s in linked_case["sources"]],
            "served_refs": path["source_refs"], "linked_decisions": path["decisions"], "condition_flags": flags,
            "answers": answers, "cost_time": {"baseline": usage(baseline_calls), "links": usage(common_calls),
                                               "conditions": usage(common_calls + extra)}}


def reports(out, cases, gold):
    summary = {"n": len(cases), "complete": {r["id"] for r in cases} == set(gold), "variants": {}}
    for name in cases[0]["answers"]:
        counts = Counter()
        wrong, safe_lost, regressions = [], [], []
        times, costs = [], []
        component = name.split("_")[0]
        for r in cases:
            cid = r["id"]
            expected = gold[cid]["expected_action"]
            correct = r["answers"][name]["action"] == expected
            counts[expected] += correct
            if not correct:
                wrong.append(cid)
                if r["answers"]["baseline"]["action"] == expected:
                    regressions.append(cid)
                if cid in SAFE:
                    safe_lost.append(cid)
            measure = r["cost_time"][component]
            times.append(measure["api_s"])
            costs.append(measure["usd"])
        summary["variants"][name] = {"correct_actions": sum(counts.values()), "correct_by_action": counts,
                                     "incorrect_action_ids": wrong, "regressed_action_ids": regressions,
                                     "safe_controls_lost_direct_answer": safe_lost,
                                     "api_s": stats(times), "usd": round(sum(costs), 12) if None not in costs else None}
    summary["controls_with_condition_repair"] = [r["id"] for r in cases if r["id"] in SAFE
                                                and r["condition_flags"] and r["condition_flags"][0]]
    save(out / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    save(out / "outcomes.jsonl", "".join(encode(r) + "\n" for r in cases))


def review(out, cases, gold, corpus):
    text = ["# Тексты для сверки с регламентами", "",
            "Проверь перенос условий в Q10 и Q22, достаточность уточнения в Q11 и предложение эскалации в Q20. "
            "Зафиксируй смысловые ошибки даже при правильном действии. Ниже также приведены шесть контрольных прямых вопросов.", ""]
    for r in cases:
        if r["id"] not in SAFE | {"Q10", "Q11", "Q20", "Q22"}:
            continue
        text.extend([f"## {r['id']}. {r['question']}", "", "Эталонные источники:", ""])
        for source in gold[r["id"]]["evidence"]:
            p = corpus[reference(source)]
            text.extend([f"- {p['doc']} {p['text']}", ""])
        prior = {}
        for arm, answer in r["answers"].items():
            key = encode(answer)
            previous = prior.get(key)
            view = ("Совпадает с " + previous) if previous else (answer["action"] + " — " + answer["message"]
                   + "\n\nСсылки: " + encode(answer["sources"]))
            text.extend([f"**{arm}**", "", view, ""])
            prior.setdefault(key, arm)
    save(out / "review.md", "\n".join(text))


def experiment(out, check, offline_report):
    cases, originals, ranks, corpus, norms, previous = data()
    gold = {r["id"]: r for r in rows(HERE.parent / "rerank/reference35.jsonl")}
    prep = replay(cases, originals, corpus, norms, previous, gold)
    print("Offline:", {k: v for k, v in prep.items() if k != "cases"}, flush=True)
    if offline_report:
        save(offline_report, json.dumps(prep, ensure_ascii=False, indent=2) + "\n")
    if check:
        return
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required for new model calls")
    system = load_dataset(HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")["system"]
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        files = [ROOT/"experiment.py", ROOT/"variants.py", HERE/"run.py", HERE/"policy.py", HERE/"report.py",
                 HERE.parent/"run_12x3.py", HERE.parent/"registry/registry.py"]
        manifest = fingerprint([cases, originals, ranks, previous, prep, system, list(norms.values()), gold,
                                [p.read_text() for p in files]])
        pin = out / "manifest.json"
        if pin.exists() and json.loads(pin.read_text()) != manifest:
            raise ValueError("Experiment changed; use a fresh --out directory")
        save(pin, encode(manifest) + "\n")
        for folder in ("calls", "cases"):
            (out / folder).mkdir(exist_ok=True)
        finished = {}
        for case in cases:
            cid = case["id"]
            path = out / "cases" / (cid + ".json")
            if path.exists():
                row = json.loads(path.read_text())
                if row["id"] != cid:
                    raise ValueError("Mismatched case checkpoint")
            else:
                for attempt in range(3):
                    try:
                        row = run_case(case, previous[cid], originals[cid], ranks, norms, corpus, system, out)
                        break
                    except error.HTTPError as exc:
                        delay = exc.headers.get("Retry-After", str(5 * (attempt + 1)))
                        if (exc.code not in (429, 502, 503, 504) or attempt == 2
                                or not delay.isdigit() or int(delay) > 60):
                            raise
                        print(f"{cid}: HTTP {exc.code}, retry in {delay}s", flush=True)
                        time.sleep(int(delay))
                save(path, encode(row) + "\n")
            finished[cid] = row
            # Rebuild partial reports without truncating a previously longer report on resume.
            if len(finished) == len(list((out / "cases").glob("*.json"))):
                reports(out, list(finished.values()), gold)
                review(out, list(finished.values()), gold, corpus)
            print(f"{len(finished)}/35 {cid}", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true", help="work offline on saved responses and original documents")
    p.add_argument("--offline-report", type=Path, help="save the deterministic replay and clause coverage")
    p.add_argument("--out", type=Path, default=ROOT / "results")
    args = p.parse_args()
    try:
        experiment(args.out, args.check, args.offline_report)
    except error.HTTPError as exc:
        sys.exit(f"OpenRouter HTTP {exc.code}; successful calls are saved. Retry-After: {exc.headers.get('Retry-After', 'unspecified')}")
    except (OSError, KeyError, ValueError, TypeError, RuntimeError) as exc:
        sys.exit(f"Experiment stopped: {exc}")
