"""Keep literal entity identifiers independent of the semantic applicability decision."""
import re
from unicodedata import normalize

WORD = re.compile(r"[^\W_]+(?:[-_/][^\W_]+)*", re.UNICODE)
# Numbers and alphabetic codes require an entity label to distinguish them from amounts
# and ordinary words. Mixed opaque identifiers can occur without a preceding label.
LABEL = re.compile(
    r"\b(?:код|номер|идентификатор|id)(?:ом|у|а)?\s+(?:(?:тестов\w+|учебн\w+)\s+)?"
    r"(?:проект\w*|сделк\w*|договор\w*|контракт\w*|клиент\w*)\s*[:№#–—-]?\s*[«\"“]?([\w/-]+)",
    re.I,
)
NUMBERED = re.compile(r"\b(?:проект\w*|сделк\w*|договор\w*|контракт\w*)\s*(?:[№#]\s*|\s+)(\d[\w/-]*)\b", re.I)
ENTITY_VALUE = re.compile(
    r"\b(?:проект\w*|сделк\w*|контракт\w*|клиент\w*|договор\w*)\b"
    r'\s*(?:с\s+кодом|№|#)?\s*(?:[«"“]([^»"”]{1,100})[»"”]|([\w/-]+))',
    re.I,
)
LOCAL_SCOPE = re.compile(
    r"\b(?:эт(?:от|ого|ому|ом|ой|у)|указанн\w+|данн\w+|конкретн\w+)\s+"
    r"(?:(?:тестов\w+|учебн\w+)\s+)?(?:проект\w*|сделк\w*|контракт\w*|клиент\w*|договор\w*)\b",
    re.I,
)


def normalized(text):
    value = normalize("NFKC", text.replace("№", "#")).casefold()
    return value.translate(str.maketrans({"‑": "-", "‐": "-", "–": "-", "ё": "е"}))


def identifiers(text):
    text = normalized(text)
    literals = {m[1] for pattern in (LABEL, NUMBERED) for m in pattern.finditer(text)}
    for word in WORD.findall(text):
        if re.search(r"(?:[a-zа-я]\d|\d[a-zа-я]|[a-zа-я]-\d|\d-[a-zа-я]{2})", word):
            literals.add(word)
        elif re.fullmatch(r"[0-9a-f]{8,}", word):
            literals.add(word)
    return literals


def answer_entities(answer):
    """In a factual answer ordinary numbers can be findings rather than identifiers."""
    return " ".join(match[0] for match in ENTITY_VALUE.finditer(answer)
                    if match[1] or any(character.isdigit() for character in match[2])
                    or match[2][:1].isupper())


def quoted_entities(text):
    return {normalized(match[1]) for match in ENTITY_VALUE.finditer(text) if match[1]}


def compatible_ids(original, incoming, conditions):
    """Compare case identifiers using employee text and persisted, verified conditions.

    A rewritten search query is excluded: it can drop or even invent an identifier.
    """
    expected = identifiers(original)
    if not expected:
        return True
    incoming_tokens = set(WORD.findall(normalized(incoming)))
    explicit = identifiers(incoming)
    if explicit - expected:
        return False
    actual_tokens = incoming_tokens | {token for value in conditions.values()
                                      for token in WORD.findall(normalized(str(value)))}
    return expected <= actual_tokens


def grounded_entity(match, original, incoming, same_name):
    """Check the judge's entity evidence for a case-specific natural-language name."""
    source = match.get("faq_entity")
    asked = match.get("request_entity")
    if not isinstance(source, str) or not isinstance(asked, str):
        return False
    source, asked = normalized(source.strip()), normalized(asked.strip())
    if not source or not asked or len(source) > 150 or len(asked) > 150:
        return False
    def contains(value, passage):
        return re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", normalized(passage)) is not None
    names = quoted_entities(original)
    if (not contains(source, original) or not contains(asked, incoming)
            or (names and source not in names)):
        return False
    # Pronouns and generic entity labels cannot identify a different employee's case.
    generic = {"проект", "проекта", "проекту", "проекте", "сделка", "сделки", "сделке",
               "договор", "клиент", "этот", "это", "тестовый", "этого", "данного", "указанного"}
    return (not LOCAL_SCOPE.search(source) and not LOCAL_SCOPE.search(asked)
            and bool(set(WORD.findall(source)) - generic) and bool(set(WORD.findall(asked)) - generic)
            and (source == asked or same_name(source, asked)))
