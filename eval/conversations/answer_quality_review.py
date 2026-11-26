"""Prepare private blinded packets and compare the visible replies of two full runs."""
import argparse
from collections import Counter
from pathlib import Path
import json
import secrets

from check import ask, fingerprint
from reasoning_trial import save


def visible(reply):
    return {key: reply.get(key) for key in ("action", "message", "sources", "requests", "offer_quality_request")}


def source_keys(sources):
    return {(s["doc"], s["ref"]) for s in sources if "doc" in s and "ref" in s}


def paired(control, candidate, out):
    arms = {"control": {r["turn"]: r for r in control},
            "candidate": {r["turn"]: r for r in candidate}}
    if arms["control"].keys() != arms["candidate"].keys() or len(arms["control"]) != len(control):
        raise ValueError("Both runs must contain the same unique turns")
    corpus = ask.full_corpus()
    details, pairs, review = Counter(), [], []
    changed_dialogues = set()
    for turn, a in arms["control"].items():
        b = arms["candidate"][turn]
        if a["question"] != b["question"]:
            raise ValueError(f"Different employee input for {turn}")
        left, right = visible(a["response"]), visible(b["response"])
        details[f'{left["action"]} -> {right["action"]}'] += 1
        first = None
        if a["past"] != b["past"]:
            first = "history"
        elif a["response"]["resolved_question"] != b["response"]["resolved_question"]:
            first = "resolution"
        elif a.get("diagnostics", {}).get("search") != b.get("diagnostics", {}).get("search"):
            first = "search"
        elif [e["ids"] for e in a["events"] if e["stage"] == "gate" and e["attempt"] == 0] != [
                e["ids"] for e in b["events"] if e["stage"] == "gate" and e["attempt"] == 0]:
            first = "selection"
        elif any(e.get("finding") for e in b["events"]):
            first = "publication gate"
        elif left != right:
            first = "publication"
        if first:
            changed_dialogues.add(turn.split(".")[0])
            pairs.append(dict(turn=turn, first_difference=first, changed_reply=left != right,
                              control_action=left["action"], candidate_action=right["action"],
                              control_technical=bool(a.get("error")), candidate_technical=bool(b.get("error"))))
        for label, row in (("control", a), ("candidate", b)):
            available = set(source_keys(row["response"]["sources"]))
            for event in row["events"]:
                if event["stage"] == "gate":
                    available.update(source_keys(event["sources"]))
            review.append(dict(turn=turn, label=label, employee=row["question"],
                               history=[{"employee": h["user"], "reply": h["assistant"]} for h in row["past"]],
                               reply=visible(row["response"]), originals=[corpus[k] for k in sorted(available) if k in corpus]))
    manifest = fingerprint([control, candidate])
    marker = out / "manifest.json"
    if marker.exists() and json.loads(marker.read_text()) != manifest:
        raise ValueError("Run inputs changed; choose a fresh review directory")
    save(out / "pair-diagnostics.json", {"turns": len(control), "transitions": details,
         "visibly_changed": sum(p["changed_reply"] for p in pairs), "changed_dialogues": sorted(changed_dialogues),
         "differences": pairs})
    if not marker.exists():
        secrets.SystemRandom().shuffle(review)
        key = {str(i): [r.pop("turn"), r.pop("label")] for i, r in enumerate(review, 1)}
        save(out / "blind-packet.json", [{"id": str(i), **r} for i, r in enumerate(review, 1)])
        save(out / "unblind-key.json", key)
        save(marker, manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    paired(json.loads(args.control.read_text()), json.loads(args.candidate.read_text()), args.out)
