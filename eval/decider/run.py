"""Paired, resumable Decisions API experiment over frozen Gemma responses."""
import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from urllib import error, request

from policy import (ENDPOINT, GEMMA, HERE, MESSAGES, REPAIR, citations, decision_body,
                    draft, extended, fingerprint, full_corpus, gate, inputs, make_request, reference)
from run_12x3 import complete
from report import write_report


def save(path, text):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def decide(body):
    headers = {"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"], "Content-Type": "application/json"}
    req = request.Request(ENDPOINT, data=encode(body).encode(), headers=headers)
    start = time.monotonic()
    with request.urlopen(req, timeout=150) as response:
        result = json.load(response)
        return {"response": result, "http": response.status, "usage": result.get("usage") or {},
                "latency_s": round(time.monotonic() - start, 3)}


def cached_call(out, case_id, stage, body, call, validate):
    digest = fingerprint(body)
    path = out / "calls" / f"{case_id}-{stage}-{digest}.json"
    if path.exists():
        record = json.loads(path.read_text())
        if record["request_hash"] != digest or record["id"] != case_id or record["stage"] != stage:
            raise ValueError(f"Bad checkpoint {path.name}")
        validate(record["response"])
        return record

    result = call()
    validate(result["response"])
    record = dict(result, id=case_id, stage=stage, request_hash=digest, model=body["model"])
    save(path, encode(record) + "\n")
    return record


def evaluate(case, original, pool, corpus, out):
    case_id = case["id"]
    initial = draft(original["text"])
    current = initial
    sources = case["sources"]
    calls = []
    decisions = []
    expanded = False
    for round_no in (1, 2):
        body = decision_body(case["question"], current, sources, expanded)
        row = cached_call(out, case_id, f"decision{round_no}", body, lambda: decide(body),
                          lambda result: gate(result, current, sources))
        calls.append(row)
        verdict, final = gate(row["response"], current, sources)
        decisions.append(verdict)
        if final is not None:
            break
        if round_no == 2:
            final = {"action": "escalate", "message": MESSAGES["escalate"], "sources": []}
            break
        if verdict == "retrieve":
            sources = extended(sources, pool, corpus)
            expanded = True
        repair = make_request(dict(case, sources=sources), GEMMA, REPAIR)

        def regenerate():
            row = complete(case_id, repair, "decider35")
            if not row["ok"]:
                raise RuntimeError(f"{case_id} Gemma HTTP {row['http']}: {row.get('error', 'Incomplete stream')}")
            return {"response": row, "http": row["http"], "usage": row["usage"], "latency_s": row["latency_s"]}

        row = cached_call(out, case_id, "repair", repair, regenerate, lambda r: draft(r["text"]))
        calls.append(row)
        current = draft(row["response"]["text"])

    costs = [c["usage"].get("cost") for c in calls]
    return {"id": case_id, "question": case["question"], "baseline": initial, "final": final,
            "baseline_request_hash": original["request_hash"], "decisions": decisions, "expanded": expanded,
            "source_refs": [reference(p) for p in sources], "calls": [
                {k: r[k] for k in ("stage", "request_hash", "latency_s", "usage")} for r in calls],
            "added_api_s": round(sum(r["latency_s"] for r in calls), 3),
            "added_usd": round(sum(costs), 12) if None not in costs else None}


def check(cases, originals, ranks, corpus):
    normalized = []
    for case in cases:
        cid = case["id"]
        obj = draft(originals[cid]["text"])
        refs = citations(obj, case["sources"])
        if refs is None:
            raise ValueError(f"Unresolved source in baseline {cid}")
        if refs != obj["sources"]:
            normalized.append(cid)
        extra = extended(case["sources"], ranks[cid], corpus)
        keys = [reference(s) for s in extra]
        if len(set(keys)) != len(keys) or keys[:len(case["sources"])] != [reference(s) for s in case["sources"]]:
            raise ValueError(f"Broken expansion {cid}")
        if cid == "Q22":
            needed = {("DB", "8." + str(n)) for n in range(4, 9)}
            print(f"Q22 coverage: baseline={len(needed.intersection(keys[:5]))}/5, expanded={len(needed.intersection(keys))}/5")
        for sources, expanded in ((case["sources"], False), (extra, True)):
            state = json.loads(decision_body(case["question"], obj, sources, expanded)["state"])
            if set(state) != {"employee_question", "draft", "sources", "extra_search_already_done"}:
                raise ValueError("Unexpected judge inputs")
    print(f"{len(cases)} paired requests verified; normalized citation shapes: {','.join(normalized)}")


def run(out):
    cases, originals, ranks = inputs()
    corpus = full_corpus()
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another run already owns this output directory") from None
        # A resume must use the same policies, frozen drafts, expansion and scoring.
        manifest = {"version": 1, "inputs": fingerprint([cases, originals, ranks, list(corpus.values())]),
                    "code": fingerprint([p.read_text() for p in (HERE / "policy.py", HERE / "run.py", HERE / "report.py",
                                                                HERE.parent / "run_12x3.py")])}
        path = out / "manifest.json"
        if path.exists() and json.loads(path.read_text()) != manifest:
            raise RuntimeError("Inputs or code changed; choose a fresh --out directory")
        save(path, encode(manifest) + "\n")
        (out / "calls").mkdir(exist_ok=True)
        old_path = out / "outcomes.jsonl"
        published = len(old_path.read_text().splitlines()) if old_path.exists() else 0
        outcomes = []
        for case in cases:
            cid = case["id"]
            outcome = evaluate(case, originals[cid], ranks[cid], corpus, out)
            outcomes.append(outcome)
            if len(outcomes) >= published:
                save(old_path, "".join(encode(o) + "\n" for o in outcomes))
                write_report(out, outcomes, originals, save)
            print(f"{len(outcomes)}/35 {cid} {' -> '.join(outcome['decisions'])} -> {outcome['final']['action']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate all 35 inputs offline")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args()
    try:
        if args.check:
            cases, originals, ranks = inputs()
            check(cases, originals, ranks, full_corpus())
        elif not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("OPENROUTER_API_KEY is required for a live run")
        else:
            run(args.out)
    except error.HTTPError as exc:
        wait = exc.headers.get("Retry-After")
        sys.exit(f"OpenRouter HTTP {exc.code}. Check access or quota and rerun; successful calls are cached."
                 + (f" Retry-After: {wait}" if wait else ""))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        sys.exit(f"Experiment stopped: {exc}. Rerun after correcting the error; successful calls are cached.")
