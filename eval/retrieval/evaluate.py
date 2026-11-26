"""Score frozen retrieval after search, independently of model calls."""
import csv
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from run_12x3 import load_dataset


def evaluate():
    gold = load_dataset(HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")["gold"]
    cases = {c["id"]: c for c in map(json.loads, (HERE / "contexts.jsonl").read_text().splitlines())}
    results = []
    for g in gold:
        expected = list(dict.fromkeys((e["doc"], e["ref"]) for e in g["evidence"]))
        found = [(s["doc"], s["ref"]) for s in cases[g["id"]]["sources"]]
        hits = [key for key in expected if key in found]
        def display(keys):
            return " | ".join(f"{doc} {ref}" for doc, ref in keys)
        results.append(dict(id=g["id"], action=g["expected_action"], top5=display(found),
                            expected=display(expected), missing=display(k for k in expected if k not in hits),
                            hit_count=len(hits), expected_count=len(expected), all_found=hits == expected))
    return results


if __name__ == "__main__":
    rows = evaluate()
    with (HERE / "retrieval_scores.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    for action in ("answer", "clarify", "escalate"):
        subset = [r for r in rows if r["action"] == action]
        print(action, "all reference clauses:", sum(r["all_found"] for r in subset), "/", len(subset))
