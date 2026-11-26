"""Compare the candidate on 50 working questions; keep questions, reviews and calls private."""
import argparse
import hashlib
import io
import json
import os
import secrets
import statistics
from pathlib import Path
from zipfile import ZipFile

import ask
import evidence
from variants import linked, reference
from policy import fingerprint, full_corpus, rows
from run import cached_call, complete, decide, save
from run_12x3 import SEARCH_MODELS
import thresholds
import procedures

HERE = Path(__file__).resolve().parent
EVAL = HERE.parent


def blind_inputs(packet):
    with ZipFile(packet) as archive:
        search_file = archive.read("code/eval/final15/search.json")
        expected = archive.read("code/eval/final15/search.sha256").decode().split()[0]
        if hashlib.sha256(search_file).hexdigest() != expected:
            raise ValueError("Candidate pool checksum mismatch")
        pools = json.loads(search_file)["inputs"]
        bank = archive.read("bank/bank.zip")
    with ZipFile(io.BytesIO(bank)) as data:
        group = lambda name: [json.loads(line) for line in data.read(name + "/questions.jsonl").splitlines()]
        dev, former_holdout = group("development35"), group("holdout15")
    ranks = {r["id"]: r for r in rows(EVAL / "rerank/rankings.jsonl")}
    original = {r["id"]: r["question"] for r in rows(EVAL / "rerank/questions35.jsonl")}
    if original != {r["id"]: r["question"] for r in dev}:
        raise ValueError("Working bank does not match the ranked inputs")
    cases = {}
    for r in dev:
        group = ranks[r["id"]]
        cases[r["id"]] = (r["question"], group["methods"]["hybrid_condition"], group["pool"])
    for r in former_holdout:
        group = pools[fingerprint(r["question"])]
        cases[r["id"]] = (r["question"], group["seeds"], group["pool"])
    if set(cases) != {f"Q{i:02}" for i in range(1, 51)} or len(dev) != 35 or len(former_holdout) != 15:
        raise ValueError("Expected 35 + 15 distinct working questions")
    return cases, bank


def read_keys(bank):
    with ZipFile(io.BytesIO(bank)) as archive:
        return {r["id"]: r for part in ("development35", "holdout15")
                for line in archive.read(part + "/answer_key.jsonl").splitlines() if (r := json.loads(line))}


def coverage(cases, keys, corpus):
    norms = evidence.REGISTRY()
    counts = {}
    for cid, (question, seeds, pool) in cases.items():
        base = linked([corpus[tuple(k)] for k in seeds], corpus, norms)
        extra = evidence.alternatives(base, pool, corpus)
        fact = procedures.requested_fact(question)
        matched = procedures.fact_sources(fact, question, corpus) if fact else []
        process = procedures.operation(question) if not fact else None
        expanded = procedures.augment_sources(extra, process, question, matched, corpus)
        expected = {reference(r) for r in keys[cid]["evidence"]}
        counts[cid] = [len(expected)] + [len(expected & set(map(reference, group)))
                                         for group in (base, extra, expanded)]
    for n, i in (("links_threshold", 1), ("alternative directorates", 2), ("procedure candidate", 3)):
        denom = sum(bool(v[0]) for v in counts.values())
        hits = sum(0 < v[0] == v[i] for v in counts.values())
        print(f"{n}: all reference clauses available for {hits}/{denom} questions with reference clauses")
    return counts


def diagnose(cases, keys, corpus, out):
    reviews = []
    for cid, (question, _, _) in sorted(cases.items()):
        result = json.loads((out / (cid + ".json")).read_text())
        gold = keys[cid]
        expected = {reference(r) for r in gold["evidence"]}
        cited = {reference(r) for r in result["sources"]}
        sources = [corpus[k] for k in sorted(expected | cited)]
        reviews.append(dict(id=cid, question=question, response=result, expected_action=gold["expected_action"],
                            reference_answer=gold["reference_answer"], missing=gold["missing"], originals=sources))
    save(out / "review.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reviews))
    print("Correct actions:", sum(r["response"]["action"] == r["expected_action"] for r in reviews), "/ 50")
    print("Review all 50 messages against the originals in review.jsonl to determine usable answers.")


def reuse_call(previous, cid, stage, body, live, validate):
    """Reuse an identical model request; never copy a previous final answer."""
    name = f"{cid}-{stage}-{fingerprint(body)}.json"
    if previous is not None and (previous / "calls" / name).is_file():
        def disappeared():
            raise ValueError(f"Previous checkpoint disappeared: {name}")
        row = cached_call(previous, cid, stage, body, disappeared, validate)
        if row["model"] != body["model"]:
            raise ValueError(f"Model differs in checkpoint {name}")
        return dict(row, reused=True)
    return live()


def run(cases, bank, out, model, previous=None):
    if previous is not None and (not (previous / "calls").is_dir() or previous.resolve() == out.resolve()):
        raise ValueError("Previous run must have its own calls directory; use a fresh --out")
    corpus = full_corpus()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    (out / ".gitignore").write_text("*\n")
    calls = out / "calls"
    calls.mkdir(exist_ok=True)
    # Pin inputs, corpus and implementation so resumed outputs cannot mix experiments.
    files = [HERE / f for f in ("run50.py", "evidence.py", "ask.py", "procedures.py", "quality.py", "thresholds.py")]
    files.extend([EVAL / s for s in ("decider/policy.py", "decider/run.py", "linked35/variants.py", "run_12x3.py")])
    digest = fingerprint([
        model, cases, list(corpus.values()), list(evidence.REGISTRY().values()), [p.read_text() for p in files]])
    manifest = out / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != digest:
        raise ValueError("Inputs or candidate changed; use another --out")
    save(manifest, json.dumps(digest))
    for cid, (question, seeds, pool) in sorted(cases.items()):
        result = out / (cid + ".json")
        if result.exists():
            continue
        trace = []
        def generation(_, body, dataset):
            def call():
                r = complete(cid, body, dataset)
                if not r["ok"]:
                    raise RuntimeError(f"{cid} {model} HTTP {r['http']}")
                return {"response": r}
            def validate(response):
                if not response["ok"] or response["request_hash"] != fingerprint(body) or not response["text"]:
                    raise ValueError(f"{cid}: invalid generator checkpoint")
            row = cached_call(out, cid, "generate", body,
                              lambda: reuse_call(previous, cid, "generate", body, call, validate), validate)
            response = row["response"]
            trace.append({"stage": "generate", "reused": row.get("reused", False), "hash": row["request_hash"], "model": body["model"],
                          "seconds": response["latency_s"], "cost": response["usage"].get("cost")})
            return response

        def judge(body):
            def validate(response):
                if not response["answers"]:
                    raise ValueError(f"{cid}: empty judge response")
            row = cached_call(out, cid, "judge", body,
                              lambda: reuse_call(previous, cid, "judge", body, lambda: decide(body), validate),
                              validate)
            trace.append({"stage": "judge", "reused": row.get("reused", False), "hash": row["request_hash"], "model": body["model"],
                          "seconds": row["latency_s"], "cost": row["usage"].get("cost"),
                          "answers": row["response"]["answers"]})
            return row

        final = ask.answer(question, lambda _: (seeds, pool),
                           generate_answer=lambda *args: evidence.generate(*args, call=generation, model=model),
                           judge=lambda body: evidence.judge(body, call=judge),
                           add_sources=evidence.alternatives, instruction="", procedure_mode=True)
        save(out / (cid + ".trace.json"), json.dumps(trace, ensure_ascii=False))
        save(result, json.dumps(final, ensure_ascii=False))
        print(cid, final["action"], flush=True)
    diagnose(cases, read_keys(bank), corpus, out)


def offline(cases, keys):
    coverage(cases, keys, full_corpus())
    class NeedsModels(Exception):
        pass

    def stop(_):
        raise NeedsModels

    local = {}
    for cid, (question, _, _) in cases.items():
        try:
            local[cid] = ask.answer(question, stop, procedure_mode=True)
        except NeedsModels:
            pass
    correct = sum(r["action"] == keys[cid]["expected_action"] for cid, r in local.items())
    print(f"Completed before model calls: {len(local)}/{len(cases)}; expected action: {correct}/{len(local)}")
    return local


def public_check():
    ranks = {r["id"]: r for r in rows(EVAL / "rerank/rankings.jsonl")}
    cases = {r["id"]: (r["question"], ranks[r["id"]]["methods"]["hybrid_condition"], ranks[r["id"]]["pool"])
             for r in rows(EVAL / "rerank/questions35.jsonl")}
    keys = {r["id"]: r for r in rows(EVAL / "rerank/reference35.jsonl")}
    if set(cases) != set(keys) or len(cases) != 35:
        raise ValueError("The public 35-question bank and its references differ")
    local = offline(cases, keys)
    if any(answer["action"] != keys[cid]["expected_action"] for cid, answer in local.items()):
        raise ValueError("A local decision contradicted the public reference")


def summarize(out, cases):
    traces = [json.loads((out / (cid + ".trace.json")).read_text()) for cid in cases]
    calls = [item for trace in traces for item in trace]
    times = sorted(sum(r["seconds"] for r in trace) for trace in traces)
    costs = [r["cost"] for r in calls]
    summary = dict(cases=len(traces), calls=len(calls), api_seconds_p50=statistics.median(times),
                   api_seconds_p95=times[(95 * len(times) + 99) // 100 - 1],
                   total_api_usd=sum(costs) if all(v is not None for v in costs) else None)
    new_costs = [r["cost"] for r in calls if not r["reused"]]
    summary.update(reused_calls=len(calls) - len(new_costs), new_calls=len(new_costs),
                   new_api_usd=sum(new_costs) if all(c is not None for c in new_costs) else None)
    save(out / "measurements.json", json.dumps(summary, indent=2))
    print(summary)
    records = []
    for row in rows(out / "review.jsonl"):
        log = json.loads((out / (row["id"] + ".trace.json")).read_text())
        last = next((call["answers"] for call in reversed(log) if call["stage"] == "judge"), None)
        records.append((row["response"]["action"], last, row["expected_action"]))
    save(out / "threshold-replay.json", json.dumps(thresholds.sweep(records), indent=2))


def paired(base, challenger, directory):
    answers = [{r["id"]: r for r in rows(d / "review.jsonl")} for d in (base, challenger)]
    if answers[0].keys() != answers[1].keys() or len(answers[0]) != 50:
        raise ValueError("Expected two complete runs on the same bank")
    saltfile = directory / "blind-salt"
    if not saltfile.exists():
        save(saltfile, secrets.token_hex(16))
    salt = saltfile.read_text()
    comparisons, unblind = [], {}
    for cid, a in sorted(answers[0].items()):
        b = answers[1][cid]
        if any(a[k] != b[k] for k in ("question", "expected_action", "reference_answer")):
            raise ValueError("Mismatched paired inputs")
        order = hashlib.sha256((salt + cid).encode()).digest()[0] % 2
        left, right = (a,b) if order == 0 else (b,a)
        unblind[cid] = "base" if order == 0 else "challenger"
        originals = {(r["doc"],r["ref"]): r for item in (a,b) for r in item["originals"]}
        comparisons.append(dict(id=cid, question=a["question"], expected_action=a["expected_action"],
                                reference_answer=a["reference_answer"], missing=a["missing"],
                                originals=list(originals.values()), A=left["response"], B=right["response"]))
    save(directory / "paired-review.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in comparisons))
    save(directory / "unblind.json", json.dumps(unblind))


if __name__ == "__main__":
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", nargs="?", type=Path, help="Private IIkuznitsa_Final15.zip")
    parser.add_argument("--public-check", action="store_true", help="Check local routing and source coverage on the published 35")
    parser.add_argument("--check", action="store_true", help="Run offline routing and coverage checks")
    parser.add_argument("--out", type=Path, default=Path("procedures50-private"))
    parser.add_argument("--reuse-calls", type=Path, help="Root of an earlier run, containing per-model call caches")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--model", choices=SEARCH_MODELS, default=ask.GEMMA)
    group.add_argument("--compare-models", action="store_true", help="Run a blinded Gemma/Qwen pair")
    args = parser.parse_args()
    if args.public_check:
        if args.packet or args.check or args.compare_models:
            parser.error("--public-check runs independently of the private packet and comparison")
        public_check()
        raise SystemExit(0)
    if args.packet is None:
        parser.error("Specify the private packet, or use --public-check")
    cases, bank = blind_inputs(args.packet)
    if args.check:
        offline(cases, read_keys(bank))
        print("Historical linked35 threshold replay:", thresholds.historical(EVAL))
    elif not os.getenv("OPENROUTER_API_KEY"):
        parser.error("Live run requires OPENROUTER_API_KEY")
    else:
        args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        (args.out / ".gitignore").write_text("*\n")
        with (args.out / ".run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            folders = []
            for model in (SEARCH_MODELS if args.compare_models else [args.model]):
                folder = args.out / model.replace("/", "_")
                previous = args.reuse_calls / folder.name if args.reuse_calls is not None else None
                run(cases, bank, folder, model, previous)
                summarize(folder, cases)
                folders.append(folder)
            if args.compare_models:
                paired(*folders, args.out)
