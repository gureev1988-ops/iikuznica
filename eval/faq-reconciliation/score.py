"""Score two blind FAQ reconciliation runs against the same frozen case set (stdlib only)."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

RELATIONS = {"conflict", "covered", "supplement", "irrelevant", "uncertain"}
ACTIONS = {"review_mail", "publish_faq", "link_rule", "keep_faq_flag", "archive_duplicate", "keep_faq", "clarify"}
SOURCES = {"expert", "rule", "none"}
LABELS = {"relation": RELATIONS, "action": ACTIONS, "source": SOURCES}
MEASURES = ("relation", "action", "source", "routing", "evidence", "exact")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key {key!r}")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_keys)


def check_string(value, where):
    require(isinstance(value, str) and value.strip(), f"{where}: expected nonempty string")


def string_set(value, where):
    require(isinstance(value, list), f"{where}: expected array")
    for item in value:
        check_string(item, where)
    require(len(value) == len(set(value)), f"{where}: duplicate entries")
    return set(value)


def keyed(rows, where):
    require(isinstance(rows, list), f"{where}: expected array")
    result = {}
    for row in rows:
        require(isinstance(row, dict), f"{where}: expected object")
        item_id = row.get("id")
        check_string(item_id, f"{where}.id")
        require(item_id not in result, f"{where}: duplicate identifier {item_id!r}")
        result[item_id] = row
    return result


def check_labels(row, where):
    for field, options in LABELS.items():
        require(isinstance(row.get(field), str) and row[field] in options, f"{where}: invalid {field}")
    check_string(row.get("reason"), f"{where}.reason")


def load_benchmark(cases_path, gold_path):
    payload, gold_payload = read(cases_path), read(gold_path)
    require(isinstance(payload, dict) and isinstance(gold_payload, dict), "cases and gold must be objects")
    clauses = set()
    for doc_id, doc in keyed(payload.get("documents"), "documents").items():
        for ref, row in keyed(doc.get("clauses"), f"{doc_id}.clauses").items():
            require(ref not in clauses, f"clause id collision {ref}")
            check_string(row.get("text"), f"{ref}.text")
            clauses.add(ref)
    cases = keyed(payload.get("cases"), "cases")
    gold = keyed(gold_payload.get("cases"), "gold")
    require(bool(cases) and cases.keys() == gold.keys(), "cases and gold must have the same nonempty set of identifiers")
    pools = {}
    for cid, row in cases.items():
        require(row.get("event_kind") in ("mail", "wiki"), f"{cid}: invalid event_kind")
        for k in ("context", "event"):
            check_string(row.get(k), f"{cid}.{k}")
        refs = string_set(row.get("candidate_clause_ids"), f"{cid}.candidate_clause_ids")
        require(refs <= clauses, f"{cid}: candidates absent from documents: {sorted(refs - clauses)}")
        evidence = keyed(row.get("evidence"), f"{cid}.evidence")
        require(not refs.intersection(evidence), f"{cid}: candidate id collides with evidence")
        for ref, entry in evidence.items():
            check_string(entry.get("text"), f"{cid}.{ref}.text")
        pool = refs.union(evidence)
        pools[cid] = pool
        expected = gold[cid]
        check_labels(expected, cid)
        required = string_set(expected.get("required_citations"), f"{cid}.required_citations")
        allowed = required | string_set(expected.get("allowed_citations", []), f"{cid}.allowed_citations")
        require(allowed <= pool, f"{cid}: gold includes unavailable sources: {sorted(allowed - pool)}")
    return cases, gold, pools


def predictions(path, cases):
    errors, records, blocked = [], {}, set()
    try:
        payload = read(path)
        require(isinstance(payload, dict) and set(payload) == {"cases"}, "expected one top-level key 'cases'")
        require(isinstance(payload["cases"], list), "expected an array of decisions")
    except (OSError, ValueError, UnicodeError) as exc:
        return {}, [str(exc)]

    for index, row in enumerate(payload["cases"]):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            errors.append(f"row {index}: missing string id")
            continue
        cid = row["id"]
        if cid not in cases:
            errors.append(f"unexpected case {cid}")
            continue
        if cid in records or cid in blocked:
            blocked.add(cid)
            records.pop(cid, None)
            errors.append(f"{cid}: duplicate decision")
            continue
        try:
            require(set(row) == {"id", "relation", "action", "source", "citations", "reason"},
                    f"{cid}: wrong decision fields")
            check_labels(row, cid)
            string_set(row.get("citations"), f"{cid}.citations")
            records[cid] = row
        except ValueError as exc:
            blocked.add(cid)
            errors.append(str(exc))
    for cid in sorted(cases.keys() - records.keys() - blocked):
        errors.append(f"{cid}: missing decision")
    return records, errors


def dangerous(actual, expected):
    if actual is None:
        return []
    failures = []
    action, wanted = actual["action"], expected["action"]
    if action == "publish_faq" and wanted != action:
        failures.append("unwarranted_publication")
    if action == "archive_duplicate" and wanted != action:
        failures.append("unwarranted_archive")
    if actual["source"] == "expert" and expected["source"] != "expert":
        failures.append("inapplicable_expert_priority")
    if actual["source"] == "rule" and expected["source"] == "expert":
        failures.append("rule_over_applicable_expert")
    if actual["source"] == "rule" and expected["source"] == "none":
        failures.append("inapplicable_rule")
    return failures


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def score(path, cases, gold, pools):
    records, errors = predictions(path, cases)
    details = {}
    for cid, case in cases.items():
        actual, expected = records.get(cid), gold[cid]
        required = set(expected["required_citations"])
        allowed = required.union(expected.get("allowed_citations", []))
        supplied = set(actual["citations"]) if actual else set()
        matches = {k: actual is not None and actual[k] == expected[k] for k in LABELS}
        matches["routing"] = matches["action"] and matches["source"]
        matches["evidence"] = actual is not None and required <= supplied <= allowed
        matches["exact"] = all(matches.values())
        details[cid] = {
            "event_kind": case["event_kind"], "expected": {k: expected[k] for k in LABELS},
            "actual": {k: actual[k] for k in LABELS} if actual else None,
            "ok": matches, "required_missing": sorted(required - supplied),
            "citations_unjustified": sorted(supplied - allowed),
            "citations_unavailable": sorted(supplied - pools[cid]),
            "hazards": dangerous(actual, expected),
        }

    rows = list(details.values())
    tp = sum(r["expected"]["relation"] == "conflict" and r["ok"]["relation"] for r in rows)
    fp = sum(r["expected"]["relation"] != "conflict" and (r["actual"] or {}).get("relation") == "conflict" for r in rows)
    fn = sum(r["expected"]["relation"] == "conflict" for r in rows) - tp
    tn = sum(r["expected"]["relation"] != "conflict" and r["actual"] is not None
             and r["actual"]["relation"] != "conflict" for r in rows)
    unclassified_negative = len(rows) - tp - fp - fn - tn

    def counts(sample):
        return {"n": len(sample), **{k: sum(row["ok"][k] for row in sample) for k in MEASURES}}

    matrix = {r: dict(Counter((row["actual"] or {}).get("relation", "invalid")
                              for row in rows if row["expected"]["relation"] == r)) for r in sorted(RELATIONS)}
    hazards = Counter(h for row in rows for h in row["hazards"])
    return {
        "totals": counts(rows), "by_event": {
            kind: counts([r for r in rows if r["event_kind"] == kind]) for kind in ("mail", "wiki")},
        "conflict": {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "unclassified_negative": unclassified_negative,
                     "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn)},
        "relation_confusion": matrix, "hazards": dict(hazards), "hazard_cases": sum(bool(r["hazards"]) for r in rows),
        "format_errors": errors, "cases": details
    }


def pair(a, b):
    rows = a["cases"]
    result = {}
    for m in MEASURES:
        outcome = {"only_a": [], "only_b": [], "both_pass": [], "both_fail": []}
        for key, first in rows.items():
            left, right = first["ok"][m], b["cases"][key]["ok"][m]
            bucket = ("both_pass" if right else "only_a") if left else ("only_b" if right else "both_fail")
            outcome[bucket].append(key)
        result[m] = outcome
    return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for flag in ("cases", "gold", "a", "b", "out"):
        cli.add_argument("--" + flag, required=True, type=Path)
    args = cli.parse_args()
    try:
        cases, gold, pools = load_benchmark(args.cases, args.gold)
        a = score(args.a, cases, gold, pools)
        b = score(args.b, cases, gold, pools)
        valid = not a["format_errors"] and not b["format_errors"]
        report = {
            "format_valid": valid,
            "definitions": {
                "source": "mail: authority for original recipient; wiki: authority for repeat FAQ question",
                "routing": "action and source both match the independent labels",
                "evidence": "includes every required ID and only required/allowed IDs from the candidate pool",
                "exact": "relation, action, source and evidence all correct on the same case",
                "conflict": "only the conflict class is positive; missing predictions count as missed positives",
                "hazards": "unwarranted publish/archive, expert or rule applied without authority, or rule overriding applicable expert; counts can overlap",
                "reason": "must be nonempty; semantic quality of free-text rationale requires independent inspection",
                "format": "missing, duplicate, malformed or unknown decisions invalidate the run; affected expected cases count as failed",
            },
            "arms": {"a": a, "b": b}, "paired": pair(a, b),
        }
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"scoring error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"format_valid": valid, "a": a["totals"], "b": b["totals"]}, ensure_ascii=False))
    return 0 if valid else 2


if __name__ == "__main__":
    sys.exit(main())
