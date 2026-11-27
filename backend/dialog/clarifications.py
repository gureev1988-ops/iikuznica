"""Interpret a reply against the exact option list persisted with a clarification."""
import re
from difflib import SequenceMatcher

UNKNOWN = re.compile(r"\b(?:не знаю|не могу (?:определить|сказать|уточнить)|нет (?:этих |таких )?(?:сведений|данных|информации)|не вижу (?:этого|в карточке)|этого не вижу|мне неизвестно)\b", re.I)
QUESTION = re.compile(r"^(?:как(?:ой|ая|ие|ов|ую)?|когда|кто|зачем|сколько|почему|где|можно ли|что (?:делать|нужно))\b", re.I)


def numbered(prompt, options):
    if not options:
        return prompt
    lines = [f"{index}. {option['text']}" for index, option in enumerate(options, 1)]
    return f"{prompt}\n\n" + "\n".join(lines) + "\n\nМожете ответить просто цифрой."


def words(text):
    return re.findall(r"[а-яёa-z0-9]+", text.casefold().replace("ё", "е"))


def match_options(text, options):
    """Return matches, or None for a free answer. Several matches require clarification."""
    text = text.strip()
    if not options:
        return None
    if text.isdecimal():
        number = int(text.lstrip('0') or '0') if len(text.lstrip('0')) <= 5 else 0
        return options[number-1:number] if 1 <= number <= len(options) else []
    normalized = words(text)
    exact = [o for o in options if words(o["text"]) == normalized]
    if exact:
        return exact
    if UNKNOWN.search(text):
        return [o for o in options if o["id"] == "unknown"] or None
    if QUESTION.search(text):
        return None
    candidates = []
    for option in options:
        tokens = words(option["text"])
        stems = [w for w in tokens if len(w) > 2]
        acronym = "".join(w[0] for w in stems)
        score = sum(any(w == v or (min(len(w), len(v)) >= 5 and
                    SequenceMatcher(None, w, v).find_longest_match().size >= max(5, min(len(w), len(v)) * .65)
                    and w[:3] == v[:3]) for v in normalized) for w in tokens)
        if len(stems) > 1 and acronym in normalized:
            score = len(tokens)
        if score:
            candidates.append((score, option))
    if not candidates:
        return None
    high = max(score for score, _ in candidates)
    return [option for score, option in candidates if score == high]


def offer_decision(text):
    reply = " ".join(words(text))
    if re.fullmatch(r"(?:да(?: (?:передай(?:те)?|отправь(?:те)?|отправляй(?:те)?)(?: (?:вопрос|письмо|обращение))?)?|(?:передай(?:те)?|отправь(?:те)?) (?:письмо|обращение|вопрос))(?: в отдел качества)?", reply):
        return "confirmed"
    if reply in {"нет", "нет не надо", "нет не отправляй", "нет не отправляйте", "не отправляй", "не отправляйте", "нет спасибо", "не надо"}:
        return "declined"
    return None
