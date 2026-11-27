"""Interpret a reply against the exact option list persisted with a clarification."""
import re
from difflib import SequenceMatcher

UNKNOWN = re.compile(r"\b(?:не знаю|не могу (?:определить|сказать|уточнить)|нет (?:этих |таких )?(?:сведений|данных|информации)|не вижу (?:этого|в карточке)|этого не вижу|мне неизвестно)\b", re.I)
QUESTION = re.compile(
    r"^(?:назовите|назови|расскажите|расскажи|покажите|покажи|подскажите|подскажи|объясните|объясни|"
    r"пришли(?:те)?|открой(?:те)?|найди(?:те)?|(?:мне )?нуж(?:ен|на|но|ны)|как(?:ой|ая|ие|ов|ую)?|"
    r"когда|кто|зачем|сколько|почему|где|можно ли|что (?:делать|нужно))\b", re.I,
)
EXCLUSIONS = {"не", "нет", "кроме", "исключая", "без"}
CHOICE_MODIFIERS = {
    "я", "мы", "у", "нас", "это", "да", "точно", "уже", "сейчас", "пока", "на", "в",
    "стадии", "этапе", "день", "дня", "дней", "неделя", "недели", "недель", "номер",
}


def numbered(prompt, options):
    if not options:
        return prompt
    lines = [f"{index}. {option['text']}" for index, option in enumerate(options, 1)]
    return f"{prompt}\n\n" + "\n".join(lines) + "\n\nМожете ответить просто цифрой."


def words(text):
    return re.findall(r"[а-яёa-z0-9]+", text.casefold().replace("ё", "е"))


def _matches_word(left, right):
    if left == right:
        return True
    length = min(len(left), len(right))
    return (length >= 5 and left[:3] == right[:3]
            and SequenceMatcher(None, left, right).find_longest_match().size >= max(5, length * .65))


def match_options(text, options, *, implicit=False):
    """Implicit routing must establish the topic as well as a possible option match."""
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
    # A word inside an exclusion is not an affirmative choice of that option.
    if QUESTION.search(text) or text.endswith("?"):
        return None
    if EXCLUSIONS.intersection(normalized):
        if not implicit:
            return None
        normalized = [word for word in normalized if word not in EXCLUSIONS]
    # A bare request can mention a choice as a modifier of another subject:
    # «Срок реализации проекта» is about the deadline; «Уже реализуем проект» names the stage.
    subject = next((w for w in normalized if w not in CHOICE_MODIFIERS and not w.isdecimal()), None)
    candidates = []
    for option in options:
        tokens = words(option["text"])
        stems = [w for w in tokens if len(w) > 2]
        acronym = "".join(w[0] for w in stems)
        if implicit and subject and subject != acronym and not any(_matches_word(t, subject) for t in tokens):
            continue
        score = sum(any(_matches_word(w, v) for v in normalized) for w in tokens)
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
