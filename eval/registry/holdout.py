"""Single final run on the private holdout15 partition after freezing the configuration."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import BadZipFile, ZipFile

import compare
from registry import CLAIMS, HERE

BM25 = HERE.parent / "retrieval"
QUESTION_FILE = "holdout15/questions.jsonl"
GOLD_FILE = "holdout15/answer_key.jsonl"


def step(*cmd):
    subprocess.run([str(part) for part in cmd], check=True)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def indexed_rows(archive, member):
    rows = [json.loads(line) for line in archive.read(member).splitlines() if line.strip()]
    if len(rows) != 15 or len({r["id"] for r in rows}) != 15:
        raise ValueError(f"Expected 15 unique rows in {member}")
    return {row["id"]: row for row in rows}


def freeze(out, bank, decisions):
    # Hash only the archive bytes here. Content is first opened after the configuration is fixed.
    files = [CLAIMS, HERE / "versions.json", HERE / "search.py", HERE / "judge.py", HERE / "registry.py",
             HERE / "compare.py", HERE / "answers.py", HERE / "holdout.py", BM25 / "search.mjs", BM25 / "extract.py",
             BM25 / "package-lock.json", HERE.parent / "run_12x3.py",
             HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py",
             HERE.parents[1] / "corpus/glossary.md"]
    frozen = {"files": {p.relative_to(HERE.parents[1]).as_posix(): sha(p) for p in files},
              "private_archive_sha256": sha(bank), "model": compare.MODEL,
              "judge_decisions_sha256": sha(decisions)}
    stamp = out / "freeze.json"
    if stamp.exists() and json.loads(stamp.read_bytes()) != frozen:
        raise ValueError("Configuration or archive changed after the holdout was opened")
    if not stamp.exists():
        compare.save_text(stamp, json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")


def evaluate(bank, out):
    decisions = HERE / "results/judge/decisions.json"
    step(sys.executable, HERE / "registry.py", "--build")
    if not (BM25 / "node_modules/@electric-sql/pglite/package.json").is_file():
        step("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--fetch-retries=1", "--prefix", BM25)
    step(sys.executable, HERE / "judge.py", "--run")
    verdict = json.loads(decisions.read_text(encoding="utf-8"))
    if verdict.get("complete") is not True or verdict.get("registry_sha256") != sha(CLAIMS):
        raise ValueError("The semantic review is incomplete or belongs to a different registry")
    freeze(out, bank, decisions)

    with ZipFile(bank) as archive:
        raw = indexed_rows(archive, QUESTION_FILE)
    questions = {i: row["question"] for i, row in raw.items()}
    if any(not isinstance(q, str) or not q.strip() for q in questions.values()):
        raise ValueError("Empty or malformed holdout question")
    query_file = out / "questions.jsonl"
    compare.save_text(query_file, "".join(json.dumps({"id": key, "question": q}, ensure_ascii=False) + "\n"
                                         for key, q in questions.items()))
    base, alt, clocks, trace = (out / name for name in
                                ("bm25.jsonl", "registry.jsonl", "bm25_timings.jsonl", "trace.jsonl"))
    step("node", BM25 / "search.mjs", "--questions", query_file, "--output", base, "--timings", clocks)
    step(sys.executable, HERE / "search.py", "--questions", query_file, "--baseline", base, "--timings", clocks,
         "--decisions", decisions, "--output", alt, "--trace", trace)
    contexts = {r: compare.load_contexts(p, questions) for r, p in zip(compare.ROUTES, (base, alt))}
    timings = compare.load_timings(trace, contexts)
    public = compare.baseline.load_dataset(HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")
    if set(questions.values()) & {c["question"] for c in public["cases"]}:
        raise ValueError("The private questions overlap the start12 questions")
    requests, identity = compare.paired_requests(contexts, public["system"])
    log = out / "attempts.jsonl"
    history = compare.read_log(log, requests, identity)
    compare.collect(requests, identity, log, history)
    completed = {(row["id"], row["route"]) for row in history if row["ok"]}
    if completed != set(requests):
        print(f"Completed {len(completed)}/30 answers. Repeat the same command to resume.")
        return 2

    # Retrieval and generation have finished for both arms before the private key is opened.
    with ZipFile(bank) as archive:
        gold = indexed_rows(archive, GOLD_FILE)
    if gold.keys() != questions.keys() or any(g["question"] != questions[qid] for qid, g in gold.items()):
        raise ValueError("Private key and questions differ")
    ok = compare.write_reports(out, requests, contexts, gold, history, timings)
    if not ok:
        return 2
    step(sys.executable, HERE / "answers.py", "--run", "--review", out / "review.jsonl",
         "--trace", trace, "--out", out / "answers")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check ZIP member names and executables, leave the holdout sealed")
    mode.add_argument("--run", action="store_true", help="Open the final split, run both routes and score")
    p.add_argument("--bank", type=Path, required=True, help="Private IIkuznitsa_QA_Bank_50.zip")
    p.add_argument("--out", type=Path, default=HERE / "results/holdout15", help="Private, dedicated output directory")
    args = p.parse_args()
    with ZipFile(args.bank) as archive:
        for name in (QUESTION_FILE, GOLD_FILE):
            if archive.namelist().count(name) != 1:
                p.error(f"Expected one archive member {name}")
    if not shutil.which("node") or not shutil.which("npm"):
        p.error("Node.js and npm are required for the baseline")
    if args.check:
        print("Private ZIP members and runtime executables checked. No holdout questions or labels opened.")
        return 0
    if not os.environ.get("OPENROUTER_API_KEY"):
        p.error("Set OPENROUTER_API_KEY before the final run")
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.out / ".holdout.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            p.error("Another holdout run is active")
        return evaluate(args.bank, args.out)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BadZipFile, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
