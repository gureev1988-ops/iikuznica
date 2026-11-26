"""Send identical frozen sources to Gemma and Qwen using the baseline prompt."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

from evaluate import HERE, keyed
sys.path.insert(0, str(HERE.parent))
from run_12x3 import SEARCH_MODELS, fingerprint, load_dataset, run_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args()
    cases = keyed(HERE / "contexts35.jsonl")
    gold = keyed(HERE / "reference35.jsonl")
    questions = keyed(HERE / "questions35.jsonl")
    if set(cases) != set(gold) or set(cases) != set(questions) or len(cases) != 35:
        parser.error("Expected the same 35 development cases in each file")
    for key, case in cases.items():
        if (set(case) != {"id", "question", "sources"} or case["question"] != questions[key]["question"]
                or len(case["sources"]) > 5 or any(set(s) != {"doc", "ref", "section", "text", "title"}
                                                    for s in case["sources"])):
            parser.error(f"Invalid model input for {key}")
    base = load_dataset(HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")
    system = base["system"]
    dataset = fingerprint({"cases": list(cases.values()), "system": system, "models": list(SEARCH_MODELS)})
    if args.check:
        print(f"{len(cases)} frozen questions × {len(SEARCH_MODELS)} models; dataset {dataset}")
        return 0
    if not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("Run in the environment with OPENROUTER_API_KEY")
    args.out.mkdir(exist_ok=True, parents=True)
    with (args.out / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another answer run is active")
        return run_cases(args.out, list(cases.values()), system, gold, dataset, SEARCH_MODELS)


if __name__ == "__main__":
    sys.exit(main())
