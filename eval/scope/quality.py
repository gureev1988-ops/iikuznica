"""Resolve the quality service's current, trusted publication snapshots."""
import json
import unicodedata
from pathlib import Path

RECHECK = "Отправить письмо в отдел качества, чтобы уточнить, какое правило применять?"


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
        if not discrepancy:
            return dict(action="answer", message=source["answer"], sources=refs, discrepancy=discrepancy,
                        offer_quality_request=False)

        rules = [r for r in source.get("sources", ()) if r.get("role") == "discrepancy"
                 and r.get("accessible") is not False and isinstance(r.get("text"), str) and r["text"].strip()
                 and (r.get("id") or (r.get("docId") and r.get("ref") and r.get("revision")))]
        if not rules:
            return dict(action="escalate", message="Источники расходятся. " + RECHECK,
                        sources=refs, offer_quality_request=True)
        rule = "\n".join(f"{r['text']} [{i}]" for i, r in enumerate(rules, 2))
        message = (f"Действующий регламент\n{rule}\n\n"
                   f"Разъяснение отдела качества\n{source['answer']} [1]\n\n{RECHECK}")
        return dict(action="answer", message=message, sources=[*refs, *rules], discrepancy=discrepancy,
                    offer_quality_request=True)


def _has_quality_offer(reply):
    return reply.get("offer_quality_request") is True and (
        reply.get("action") == "escalate" or
        (reply.get("action") == "answer" and (reply.get("discrepancy") or reply.get("coverage_gap")))
    )


def confirm_request(reply, question, request_id, *, confirmed, register, part_index=None):
    """register accepts an idempotency key and stores the confirmed employee question."""
    if confirmed is not True:
        return None
    selected_offer = None
    if reply.get("requests"):
        offers = {i: p for i, p in enumerate(reply["requests"]) if _has_quality_offer(p["result"])}
        if part_index is None and len(offers) == 1:
            part_index = next(iter(offers))
        if type(part_index) is not int or part_index not in offers:
            return None
        question, reply = offers[part_index]["query"], offers[part_index]["result"]
        if len(offers) > 1:
            selected_offer = part_index
    elif reply.get("resolved_question"):
        question = reply["resolved_question"]
    question = reply.get("escalation_question") or question
    if not _has_quality_offer(reply):
        return None
    if not request_id or not question.strip():
        raise ValueError("A request ID and the employee question are required")
    key = request_id if selected_offer is None else f"{request_id}:{selected_offer}"
    return register(key, question)
