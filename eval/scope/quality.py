"""Resolve the quality service's current, trusted publication snapshots."""
import json
import unicodedata
from pathlib import Path


def canonical(text):
    return " ".join(unicodedata.normalize("NFC", text).casefold().replace("ё", "е").split()).rstrip("?! .")


class ApprovedFAQ:
    def __init__(self, publications):
        self.publications = publications

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def lookup(self, question, scope):
        if not scope:
            return None
        candidates = []
        for item in self.publications:
            source = item["snapshot"]
            approval = source.get("approval") or {}
            if (source.get("answerKind") != "faq" or source.get("status") != "published"
                    or not source.get("answer") or approval.get("scope") != scope
                    or approval.get("verifiedText") != source["answer"]
                    or not all(approval.get(key) for key in ("mailId", "actor", "date"))):
                continue
            aliases = (source["question"], *item.get("approved_aliases", ()))
            if canonical(question) in {canonical(s) for s in aliases}:
                candidates.append(source)
        if len(candidates) != 1:
            return None
        source = candidates[0]
        proof = source["approval"]
        refs = [dict(kind="faq", id=proof["mailId"], actor=proof["actor"], date=proof["date"],
                     scope=proof["scope"], text=source["answer"])]
        discrepancy = source.get("discrepancy")
        if discrepancy:
            refs.extend(r for r in source.get("sources", ()) if r.get("role") == "discrepancy")
        return dict(action="answer", message=source["answer"], sources=refs, discrepancy=discrepancy,
                    offer_quality_request=False)


def confirm_request(reply, question, request_id, *, confirmed, register, part_index=None):
    """register accepts an idempotency key and stores the confirmed employee question."""
    if confirmed is not True:
        return None
    selected_offer = None
    if "requests" in reply:
        offers = {i: p for i, p in enumerate(reply["requests"]) if p["result"]["action"] == "escalate"
                  and p["result"].get("offer_quality_request") is True}
        if part_index is None and len(offers) == 1:
            part_index = next(iter(offers))
        if type(part_index) is not int or part_index not in offers:
            return None
        question, reply = offers[part_index]["query"], offers[part_index]["result"]
        if len(offers) > 1:
            selected_offer = part_index
    if reply.get("action") != "escalate" or reply.get("offer_quality_request") is not True:
        return None
    if not request_id or not question.strip():
        raise ValueError("A request ID and the employee question are required")
    key = request_id if selected_offer is None else f"{request_id}:{selected_offer}"
    return register(key, question)
