"""Evaluate frozen rankings. Run with Python 3.9+; no model libraries required."""
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def keyed(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    result = {row["id"]: row for row in rows}
    if len(rows) != len(result):
        raise ValueError(f"Duplicate question id in {path}")
    return result


def main():
    rankings = keyed(HERE / "rankings.jsonl")
    gold = keyed(HERE / "reference35.jsonl")
    first = keyed(HERE.parent / "retrieval/contexts.jsonl")
    if set(rankings) != set(gold) or len(gold) != 35 or not first.keys() <= gold.keys():
        raise ValueError("Expected the 35 working cases including the original 12")
    scored = []
    for key, g in gold.items():
        expected = {(s["doc"], s["ref"]) for s in g["evidence"]}
        saved = rankings[key]
        if key in first:
            control = [[s["doc"], s["ref"]] for s in first[key]["sources"]]
            if saved["methods"]["bm25"] != control:
                raise ValueError(f"Drift in the frozen BM25 control: {key}")
        for method, result in saved["methods"].items():
            found = set(map(tuple, result))
            if len(found) != min(5, len(saved["pool"])):
                raise ValueError(f"Unexpected selected passage count: {key} {method}")
            scored.append(dict(id=key, split="start12" if key in first else "next23",
                               action=g["expected_action"], method=method,
                               all_refs=(expected <= found) if expected else None,
                               found=len(expected & found), expected=len(expected)))
    with (HERE / "coverage.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(scored[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(scored)
    for method in rankings[next(iter(rankings))]["methods"]:
        subset = [r for r in scored if r["method"] == method and r["action"] == "answer"]
        print(method, [(split, sum(r["all_refs"] for r in subset if r["split"] == split))
                       for split in ("start12", "next23")])


if __name__ == "__main__":
    main()
