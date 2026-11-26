"""Compare the candidate on 50 working questions; keep questions, reviews and calls private."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
from zipfile import ZipFile

import ask
import evidence
from variants import linked, reference
from policy import fingerprint, full_corpus, rows
from run import cached_call, complete, decide, save

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
    for cid, (_, seeds, pool) in cases.items():
        base = linked([corpus[tuple(k)] for k in seeds], corpus, norms)
        extra = evidence.alternatives(base, pool, corpus)
        expected = {reference(r) for r in keys[cid]["evidence"]}
        counts[cid] = [len(expected), len(expected & set(map(reference, base))),
                       len(expected & set(map(reference, extra)))]
    for n, i in (("links_threshold", 1), ("alternative directorates", 2)):
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


def run(cases, bank, out):
    corpus = full_corpus()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    (out / ".gitignore").write_text("*\n")
    calls = out / "calls"
    calls.mkdir(exist_ok=True)
    # Pin inputs, corpus and implementation so resumed outputs cannot mix experiments.
    files = [HERE / f for f in ("run50.py", "evidence.py", "ask.py")]
    files.extend([EVAL / s for s in ("decider/policy.py", "decider/run.py", "linked35/variants.py", "run_12x3.py")])
    digest = fingerprint([
        cases, list(corpus.values()), list(evidence.REGISTRY().values()), [p.read_text() for p in files]])
    manifest = out / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()) != digest:
        raise ValueError("Inputs or candidate changed; use another --out")
    save(manifest, json.dumps(digest))
    for cid, (question, seeds, pool) in sorted(cases.items()):
        result = out / (cid + ".json")
        if result.exists():
            continue
        def generation(_, body, dataset):
            def call():
                r = complete(cid, body, dataset)
                if not r["ok"]:
                    raise RuntimeError(f"{cid} Gemma HTTP {r['http']}")
                return {"response": r}
            return cached_call(out, cid, "generate", body, call, lambda r: r["text"])["response"]

        def judge(body):
            return cached_call(out, cid, "judge", body, lambda: decide(body), lambda v: v["answers"])

        final = ask.answer(question, lambda _: (seeds, pool),
                           generate_answer=lambda *args: evidence.generate(*args, call=generation),
                           judge=lambda body: evidence.judge(body, call=judge),
                           add_sources=evidence.alternatives, instruction="")
        save(result, json.dumps(final, ensure_ascii=False))
        print(cid, final["action"], flush=True)
    diagnose(cases, read_keys(bank), corpus, out)


if __name__ == "__main__":
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path, help="Private IIkuznitsa_Final15.zip")
    parser.add_argument("--check", action="store_true", help="Compare source coverage offline")
    parser.add_argument("--out", type=Path, default=Path("core50-private"))
    args = parser.parse_args()
    cases, bank = blind_inputs(args.packet)
    if args.check:
        coverage(cases, read_keys(bank), full_corpus())
    elif not os.getenv("OPENROUTER_API_KEY"):
        parser.error("Live run requires OPENROUTER_API_KEY")
    else:
        args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.out / ".run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            run(cases, bank, args.out)
