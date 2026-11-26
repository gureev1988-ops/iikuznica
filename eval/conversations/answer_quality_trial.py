"""One-variant answer quality experiments with private, resumable traces."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import random
import time
from urllib.error import HTTPError

import reasoning_trial as control
from answer_quality_gates import assess, publication
from check import ask, evidence, fingerprint, load_bank
from policy import decision_body
from variants import make_request

VARIANTS = ("A1", "A5", "K1", "A2", "A3", "A4", "K2", "K3")
TECHNICAL = {"action": "escalate", "offer_quality_request": False, "sources": [],
             "message": "Не удалось проверить ответ из-за сбоя. Попробуй ещё раз."}
ERRORS = (AttributeError, HTTPError, KeyError, OSError, RuntimeError, TypeError, ValueError)


class Calls(control.Calls):
    def __init__(self, out, cid, reuse=None):
        super().__init__(out, cid)
        self.reuse = reuse

    def generate(self, body, dataset, arm=None):
        request = control.settings(body, arm) if arm else body
        if dataset.startswith(("A3-", "A4-", "A5-", "K1-", "K2-", "K3-")):
            request = dict(request, max_tokens=3072)
        metadata = {k: request[k] for k in ("model", "provider", "reasoning", "max_tokens") if k in request}

        def fetch():
            from check import complete
            digest = fingerprint(request)
            for retry in range(3):
                response = complete(self.cid, request, dataset)
                if response["ok"]:
                    break
                control.save(self.out / "calls" / f"{self.cid}-{dataset}-{digest}-{retry}.failure",
                             {"request": request, "response": response})
                transient = (response["http"] in (429, 502, 503, 504) or
                             (response["http"] == 400 and response.get("error") == "Provider returned error"))
                if not transient or retry == 2:
                    break
                time.sleep(2 ** (retry + 1))
            return {"response": response}

        return self.record(dataset, request, fetch, metadata)

    def record(self, stage, body, fetch, metadata):
        digest = fingerprint(body)
        name = f"{self.cid}-{stage}-{digest}.json"
        dest = self.out / "calls" / name
        if not dest.exists() and self.reuse:
            source = self.reuse / "calls" / name
            if source.is_file():
                original = json.loads(source.read_text())
                response = original["response"]
                if (original.get("request", body) == body and
                        (stage == "judge" or response.get("request_hash") == digest)):
                    control.save(dest, original)
        try:
            item = super().record(stage, body, fetch, metadata)
        except ERRORS as exc:
            control.save(self.out / "calls" / f"{name}.failure",
                         {"request": body, "http": getattr(exc, "code", None), "error": str(exc)})
            raise
        if "request" not in item:
            control.save(dest, dict(item, request=body))
        return item


class Trial:
    def __init__(self, variant, calls, lookup):
        self.variant = variant
        self.calls = calls
        self.lookup = lookup
        self.ids = None
        self.events = []

    def selection(self, selection):
        self.ids = selection["ids"] if selection["action"] == "answer" else None
        self.events.append({"stage": "selection", **selection})

    def model_json(self, stage, question, sources, instruction):
        body = make_request({"question": question, "sources": sources}, control.GEMMA, instruction)
        row = self.calls.generate(body, stage, "A")["response"]
        raw = row["text"].strip()
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw.split("\n", 1)[1][:-3]
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result

    def review(self, q, sources, pool, draft, verdict, route, final, attempt, fact):
        ids, self.ids = self.ids, None
        self.events.append(dict(stage="gate", question=q, sources=sources, pool=pool, draft=draft,
                                ids=ids, verdict=verdict, route=route, final=final, attempt=attempt))
        if (self.variant in ("A1", "A5", "K1") and final and final["action"] == "answer"
                and (self.variant != "K1" or evidence.procedures.full_procedure(q))):
            try:
                finding, detail = assess(self.variant, q, sources, draft, ids, self.model_json)
            except ERRORS as exc:
                self.events.append({"stage": self.variant, "error": str(exc)})
                return "escalate", TECHNICAL, None, None
            self.events.append({"stage": self.variant, "detail": detail, "finding": finding})
            if finding:
                return ("revise", None, finding, None) if attempt == 0 else (
                    "escalate", ask.reply("escalate", "gap"), finding, None)
        if self.variant in ("K2", "K3"):
            from answer_quality_routing import gap_route
            return gap_route(self, q, sources, pool, draft, verdict, route, final, attempt, fact)
        return route, final, None, None

    def split(self, question, past, respond):
        if self.variant != "A3":
            return None
        from answer_quality_routing import split_requests
        return split_requests(self, question, past, respond)

    def restore(self, question, past, relation, resolved):
        if self.variant != "A4":
            return relation, resolved
        from answer_quality_routing import restore_object
        return restore_object(self, question, past, relation, resolved)


def pin(path, payload):
    if path.exists() and json.loads(path.read_text()) != payload:
        raise ValueError(f"Inputs changed for {path}; choose a fresh output directory")
    if not path.exists():
        control.save(path, payload)


def cost(calls):
    return [call["usage"].get("cost") if call["usage"] else None for call in calls]


def latency(samples):
    if not samples:
        return {}
    ordered = sorted(samples)
    return {f"p{percentile}": round(ordered[math.ceil(len(ordered) * percentile / 100) - 1], 3)
            for percentile in (50, 95)}


def summarize(rows, *, panel):
    summary = {}
    for arm in ("control", "candidate"):
        calls, times = [], []
        counts = Counter()
        for row in rows:
            result = row.get(arm)
            if not result:
                continue
            counts[("technical_error" if result.get("error") else result.get("action") or "no_final")] += 1
            if panel and result.get("action") == "answer":
                counts["mutated_published" if row.get("mutated") else "unmutated_published"] += 1
            calls.extend(result.get("trace", []))
            times.append(result["wall_seconds"])
        prices = cost(calls)
        summary[arm] = {"counts": dict(counts), "calls": len(calls),
                        "known_cost_usd": round(sum(x for x in prices if x is not None), 9),
                        "missing_costs": prices.count(None), "wall_seconds": latency(times),
                        "model_seconds": latency([x["seconds"] for x in calls])}
    return summary


def frozen_gate_panel(variant, frames, out, *, jobs=4):
    frozen = [{**frame, "sources": list(frame["sources"])} for frame in frames]
    pin(out / "frozen.json", frozen)
    # Order is determined after freezing. Neither labels nor the pair ID enter the model.
    shuffled = list(frozen)
    random.Random(fingerprint(frozen)).shuffle(shuffled)

    def run(frame):
        q, draft, sources = (frame[k] for k in ("question", "draft", "sources"))
        identifier = fingerprint([q, draft, sources])[:20]
        calls = Calls(out, identifier)
        trial = Trial(variant, calls, None)
        data = {k: frame[k] for k in ("id", "mutated", "group")}
        start = time.monotonic()
        try:
            body = decision_body(q, draft, sources, False)
            body["questions"] = dict(body["questions"], scope=ask.SCOPE)
            verdict = evidence.judge(body, call=calls.judge)["response"]
            final = publication(draft, verdict, q, sources)
            data["control"] = {**(final or {}), "wall_seconds": time.monotonic() - start,
                               "trace": list(calls.trace)}
            data["gate_scores"] = {k: verdict["answers"][k] for k in ("route", "grounded", "complete")}
        except ERRORS as exc:
            data["control"] = dict(TECHNICAL, error=str(exc), trace=calls.trace,
                                   wall_seconds=time.monotonic() - start)
            return data
        start = time.monotonic()
        try:
            # Evaluate the check even when C withholds the draft. Publication still requires C.
            finding, detail = assess(variant, q, sources, draft, frame.get("ids"), trial.model_json)
            result = ask.reply("escalate", "gap") if final and final["action"] == "answer" and finding else final
            data["candidate"] = {**(result or {}), "finding": finding, "detail": detail}
        except ERRORS as exc:
            data["candidate"] = {**TECHNICAL, "error": str(exc)}
        data["candidate"].update(wall_seconds=time.monotonic() - start, trace=calls.trace[1:])
        return data

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        rows = list(executor.map(run, shuffled))
    control.save(out / "rows.json", rows)
    control.save(out / "summary.json", {group: summarize([r for r in rows if r["group"] == group], panel=True)
                                         for group in sorted({r["group"] for r in rows})})
    return rows


def gate_score(row):
    return min(row["gate_scores"][k]["noul"] for k in ("grounded", "complete"))


def calibrate_threshold(rows):
    desired = sum(not row["mutated"] and row.get("candidate", {}).get("action") == "answer" for row in rows)
    scores = [gate_score(row) for row in rows if not row["mutated"] and row["control"].get("action") == "answer"]
    limits = sorted({0.5, 1.0, *(math.nextafter(score, 1.0) for score in scores)})
    matches = [limit for limit in limits if sum(score >= limit for score in scores) == desired]
    return {"desired_correct": desired, "passed_correct": len(scores), "threshold": matches[0] if matches else None}


def run_cases(variant, cases, lookup, out, reuse=None):
    rows = []
    for case in cases:
        past = []
        for index, turn in enumerate(case["turns"], 1):
            position = case.get("position", 0) + index
            cid = f'{case["id"]}.{position}'
            calls = Calls(out, cid, reuse=reuse)
            trial = Trial(variant, calls, lookup)
            q = turn["question"]
            start = time.monotonic()
            try:
                row = control.run_turn(out, cid, q, past, lookup, "C", diagnose=True, observer=trial)
            except ERRORS as exc:
                row = {"turn": cid, "response": dict(TECHNICAL, resolved_question=q),
                       "trace": calls.trace, "error": str(exc)}
            reply = row["response"]
            row.update(question=q, past=list(past), events=trial.events, wall_seconds=time.monotonic() - start)
            past.append(control.prior_turn(q, reply))
            rows.append(row)
            control.save(out / "rows.json", rows)
            print(cid, reply["action"], row.get("error", ""), flush=True)
    control.save(out / "summary.json", summarize(
        [{"candidate": dict(row["response"], trace=row["trace"], wall_seconds=row["wall_seconds"],
                            error=row.get("error"))} for row in rows], panel=False)["candidate"])


def frozen_bank(working, cache):
    if working is None:
        return load_bank()
    from run50 import blind_inputs
    inputs, _ = blind_inputs(working)
    if len(inputs) != 50:
        raise ValueError("Expected the 50-question working bank")
    cache.update({fingerprint(q): [seeds, pool] for q, seeds, pool in inputs.values()})
    return [{"id": cid, "turns": [{"question": q}]} for cid, (q, _, _) in inputs.items()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("C", *VARIANTS), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--working50", type=Path)
    parser.add_argument("--reuse", type=Path, help="Exact request checkpoints from a prior run")
    parser.add_argument("--case", action="append")
    parser.add_argument("--panel", action="store_true")
    parser.add_argument("--calibration", action="store_true", help="Run the separate A5 threshold calibration")
    parser.add_argument("--threshold-from", type=Path, help="Replay a calibrated threshold on the A5 panel")
    parser.add_argument("--control-traces", type=Path, help="Instrumented C rows for working diagnostics")
    args = parser.parse_args()
    if args.threshold_from and (args.variant != "A5" or not args.panel):
        parser.error("--threshold-from applies only to the A5 panel")
    if args.variant == "A2" and not args.panel:
        parser.error("A2 needs the publication snapshots provided by --panel")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    corpus, lookup, cache = control.lookup_factory(args.model_dir, args.python)
    if args.calibration:
        if args.variant != "A5" or args.panel or args.working50 or args.case:
            parser.error("--calibration applies only to the standalone A5 calibration")
        from answer_quality_panels import A5_CALIBRATION, fact_frames
        rows = frozen_gate_panel("A5", list(fact_frames(corpus, A5_CALIBRATION, "calibration")), args.out)
        control.save(args.out / "threshold.json", calibrate_threshold(rows))
    elif args.panel:
        from answer_quality_panels import panel
        rows = panel(args.variant, corpus, lookup, args.out, args.control_traces)
        if args.threshold_from:
            threshold = json.loads(args.threshold_from.read_text())["threshold"]
            retained = [row for row in rows if row["group"] == "pilot_pairs" and
                        row["control"].get("action") == "answer" and threshold is not None and
                        gate_score(row) >= threshold]
            control.save(args.out / "threshold-replay.json", {"threshold": threshold,
                         "mutated_published": sum(row["mutated"] for row in retained),
                         "unmutated_published": sum(not row["mutated"] for row in retained)})
    else:
        cases = frozen_bank(args.working50, cache)
        if args.case:
            selected = set(args.case)
            cases = [case for case in cases if case["id"] in selected]
            if {case["id"] for case in cases} != selected:
                parser.error("Unknown case")
        pin(args.out / "manifest.json", {"variant": args.variant,
            "inputs": fingerprint([[c["id"], [t["question"] for t in c["turns"]]] for c in cases]),
            "corpus": fingerprint(list(corpus.values())),
            "working_zip": hashlib.sha256(args.working50.read_bytes()).hexdigest() if args.working50 else None})
        run_cases(args.variant, cases, lookup, args.out, args.reuse)


if __name__ == "__main__":
    main()
