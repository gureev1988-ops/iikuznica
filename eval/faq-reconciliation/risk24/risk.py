"""Measure operational FAQ risk from frozen labels and the existing paired scorer's output."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path


HUMAN = {"review_mail", "keep_faq_flag", "clarify"}
REVIEW = {"review_mail", "keep_faq_flag"}
WIKI_ACTIONS = {"keep_faq_flag", "archive_duplicate", "keep_faq", "link_rule", "clarify"}
MAIL_ACTIONS = {"review_mail", "publish_faq", "link_rule", "clarify"}


def case_events(cases):
    events = {}
    kinds = {}
    for case in cases:
        cid, eid, kind = case["id"], case["event_id"], case["event_kind"]
        if not isinstance(cid, str) or not isinstance(eid, str) or not eid.strip():
            raise ValueError("Every decision needs a case and external event ID")
        if cid in events or kind not in {"wiki", "mail"} or kinds.setdefault(eid, kind) != kind:
            raise ValueError(f"Inconsistent or duplicate case/event: {cid}, {eid}")
        events[cid] = (eid, kind)
    if not events:
        raise ValueError("Empty case set")
    return events


def fraction(n, d):
    return {"numerator": n, "denominator": d, "rate": n / d if d else None}


def score_arm(events, arm):
    rows = arm["cases"]
    if set(rows) != set(events):
        raise ValueError("IDs in paired scores must match all expected cases, including invalid predictions")
    marked = defaultdict(list)
    actions = defaultdict(Counter)
    by_case = {}
    for cid, (event_id, kind) in events.items():
        row = rows[cid]
        if row["event_kind"] != kind:
            raise ValueError(f"Input/source mismatch for {cid}")
        expected = row["expected"]
        actual = row["actual"] or {}
        wanted, seen = expected["action"], actual.get("action")
        valid_for_kind = WIKI_ACTIONS if kind == "wiki" else MAIL_ACTIONS
        if wanted not in valid_for_kind:
            raise ValueError(f"Unexpected reference action for {cid}: {wanted}")

        wiki = kind == "wiki"
        # In the gold rubric, wiki link_rule denotes a prior withdrawal / no active FAQ.
        active_faq = wiki and wanted != "link_rule"
        archiving, duplicate = seen == "archive_duplicate", wanted == "archive_duplicate"
        publication, should_publish = seen == "publish_faq", wanted == "publish_faq"
        conflict = expected["relation"] == "conflict"
        conflict_flag = actual.get("relation") == "conflict"
        human, expected_human = seen in HUMAN, wanted in HUMAN
        required_review, proposed_review = wanted in REVIEW, seen in REVIEW
        tags = {
            "invalid_or_missing": not actual,
            "wrong_event_action": bool(actual) and seen not in valid_for_kind,
            "wrong_priority": bool(actual) and actual["source"] != expected["source"],
            "unjustified_expert_or_rule": bool(actual) and actual["source"] in {"expert", "rule"}
                and actual["source"] != expected["source"],
            "invented_citation": bool(row["citations_unavailable"]),
            "wrong_or_incomplete_evidence": bool(actual) and not row["ok"]["evidence"],
            "active_wiki_faq": active_faq,
            "active_nondeduplicate_wiki_faq": active_faq and not duplicate,
            "true_duplicate": duplicate,
            "predicted_archive": archiving,
            "true_archive": archiving and duplicate,
            "false_archive": archiving and not duplicate,
            "false_archive_active_faq": archiving and active_faq and not duplicate,
            "archive_unsubstantiated": archiving and not row["ok"]["evidence"],
            "missed_duplicate": duplicate and not archiving,
            "predicted_publication": publication,
            "false_publication": publication and not should_publish,
            "publication_unsubstantiated": publication and not row["ok"]["evidence"],
            "true_conflict": conflict,
            "true_positive_conflict": conflict and conflict_flag,
            "missed_conflict": conflict and not conflict_flag,
            "false_conflict": conflict_flag and not conflict,
            "correct_conflict_unsafe_route": conflict and conflict_flag and seen != wanted,
            "conflict_without_required_handoff": conflict and expected_human and not human,
            "gold_review": required_review,
            "substantive_review": proposed_review,
            "clarification": seen == "clarify",
            "gold_clarification": wanted == "clarify",
            "gold_human": expected_human,
            "referred": human,
            "needed_referral": human and expected_human,
            "unnecessary_referral": human and not expected_human,
            "missed_handoff": expected_human and not human,
            "missed_specific_review": required_review and seen != wanted,
            "wrong_human_queue": human and expected_human and seen != wanted,
        }
        flags = [name for name, active in tags.items() if active]
        for name in flags:
            marked[name].append(cid)
        actions[wanted][seen or "invalid"] += 1
        by_case[cid] = {"event_id": event_id, "event_kind": kind, "expected": expected,
                        "actual": row["actual"], "paired_scorer_flags": row["hazards"],
                        "risk_flags": flags}
    def group(name):
        ids = marked[name]
        external_ids = sorted({events[cid][0] for cid in ids})
        return {"count": len(ids), "case_ids": ids, "external_events": len(external_ids),
                "event_ids": external_ids}

    def rate(numerator, denominator):
        return fraction(len(marked[numerator]), len(marked[denominator]))

    return {
        "decisions": len(events), "external_events": len({e for e, _ in events.values()}),
        "prior_format_errors": arm["format_errors"],
        "metrics": {name: group(name) for name in tags},
        "archive_precision": rate("true_archive", "predicted_archive"),
        "archive_recall": rate("true_archive", "true_duplicate"),
        "false_archive_rate_among_active_nondeduplicates":
            rate("false_archive_active_faq", "active_nondeduplicate_wiki_faq"),
        "conflict_detection_recall": rate("true_positive_conflict", "true_conflict"),
        "referral_precision": rate("needed_referral", "referred"),
        "required_handoff_recall": rate("needed_referral", "gold_human"),
        "action_confusion": {wanted: dict(counts) for wanted, counts in sorted(actions.items())},
        "cases": by_case,
    }


def evaluate(inputs, scores):
    events = case_events(inputs["cases"])
    if not scores.get("arms"):
        raise ValueError("No scored arms")
    return {
        "basis": {
            "unit": "One incoming message or one (wiki change, FAQ) pair. Each event_id is counted once in external_events.",
            "active_faq": "Wiki cases with a current FAQ. Gold link_rule after an explicit withdrawal is excluded from the active denominator.",
            "archive": "False archive = proposed archive with any gold action other than archive_duplicate. No real FAQ is modified.",
            "load": "One model-requested human handoff per pair. review_mail and keep_faq_flag are review cards. clarify is reported separately as a clarification contact, not necessarily work performed by a moderator.",
            "misses": "missed_handoff means the gold action requires a human and the model sends no human handoff. missed_specific_review also counts a different human action.",
            "detection": "missed_conflict measures the semantic label. correct_conflict_unsafe_route catches a true detected conflict followed by any action outside the gold policy.",
            "invalid": "Missing/malformed decisions keep their denominator and miss gold-positive actions. They have no observed archive, publication or handoff. Invalid rows never receive correct-action or true-negative credit; format errors are listed. No assumed retry or emergency routing.",
            "interpretation": "Counts describe these synthetic pairs. Model-requested actions are assessed before any runtime safety gate. Card counts do not estimate minutes or per-day incident prevalence.",
        },
        "format_valid": scores["format_valid"],
        "arms": {key: score_arm(events, arm) for key, arm in scores["arms"].items()},
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for key in ("cases", "scores", "out"):
        cli.add_argument("--" + key, required=True, type=Path)
    args = cli.parse_args()
    try:
        with args.cases.open(encoding="utf-8") as f:
            inputs = json.load(f)
        with args.scores.open(encoding="utf-8") as f:
            scores = json.load(f)
        report = evaluate(inputs, scores)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        cli.exit(2, f"Risk scoring failed: {exc}\n")
    print(json.dumps({name: {key: arm["metrics"][key]["count"] for key in
          ("false_archive_active_faq", "missed_conflict", "false_conflict", "false_publication",
           "wrong_priority", "missed_handoff", "substantive_review", "clarification",
           "unnecessary_referral", "invalid_or_missing")}
          for name, arm in report["arms"].items()}, ensure_ascii=False))
    return 0 if report["format_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
