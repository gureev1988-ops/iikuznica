"""Render the project's former smoke-test examples without changing the original mail."""

import re

PROJECT = "«Тестовый проект iishka»"
GAP = ("Какой точный процент минимальной маржинальности применяется к проекту пресейла ИБ "
       "по внутреннему регламенту контроля маржинальности")
TAG = r"(?P<tag>QA-[0-9a-f]{10}|JURY-[0-9]+)(?![\w-])"
PROJECT_WORDS = rf"учебного проекта {TAG}"
END_OF_EXAMPLE = r"(?=$|[.!?\n]|\s+\[\d+\])"
QUESTION = re.compile(rf"(?P<prefix>{GAP}[?!.]?\s+Это только для ){PROJECT_WORDS}{END_OF_EXAMPLE}", re.I)
ANSWER = re.compile(rf"(?P<prefix>(?:Только )?для ){PROJECT_WORDS}"
                    rf"(?P<suffix> минимальный порог маржинальности составляет 18 процентов){END_OF_EXAMPLE}", re.I)


def display_text(text):
    if not isinstance(text, str) or "учебного проекта" not in text.casefold():
        return text
    text = QUESTION.sub(rf"\g<prefix>проекта {PROJECT}", text)
    return ANSWER.sub(rf"\g<prefix>проекта {PROJECT}\g<suffix>", text)


def display_example(value):
    """Project browser payloads after authorization. Regulation text stays verbatim."""
    if isinstance(value, dict):
        if value.get("kind") == "document":
            return value
        return {key: display_example(item) for key, item in value.items()}
    if isinstance(value, list):
        return [display_example(item) for item in value]
    return display_text(value)


CANONICAL_QUESTION = f"Какой минимальный процент маржинальности требуется на пресейле ИБ для проекта {PROJECT}"
CANONICAL_ANSWER = re.compile(
    rf"(?:Только )?для проекта {PROJECT}(?: на пресейле ИБ)? минимальный порог маржинальности составляет 18 процентов",
    re.I,
)


def legacy_faq_tag(question, answer, *originals):
    """Match a sample even when its visible name was used in a later reply or card edit."""
    questions = [text.strip(" .!?").casefold() for text in (*originals, question) if text]
    legacy = next((match for text in questions if (match := QUESTION.fullmatch(text))), None)
    if not legacy:
        return None
    # Each representation must identify this particular old example or its published
    # replacement. Two old case tags never become interchangeable by sharing a prefix.
    allowed = {legacy[0], display_text(legacy[0]).casefold(), CANONICAL_QUESTION.casefold()}
    if any(text not in allowed for text in questions):
        return None
    answer = answer.strip(" .!?")
    reply = ANSWER.fullmatch(answer)
    if (reply and legacy["tag"] == reply["tag"].casefold()) or CANONICAL_ANSWER.fullmatch(answer):
        return legacy["tag"]
    return None


def asks_about_demo_project(question, conditions):
    return any(re.search(r"(?<![\w-])iishka(?![\w-])", str(value), re.I)
               for value in (question, *conditions.values()))


def display_case_text(text, tag):
    """A confirmed example may also be mentioned in free-form expert or judge prose."""
    if not text or not tag:
        return text
    token = rf"(?<![\w-]){re.escape(tag)}(?![\w-])"
    text = re.sub(rf"\bучебного проекта {token}", f"проекта {PROJECT}", text, flags=re.I)
    return re.sub(token, PROJECT, text, flags=re.I)


def display_report_text(text):
    """Old reports store one question, verdict and answer per block, followed by source quotes."""
    blocks = []
    for block in text.split("\n\n"):
        lines = block.split("\n")
        if len(lines) >= 3 and lines[2].startswith("Разъяснение: "):
            answer = lines[2].removeprefix("Разъяснение: ")
            tag = legacy_faq_tag(lines[0], answer)
            if tag:
                lines[:3] = [display_case_text(line, tag) for line in lines[:3]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
