"""Resolve a request to open a document against the caller's current catalog."""
import re
from collections import defaultdict

from backend.contracts.engine import AnswerResult, Citation, ClarificationOption


DOCUMENT = re.compile(r"регламент|документ|редакци|положени|инструкци", re.I)
OPEN = re.compile(r"\b(?:где (?:(?:можно|могу|я могу) )?(?:лежит|находится|найти|взять|почитать|искать|посмотреть|регламент|документ)|"
                  r"(?:дай(?:те)?|пришли(?:те)?|нужна?)\s+(?:мне\s+)?ссылк|откр(?:ой|ыть)|"
                  r"покажи(?:те)?\s+(?:мне\s+)?(?:весь |полный |текст )?(?:регламент|документ)|"
                  r"(?:пришли(?:те)?|покажи(?:те)?|отправь(?:те)?|дай(?:те)?)\s+(?:сам |весь |мне |полный )*(?:регламент|документ)|ссылк[ау]\s+на|хочу\s+(?:прочитать|посмотреть))", re.I)
ANCHOR = re.compile(r"\b(?:пункт(?:е|а)?|п\.)\s*(\d+(?:\.\d+)*)", re.I)
NOISE = re.compile(r"^(?:регламент|документ|редакци|пункт|леж|наход|найти|ссыл|откр|покаж|дай|"
                   r"пришл|посмотр|ищ|инструкц|почита|искать|полны|полно|нуж|хочу|текст|цел|"
                   r"актуальн|действую|скача|назван|получ|можно|укаж|спасиб|пожалуйст|где|мне|весь|взять|читать|ищу)")
ALIASES = (("информационной безопасности", "иб"), ("технической дирекции", "тех дирекции"),
           ("техническая дирекция", "тех дирекция"), ("продаж", "пресейл"))

# Words in a request for a link can describe the act of opening a document or
# its format. All remaining terms must be supported by the same catalog title.
CATALOG_FILLER = re.compile(
    r"\b(?:для (?:наших )?сотрудник(?:ов|а)|у нас|про|для|как|что|могу|его|её|этот|эту|этом|сам|саму|самого|всю|наш|нашей|ваш|есть|"
    r"подскажите|поделитесь|отправьте|здравствуйте|или|верси[а-яё]*|файл[а-яё]*|ознакомиться|"
    r"дирекци[а-яё]*|положени[а-яё]*)\b", re.I)
DIRECTORATE_TERMS = {"иб", "дбрцт", "тех"}
SUBJECT_TERMS = {"проект", "пресейл"}
CASE_ENDING = re.compile(r"(?<=[а-я]{3})(?:ами|ями|ах|ях|ам|ям|ом|ем|ов|ев|ой|ей|ия|ие|ии|ию|а|я|у|ю|е|ы|и|ь)$")
EXCLUDED_TERM = re.compile(
    r"\b(?:не|кроме|исключая|без)(?:\s+(?:в|на|из|для|по|стади[а-я]*|этап[а-я]*|дирекци[а-я]*))*"
    r"\s+(иб|дбрцт|тех|пресейл[ауе]?|проект(?:а|е|ы|ов|ах|ами)?)\b")


def keywords(text):
    text = text.casefold().replace("ё", "е")
    for name, alias in ALIASES:
        text = text.replace(name, alias)
    tokens = re.findall(r"[а-яa-z]+", text)
    return {t[:5] if len(t) > 4 else t for t in tokens if (len(t) > 2 or t in {"иб"}) and not NOISE.match(t)}


def catalog_terms(text):
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"\b(?:диб|информационн[а-я]* безопасност[а-я]*)\b", "иб", text)
    text = re.sub(r"\bтех(?:ническ[а-я]*)?\.?\s+дирекци[а-я]*\b", "тех", text)
    text = re.sub(r"\b(?:департамент|дирекци)[а-я]* бизнес[- ]решений(?: и цифровых технологий)?\b", "дбрцт", text)
    text = re.sub(r"\b(?:предпродаж|продаж|пресейлов)[а-я]*\b", "пресейл", text)
    text = re.sub(r"\b(?:управлени|ведени)[а-я]* проект(?:ами|ом|ов|а)\b|\bпроектн[а-я]*\b", "проект", text)
    excluded = {CASE_ENDING.sub("", match[1]) for match in EXCLUDED_TERM.finditer(text)}
    text = EXCLUDED_TERM.sub(" ", text)
    tokens = re.findall(r"[а-яa-z]+", CATALOG_FILLER.sub(" ", OPEN.sub(" ", text)))
    terms = {CASE_ENDING.sub("", word) for word in tokens
             if (len(word) > 2 or word == "иб") and not NOISE.match(word)}
    return terms, excluded


def matching_documents(request, groups):
    terms, excluded = catalog_terms(request.question)
    titles = {doc: catalog_terms(group[0].title)[0] for doc, group in groups.items()}
    # Current conditions apply even if only one title matches the topic. Infer
    # a missing condition from history only when the catalog still leaves a choice.
    previous = next((m.resolved or m.question or m.text for m in reversed(request.history)
                     if m.role == "assistant"), "")
    candidates = [doc for doc, title in titles.items() if terms - DIRECTORATE_TERMS <= title]
    for key, dimension in (("directorate", DIRECTORATE_TERMS), ("stage", SUBJECT_TERMS)):
        positive, negative = terms & dimension, excluded & dimension
        if not positive and not negative:
            value = request.conditions.get(key)
            if not value and len(candidates) > 1:
                value = previous
            context, exclusions = catalog_terms(str(value or ""))
            positive, negative = context & dimension, exclusions & dimension
        candidates = [doc for doc in candidates if not negative & titles[doc]
                      and (not positive or positive & titles[doc])]
    return candidates


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
        candidates = matching_documents(request, groups)
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
