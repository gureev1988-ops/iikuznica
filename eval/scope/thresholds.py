"""Replay stricter thresholds on recorded terminal decisions and the same employee drafts."""
import json
from pathlib import Path

POINTS = (.50, .65, .80, .90, .95)


def action_at(action, decision, cutoff):
    if action != "answer" or not decision or decision["route"]["confidence"] >= cutoff:
        return action
    scores = decision["route"]["probabilities"]
    best = max(("clarify", "retrieve", "escalate", "out_of_scope"), key=scores.__getitem__)
    return "clarify" if best == "clarify" and decision["missing"]["choice"] != "other" else "escalate"


def sweep(records):
    result = []
    for cutoff in POINTS:
        actions = [(action_at(action, decision, cutoff), target) for action, decision, target in records]
        result.append(dict(threshold=cutoff, correct_actions=sum(a == b for a, b in actions),
                           excess_escalations=sum(a == "escalate" and b != a for a, b in actions),
                           n=len(actions)))
    return result


def historical(eval_dir):
    root = Path(eval_dir) / "linked35/results"
    keys = {}
    for line in (Path(eval_dir) / "rerank/reference35.jsonl").read_text().splitlines():
        row = json.loads(line)
        keys[row["id"]] = row["expected_action"]
    records = []
    for line in (root / "outcomes.jsonl").read_text().splitlines():
        row = json.loads(line)
        entry = next(c for c in reversed(row["cost_time"]["links"]["calls"]) if c["stage"].startswith("decision"))
        name = f"{row['id']}-{entry['stage']}-{entry['request_hash']}.json"
        decision = json.loads((root / "calls" / name).read_text())["response"]["answers"]
        records.append((row["answers"]["links_threshold"]["action"], decision, keys[row["id"]]))
    return sweep(records)
