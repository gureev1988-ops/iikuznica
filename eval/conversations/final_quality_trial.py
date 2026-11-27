"""Pair independent quality candidates with the pinned A1 working control."""
import argparse
from collections import Counter
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import time

import reasoning_trial as control
from answer_quality_trial import Calls, ERRORS, TECHNICAL, Trial, frozen_bank, pin, summarize
from check import ask, evidence, fingerprint
from policy import decision_body

BASE = "1cf4496d525edc9d4ccba8941ff8388abc939082"
VISIBLE = ("action", "message", "sources", "requests", "offer_quality_request")


def reviewed(trial, question, draft, sources):
    """Apply the same Decider and A1 check to a new candidate draft."""
    body = decision_body(question, draft, sources, False)
    body["questions"] = dict(body["questions"], scope=ask.SCOPE)
    verdict = evidence.judge(body, call=trial.calls.judge)["response"]
    fact = evidence.procedures.requested_fact(question)
    route, result = ask.scoped_result(verdict, draft, sources, fact)
    _, result, _, _ = trial.review(question, sources, [], draft, verdict, route, result, 1, fact)
    return result


def frozen_turn(cid, question, turn, trial):
    draft, sources = turn["frozen"]
    trial.ids = turn.get("claim_ids")
    result = reviewed(trial, question, draft, sources)
    if result is None:
        result = ask.reply("escalate", "gap")
    return {"turn": cid, "response": dict(result, resolved_question=question), "trace": trial.calls.trace}


def run_arm(arm, candidate, cases, lookup, out, reuse):
    out.mkdir(mode=0o700, exist_ok=True)
    rows = []
    for case in cases:
        past = list(case.get("history", ()))
        for i, turn in enumerate(case["turns"], 1):
            cid = f"{case['id']}.{case.get('position', 0) + i}"
            question = turn["question"]
            calls = Calls(out, cid, reuse=reuse)
            trial = Trial("A1", calls, lookup)
            start = time.monotonic()
            try:
                if "frozen" in turn:
                    row = frozen_turn(cid, question, turn, trial)
                else:
                    row = control.run_turn(out, cid, question, past, lookup, "C", diagnose=True, observer=trial)
            except ERRORS as exc:
                row = {"turn": cid, "response": dict(TECHNICAL, resolved_question=question),
                       "trace": calls.trace, "error": str(exc)}
            if candidate and not row.get("error"):
                try:
                    replacement = candidate(arm, question, past, row, trial)
                    if replacement is not None:
                        row["response"] = dict(replacement, resolved_question=row["response"]["resolved_question"])
                except ERRORS as exc:
                    trial.events.append({"stage": "candidate_error", "error": str(exc)})
            row.update(question=question, past=list(past), events=trial.events, wall_seconds=time.monotonic() - start)
            past.append(control.prior_turn(question, row["response"]))
            rows.append(row)
            control.save(out / "rows.json", rows)
            print(arm, cid, row["response"]["action"], row.get("error", ""), flush=True)
    return rows


def metrics(rows):
    summary = summarize([{"candidate": dict(r["response"], trace=r["trace"], wall_seconds=r["wall_seconds"],
                                           error=r.get("error"))} for r in rows], panel=False)["candidate"]
    summary["stages"] = dict(Counter(c["stage"] for r in rows for c in r["trace"]))
    summary["events"] = dict(Counter(e["stage"] for r in rows for e in r["events"] if e["stage"].startswith("F")))
    summary["candidate_errors"] = sum(any(e["stage"] == "candidate_error" for e in r["events"]) for r in rows)
    return summary


def visible(reply):
    return {k: reply[k] for k in VISIBLE if k in reply}


def pairs(by_arm, cases):
    keys = {f"{case['id']}.{case.get('position', 0)+i}": turn.get("expect")
            for case in cases for i, turn in enumerate(case["turns"], 1)}
    result = {cid: {"turn": cid, "expect": expect, "arms": {}} for cid, expect in keys.items()}
    for arm, rows in by_arm.items():
        for row in rows:
            item = result[row["turn"]]
            item.update(question=row["question"])
            item["arms"][arm] = visible(row["response"])
    return list(result.values())


def main(panel, candidate, *, arms=("candidate",), description, check=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--suite", choices=("panel", "working42", "working50"), default="panel")
    parser.add_argument("--arm", choices=("all", "control", *arms), default="all")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--working50", type=Path)
    parser.add_argument("--reuse", type=Path, help="Previously saved exact model requests")
    parser.add_argument("--independent-calls", action="store_true", help="Do not reuse identical calls across paired arms")
    parser.add_argument("--check", action="store_true", help="Validate the fixture and local invariants without model calls")
    args = parser.parse_args()
    if (args.suite == "working50") != (args.working50 is not None):
        parser.error("--working50 requires --suite working50, and vice versa")
    if args.independent_calls and args.reuse:
        parser.error("--independent-calls and --reuse are mutually exclusive")
    if not args.check and (not args.out or not args.model_dir):
        parser.error("Live runs require --out and the pinned --model-dir")
    if not args.check and (not args.model_dir.is_dir() or not shutil.which(args.python)):
        parser.error("The MiniLM model directory and ranking Python must exist")
    corpus, lookup, cache = control.lookup_factory(args.model_dir, args.python)
    cases = panel(corpus) if args.suite == "panel" else frozen_bank(args.working50, cache)
    if len({c["id"] for c in cases}) != len(cases) or not cases or any(not c["turns"] for c in cases):
        raise ValueError("The selected panel has missing or repeated cases")
    turns = sum(len(c["turns"]) for c in cases)
    expected = {"working42": 42, "working50": 50}.get(args.suite)
    if expected and turns != expected:
        raise ValueError(f"Expected {expected} turns, found {turns}")
    if args.check:
        if check:
            check(corpus)
        print(f"{len(cases)} scenarios, {turns} turns. Offline checks passed.")
        return
    if not os.getenv("OPENROUTER_API_KEY"):
        parser.error("Live runs require OPENROUTER_API_KEY in the environment")
    output = args.out
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    (output / ".gitignore").write_text("*\n", encoding="utf-8")
    revision = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    pin(output / "manifest.json", {
        "revision": revision, "base": BASE, "suite": args.suite, "inputs": fingerprint(cases),
        "corpus": fingerprint(list(corpus.values())), "model_dir": str(args.model_dir.resolve()),
        "python": args.python, "arm": args.arm, "reuse": str(args.reuse.resolve()) if args.reuse else None,
        "independent_calls": args.independent_calls,
        "working_zip_sha256": hashlib.sha256(args.working50.read_bytes()).hexdigest() if args.working50 else None,
    })
    selected = ("control", *arms) if args.arm == "all" else (args.arm,)
    paired_reuse = args.arm == "all" and not args.independent_calls
    warm = bool(paired_reuse or args.reuse or any(output.glob("*/calls/*.json")))
    # The existing routing wrapper temporarily patches ask.answer. Runs within
    # this process are sequential and each arm builds its own conversation.
    by_arm = {}
    for arm in selected:
        reuse = output / "control" if paired_reuse and arm != "control" else args.reuse
        by_arm[arm] = run_arm(arm, None if arm == "control" else candidate, cases, lookup, output / arm, reuse)
    comparison = pairs(by_arm, cases)
    changed = {arm: sum(p["arms"][arm] != p["arms"]["control"] for p in comparison)
               for arm in selected if arm != "control" and "control" in by_arm}
    control.save(output / "pairs.json", comparison)
    control.save(output / "summary.json", {"warm_or_reused": warm, "changed_visible": changed,
                                          "arms": {arm: metrics(rows) for arm, rows in by_arm.items()}})
