"""Bounded proof for the documented supply-only handoff in IB presales."""
import re

import evidence
import procedures

HANDOFF = ("PRE", "1.3")
SOURCE_LINE = ("Если Заказчик явно обозначил, что ему не потребуются услуги, "
               "BDM формирует ТКП только на поставку и отправляет его АМ.")
TRANSFER = re.compile(r"переда\w*|передат\w*|отправ\w*|направ\w*")
DEADLINE = re.compile(r"срок\w*|когда|как быстро|через сколько")
TIME_VALUE = re.compile(r"\b(?:не позднее|не ранее|в течение|сразу|немедленно|до конца|в день|"
                        r"\d+\s*(?:рабоч\w*\s+|календарн\w*\s+)?(?:час\w*|дн\w*|минут\w*)|"
                        r"на следующий|в тот же день|рабоч\w*\s+д\w*)\b")
# Every requested field has to belong to this transfer. Unknown tails return to the
# ordinary retriever, even when the first clause alone has a certified answer.
FIELD_WORD = re.compile(r"(?:и|а|в|к|за|по|от|для|его|ее|ему|им|нам|мне|я|мы|како\w*|"
                        r"каку\w*|каки\w*|кому|куда|кто|что|нужно|надо|следует|долж\w*|"
                        r"готов\w*|подготов\w*|состав\w*|формир\w*|сформир\w*|переда\w*|"
                        r"направ\w*|отправ\w*|внутри|компани\w*|предложени\w*|ткп|"
                        r"срок\w*|когда|через|сколько|времени|как|быстро|эт\w*|bdm|ам)")
TRANSFER_QUESTION = "В какой срок BDM передаёт АМ ТКП только на поставку при отказе заказчика ИБ от услуг?"


def requested_fields(question):
    if not procedures.supply_handoff(question):
        return None
    chunks = re.split(r"(?<=[.!?])\s+|:\s*", question.strip())
    start = next((i for i, text in enumerate(chunks)
                  if re.match(r"(?:как(?:ое|ую)|кому|кто|куда|что|какой)\b", text, re.I)), None)
    if start is None:
        return None
    circumstances = procedures.normalized(" ".join(chunks[:start]))
    if (not procedures.refused_services(circumstances) or
            re.search(r"кроме|исключени|особ\w* услов|за рубеж|для иностран|\?|\bесли\b", circumstances)):
        return None
    request = procedures.normalized(" ".join(chunks[start:]))
    words = re.findall(r"[\w-]+", request)
    if not words or not all(FIELD_WORD.fullmatch(w) for w in words):
        return None
    if not (TRANSFER.search(request) and re.search(r"кому|куда", request)):
        return None
    if DEADLINE.search(request):
        # A deadline about preparing the offer is a different fact.
        if not re.search(r"(?:срок\w*|когда|как быстро|через сколько)\s+(?:(?:его|ее|надо|нужно|долж\w*|мы|это)\s+){0,3}(?:переда|отправ|направ)", request):
            return None
        return ("handoff", "deadline")
    return ("handoff",)


def _returned_keys(search, question):
    if hasattr(search, "lexical_pool"):
        # Assembly selects every reranked seed from this same authorized pool.
        return {(r["doc"], r["ref"]) for r in search.lexical_pool(question)}
    seeds, pool = search(question)
    return {tuple(k) for k in seeds} | {(r["doc"], r["ref"]) for r in pool}


def verified_parts(question, search, corpus, faq, faq_scope, *, norms=None):
    fields = requested_fields(question)
    if not fields:
        return None
    returned = _returned_keys(search, question)
    p = corpus.get(HANDOFF)
    if not p or HANDOFF not in returned or SOURCE_LINE not in [re.sub(r"^[a-zа-я][.)]\s*", "", s.strip()) for s in p["text"].splitlines()]:
        return None
    claims = (evidence.REGISTRY() if norms is None else norms).get(HANDOFF, [])
    applicable = {r["id"]: r for r in claims if r["quote"] == p["text"]}
    if not all(key in applicable for key in evidence.SUPPLY_HANDOFF_CLAIMS):
        return None
    # The governing paragraph assigns the CRM request before the conditional transfer.
    prerequisite = applicable[evidence.SUPPLY_HANDOFF_CLAIMS[0]]["statement"]
    verified = dict(action="answer", message=prerequisite + " [1]\n\n" + SOURCE_LINE + " [1]",
                    sources=[dict(doc=HANDOFF[0], ref=HANDOFF[1])],
                    offer_quality_request=False)
    first = {"text": "Предложение и получатель", "query": question, "result": verified}
    if len(fields) == 1:
        return [first]
    first["query"] = "Какое предложение готовит BDM и кому его передаёт при отказе заказчика ИБ от услуг?"
    official = faq.lookup(TRANSFER_QUESTION, faq_scope) if faq else None
    if not official:
        # A time limit from a different directorate, party or stage cannot establish this deadline.
        for key in _returned_keys(search, TRANSFER_QUESTION):
            row = corpus[key]
            if row["doc"] not in {"IB", "PRE"}:
                continue
            for line in re.split(r"(?<=[.!?])\s+|\n", row["text"]):
                t = procedures.normalized(line)
                if (TIME_VALUE.search(t) and re.search(r"\bbdm\b.{0,100}(?:переда|направ|отправ).{0,100}\bам\b", t)
                        and "ткп" in t and re.search(r"только на поставк|отказ\w* от услуг", t)):
                    # The general generator and judge still check applicability and exceptions.
                    return [first, {"text": "Срок передачи", "query": TRANSFER_QUESTION, "result": None}]
        official = dict(action="escalate", sources=[], offer_quality_request=True,
                        message="Я не нашёл подтверждённого срока передачи этого ТКП от BDM к АМ. "
                                "Запросить срок в службе качества?")
    return [first, {"text": "Срок передачи", "query": TRANSFER_QUESTION, "result": official}]
