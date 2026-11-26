"""One entry point for the registry-versus-BM25 experiment on start12."""
import argparse
from collections import Counter
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from registry import atomic_write
from report import export_report

HERE = Path(__file__).resolve().parent
BASE = HERE.parent / "retrieval"
sys.path.insert(0, str(HERE.parent))
from run_12x3 import load_dataset


def script(name, *args):
    subprocess.run([sys.executable, str(HERE / name), *map(str, args)], check=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def baseline_input(out, online):
    frozen = BASE / "contexts.jsonl"
    if not (BASE / "node_modules/@electric-sql/pglite/package.json").is_file() and online and shutil.which("npm"):
        # Some Codex environments permit only OpenRouter. The frozen control remains usable there.
        try:
            subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                            "--fetch-retries=0", "--fetch-timeout=12000", "--prefix", str(BASE)],
                           check=True, timeout=35)
        except (OSError, subprocess.SubprocessError):
            print("Could not install PGlite. Using the frozen BM25 sources.", flush=True)
    if not (shutil.which("node") and (BASE / "node_modules/@electric-sql/pglite/package.json").is_file()):
        print("Frozen BM25 control. Live BM25 lookup time is unavailable.", flush=True)
        return frozen, []
    contexts, timings = out / "baseline_contexts.jsonl", out / "baseline_timings.jsonl"
    subprocess.run(["node", str(BASE / "search.mjs"), "--output", str(contexts), "--timings", str(timings)],
                   check=True)
    if jsonl(contexts) != jsonl(frozen):
        raise ValueError("The live BM25 control differs from the frozen source ranking")
    return contexts, ["--timings", timings]


def score_retrieval(out, baseline, contexts, trace, judged):
    # Read the gold evidence only after search has finished and both rankings are frozen.
    data = load_dataset(HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")
    def keyed(path):
        return {row["id"]: row for row in jsonl(path)}
    base, alt, traces = map(keyed, (baseline, contexts, trace))
    rows = []
    for gold in data["gold"]:
        qid = gold["id"]
        required = {f"{s['doc']} {s['ref']}" for s in gold["evidence"]}
        row = {"id": qid, "expected_action": gold["expected_action"], "judged_registry": judged,
               "registry_route": traces[qid]["route"], "fallback_reason": traces[qid]["fallback_reason"]}
        for label, cases in (("baseline", base), ("registry", alt)):
            refs = [f"{s['doc']} {s['ref']}" for s in cases[qid]["sources"]]
            row[label + "_all"] = required.issubset(refs)
            row[label + "_refs"] = " | ".join(refs)
            row[label + "_missing"] = " | ".join(sorted(required - set(refs)))
        rows.append(row)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=rows[0], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(out / "retrieval_scores.csv", buffer.getvalue())
    summary = {"stage": "judged" if judged else "preflight_without_semantic_judge",
               "questions": len(rows), "baseline_all": sum(r["baseline_all"] for r in rows),
               "registry_all": sum(r["registry_all"] for r in rows),
               "routes": dict(Counter(r["registry_route"] for r in rows)),
               "claims_sha256": digest(HERE / "claims.jsonl"), "baseline_sha256": digest(baseline),
               "alternative_sha256": digest(contexts)}
    atomic_write(out / "retrieval_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"All expected sources in top 5 ({summary['stage']}): "
          f"BM25 {summary['baseline_all']}/{len(rows)}, registry {summary['registry_all']}/{len(rows)}.", flush=True)


def experiment(online):
    results = HERE / "results"
    out = results if online else results / "preflight"
    out.mkdir(parents=True, exist_ok=True)
    script("registry.py", "--build")
    script("judge.py", "--check")
    decisions = out / "judge/decisions.json"
    if online:
        script("judge.py", "--run", "--out", decisions.parent)
    baseline, timings = baseline_input(out, online)
    contexts, trace = out / "contexts.jsonl", out / "retrieval_trace.jsonl"
    script("search.py", "--baseline", baseline, *timings, "--output", contexts, "--trace", trace,
           *(["--decisions", decisions] if online else []))
    score_retrieval(out, baseline, contexts, trace, online)
    args = ["--baseline-contexts", baseline, "--registry-contexts", contexts, "--trace", trace, "--out", out]
    script("compare.py", "--check", *args)
    if online:
        script("compare.py", "--run", *args)
        script("answers.py", "--run", "--review", out / "review.jsonl", "--trace", trace, "--out", out / "answers")
        export_report(out)
    else:
        print("Offline checks completed. No model calls; online answer quality and cost are pending.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Check all four originals and run retrieval without network")
    mode.add_argument("--run", action="store_true", help="Audit the registry, compare the routes and grade the answers")
    args = parser.parse_args()
    if args.run and not os.getenv("OPENROUTER_API_KEY"):
        parser.error("OPENROUTER_API_KEY is required for the online run")
    # Avoid replacing a live run's inputs when two commands target the same checkout.
    parent = HERE / "results"
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / ".pipeline.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another registry experiment is using this checkout")
        experiment(args.run)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except (ValueError, OSError) as error:
        sys.exit(str(error))
