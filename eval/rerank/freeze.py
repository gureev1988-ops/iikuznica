"""Materialize a selected method's source texts without reading any answer key."""
import argparse
import json
from pathlib import Path
from evaluate import HERE, keyed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    ranking = keyed(HERE / "rankings.jsonl")
    pool = keyed(args.pool)
    if set(ranking) != set(pool):
        raise ValueError("Candidate pool and ranking contain different questions")
    cases = []
    for key, case in pool.items():
        original = {(p["doc"], p["ref"]): p for p in case["sources"]}
        if list(original) != [(s["doc"], s["ref"]) for s in ranking[key]["pool"]]:
            raise ValueError(f"Candidate pool changed for {key}")
        refs = ranking[key]["methods"][args.method]
        cases.append(dict(id=key, question=case["question"], sources=[original[tuple(r)] for r in refs]))
    args.out.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases))


if __name__ == "__main__":
    main()
