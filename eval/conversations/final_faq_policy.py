"""Compose an applicable FAQ only after the final candidate has reviewed the regulation."""
import re

import conversation
from quality import ApprovedFAQ, canonical

RECHECK = "Повторно уточнить расхождение письмом в Отделе качества?"
STATUSES = frozenset(("match", "conflict", "gap", "pending"))


class RegulationFirstFAQ:
    def __init__(self, publications=(), *, scope=None, corpus=None):
        if not isinstance(publications, (list, tuple)) or (publications and not scope):
            raise ValueError("FAQ snapshots need an explicit authorized scope")
        self.publications = publications
        self.scope = scope
        self.corpus = corpus or {}
        for item in publications:
            if not isinstance(item.get("snapshot"), dict):
                raise ValueError("FAQ entry has no publication snapshot")
            comparison = item.get("comparison", {})
            if comparison.get("status", "pending") not in STATUSES:
                raise ValueError("Unknown FAQ comparison status")
            if comparison.get("status") in ("match", "conflict"):
                basis = comparison.get("basis")
                source = self.corpus.get((basis["doc"], basis["ref"])) if isinstance(basis, dict) else None
                quote = basis.get("quote") if source else None
                if not isinstance(quote, str) or not quote.strip() or quote not in source["text"]:
                    raise ValueError("The FAQ comparison does not cite the pinned regulation")

    def merge(self, question, reply):
        if not self.publications or reply["action"] == "out_of_scope":
            return reply
        if reply.get("requests"):
            parts = [dict(p, result=self.merge(p["query"], p["result"])) for p in reply["requests"]]
            return dict(reply, **conversation.assemble(parts)) if parts != reply["requests"] else reply
        query = reply.get("resolved_question") or question
        matches = []
        for item in self.publications:
            hit = ApprovedFAQ([item]).lookup(query, self.scope)
            if hit:
                matches.append((item, hit["sources"][0]))
        if len(matches) != 1:
            return reply
        item, ref = matches[0]
        status, faq = item.get("comparison", {}).get("status", "pending"), item["snapshot"]["answer"]
        if reply["action"] == "escalate" and status == "gap":
            return dict(reply, action="answer", message=f"Консультация Отдела качества\n{faq} [1]",
                        sources=[ref], offer_quality_request=False)
        if reply["action"] != "answer":
            return reply

        sources = reply["sources"]
        basis = item.get("comparison", {}).get("basis", {})
        relevant = any((s.get("doc"), s.get("ref")) == (basis.get("doc"), basis.get("ref"))
                       for s in sources if s.get("doc"))
        same = canonical(re.sub(r"\[\d+\]", "", reply["message"])) == canonical(faq)
        if same or (status == "match" and relevant):
            message = f'{reply["message"]}\n\nFAQ Отдела качества подтверждает этот вывод [{len(sources)+1}].'
            return dict(reply, message=message, sources=[*sources, ref])
        conflict = status == "conflict" and relevant
        message = (f'Регламент\n{reply["message"]}\n\n'
                   f'FAQ Отдела качества\n{faq} [{len(sources)+1}]')
        if conflict:
            message += f"\n\nПри расхождении руководствуйся регламентом. {RECHECK}"
        else:
            message += "\n\nПри применении нормы руководствуйся регламентом. Соответствие FAQ этому пункту требует проверки."
        return dict(reply, message=message, sources=[*sources, ref],
                    discrepancy="possible_conflict" if conflict else reply.get("discrepancy"),
                    offer_quality_request=conflict or reply.get("offer_quality_request", False))


def check_policy(corpus):
    from quality import confirm_request

    q = "Куда TPS прикладывает финальный расчёт?"
    source = corpus["TECH", "6.1"]
    verified = dict(action="answer", message="TPS прикладывает расчёт в Заявку на пресейл. [1]",
                    sources=[dict(doc="TECH", ref="6.1")], offer_quality_request=False)
    def item(text, status, *, publication="published", scope="fixture", with_basis=True):
        approval = dict(scope=scope, verifiedText=text, mailId="example", actor="Отдел качества", date="2026-11-27")
        comparison = dict(status=status)
        if with_basis:
            comparison["basis"] = dict(doc="TECH", ref="6.1", quote=source["text"])
        return dict(snapshot=dict(question=q, answer=text, approval=approval, status=publication,
                                  answerKind="faq"), comparison=comparison)

    def merge(snapshot, result=verified):
        return RegulationFirstFAQ([snapshot], scope="fixture", corpus=corpus).merge(q, result)

    snapshot = item("В Заявку на согласование договора.", "conflict")
    conflict = merge(snapshot)
    assert conflict["action"] == "answer" and conflict["offer_quality_request"]
    assert conflict["sources"][0] == verified["sources"][0] and conflict["sources"][1]["kind"] == "faq"
    assert conflict["message"].startswith("Регламент\n" + verified["message"])
    assert "руководствуйся регламентом" in conflict["message"]
    stored = []
    def register(key, question):
        stored.append((key, question))

    confirm_request(conflict, q, "fixture", confirmed=False, register=register)
    assert not stored
    confirm_request(conflict, q, "fixture", confirmed=True, register=register)
    assert stored == [("fixture", q)]
    edited = merge(item("Заявка на пресейл.", "match"))
    assert len(edited["sources"]) == 2 and not edited["offer_quality_request"]
    assert merge(item("Спорная запись.", "conflict", publication="withdrawn")) == verified
    assert merge(item("Другая область.", "conflict", scope="other")) == verified
    abstain = dict(action="escalate", message="Уточнить в Отделе качества?", sources=[], offer_quality_request=True)
    assert merge(snapshot, abstain) == abstain
    gap = merge(item("Уточнить у координатора.", "gap", with_basis=False), abstain)
    assert gap["message"].startswith("Консультация") and gap["sources"][0]["kind"] == "faq"
