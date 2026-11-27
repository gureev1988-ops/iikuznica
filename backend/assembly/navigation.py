"""Resolve a request to open a document against the caller's current catalog."""
import re
from collections import defaultdict

from backend.contracts.engine import AnswerResult, Citation, ClarificationOption


DOCUMENT = re.compile(r"регламент|документ|редакци|положени|инструкци", re.I)
OPEN = re.compile(r"\b(?:где (?:лежит|находится|найти|взять|почитать|искать|посмотреть|регламент|документ)|"
                  r"(?:дай(?:те)?|пришли(?:те)?|нужна?)\s+(?:мне\s+)?ссылк|откр(?:ой|ыть)|"
                  r"покажи(?:те)?\s+(?:мне\s+)?(?:весь |полный |текст )?(?:регламент|документ)|"
                  r"(?:пришли(?:те)?|покажи(?:те)?|отправь(?:те)?|дай(?:те)?)\s+(?:сам |весь |мне |полный )*(?:регламент|документ)|ссылк[ау]\s+на|хочу\s+(?:прочитать|посмотреть))", re.I)
ANCHOR = re.compile(r"\b(?:пункт(?:е|а)?|п\.)\s*(\d+(?:\.\d+)*)", re.I)
NOISE = re.compile(r"^(?:регламент|документ|редакци|пункт|леж|наход|найти|ссыл|откр|покаж|дай|"
                   r"пришл|посмотр|ищ|инструкц|почита|искать|полны|полно|нуж|хочу|текст|цел|"
                   r"актуальн|действую|скача|назван|получ|можно|укаж|спасиб|пожалуйст|где|мне|весь|взять|читать|ищу)")
ALIASES = (("информационной безопасности", "иб"), ("технической дирекции", "тех дирекции"),
           ("техническая дирекция", "тех дирекция"), ("продаж", "пресейл"))


def keywords(text):
    text = text.casefold().replace("ё", "е")
    for name, alias in ALIASES:
        text = text.replace(name, alias)
    tokens = re.findall(r"[а-яa-z]+", text)
    return {t[:5] if len(t) > 4 else t for t in tokens if (len(t) > 2 or t in {"иб"}) and not NOISE.match(t)}


def document_answer(request):
    question = request.question
    selected = request.clarification and request.clarification.get("key") == "document_to_open"
    if not selected and not (OPEN.search(question) and DOCUMENT.search(question)):
        return None
    groups = defaultdict(list)
    for fragment in request.sources.fragments:
        if fragment.kind == "document":
            groups[fragment.document_id].append(fragment)
    if selected:
        choice = request.clarification["value"]
        candidates = [doc for doc, group in groups.items() if group[0].title == choice]
        if choice.casefold() == "не знаю":
            shown = set(request.clarification.get("choices", ()))
            sources = [group[0] for group in groups.values() if group[0].title in shown]
            if sources:
                citations = tuple(Citation(f.document_id, f.revision_id, f.fragment_id, f.text,
                                           "Открыть регламент") for f in sources)
                text = "По известным условиям подходят следующие регламенты:\n" + "\n".join(
                    f"{f.title}, редакция {f.version} [{i}]" for i, f in enumerate(sources, 1))
                return AnswerResult("answer", text + "\n\nПередать вопрос о выборе регламента в Отдел качества?",
                                    citations=citations, escalation_question=question, allow_faq=False,
                                    resolved_question=question)
    else:
        keys = keywords(question)
        # A generic request can refer to the scope established by the previous user turn.
        if not keys or keys <= {"дирек", "деяте", "проек"}:
            context = " ".join(str(v) for v in request.conditions.values())
            if request.history:
                context += " " + next((m.resolved or m.question or m.text for m in reversed(request.history)
                                        if m.role == "assistant"), "")
            known = keywords(context) & {"иб", "дбрцт", "тех", "пресе"}
            keys |= known
        scores = {doc: len(keys & keywords(group[0].title)) for doc, group in groups.items()}
        high = max(scores.values(), default=0)
        candidates = [doc for doc, score in scores.items() if score == high and (not keys or high > 0)]
    if len(candidates) > 1:
        choices = tuple(ClarificationOption(str(doc), groups[doc][0].title,
                                            {"document_to_open": groups[doc][0].title}) for doc in candidates)
        choices += (ClarificationOption("unknown", "Не знаю", {"document_to_open": "не знаю"}),)
        return AnswerResult("clarify", "Какой регламент вы хотите открыть?", options=choices,
                            clarification_key="document_to_open", allow_faq=False, resolved_question=question)
    if not candidates:
        return AnswerResult("escalate", "Не нашёл доступной редакции этого регламента. "
                            "Передать запрос в Отдел качества?", escalation_question=question,
                            allow_faq=False, resolved_question=question)
    group = groups[candidates[0]]
    anchor = ANCHOR.search(question)
    chosen = next((f for f in group if anchor and f.anchor.rstrip(".") == anchor[1]), group[0])
    text = f"{chosen.title}. Действующая редакция {chosen.version}."
    if anchor and chosen.anchor.rstrip(".") != anchor[1]:
        text += f" Пункт {anchor[1]} в этой редакции не найден."
    return AnswerResult("answer", text, citations=(Citation(chosen.document_id, chosen.revision_id,
                        chosen.fragment_id, chosen.text, "Открыть регламент"),),
                        allow_faq=False, resolved_question=question)


def split_document_request(question):
    """Separate an explicit document link from an independently asked factual question."""
    if not (OPEN.search(question) and DOCUMENT.search(question)):
        return None
    parts = re.split(r"(?<=[?.!])\s+(?=[А-ЯЁA-Z])|\s+и\s+(?=(?:кто|когда|как|что|кому|к кому|сколько)\b)",
                     question.strip(), maxsplit=1, flags=re.I)
    if len(parts) == 2 and all(part.strip() for part in parts):
        first = bool(OPEN.search(parts[0]) and DOCUMENT.search(parts[0]))
        second = bool(OPEN.search(parts[1]) and DOCUMENT.search(parts[1]))
        if first != second:
            return tuple(parts) if first else (parts[1], parts[0])
    return None
