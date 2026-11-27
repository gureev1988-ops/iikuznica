"""Distinguish the value of a correct split from the reliability of automatic splitting."""
import re

from check import ask
import final_parts as original
import final_quality_trial as experiment
from procedures import directories
from variants import make_request

CHOOSE = """Тебе показаны варианты разбора текущей реплики сотрудника.
Выбери вариант, только если обе просьбы можно обработать отдельно при указанных дирекции и контексте.
Результат одной просьбы не даёт права выполнить вторую. Если есть такая зависимость, неясная ссылка,
потеря условия или вместо двух просьб это одна процедура, верни 0. Контекст можно опустить только
для явно личной непрофильной просьбы. Исходные вопросы бота и сотрудника нужны только для понимания
темы. Не давай ответ и не пересказывай текст. Верни только номер готового варианта или 0."""

BOUNDARY = re.compile(
    r"(?<=\?)\s+(?:(?:и\s+ещ[её]|а\s+также|заодно|[аи])\s+)?"
    r"|(?:,\s*|\s+)(?:(?:и\s+ещ[её]|а\s+также|заодно)\s+|[аи]\s+"
    r"(?=(?:кто|кому|кого|как|когда|где|куда|сколько|како\w*|что|подскажи|назови|можно)\b))", re.I)
SCOPE = re.compile(r"^(?:в\s+)?(?:ДБРЦТ|(?:пресейл\s+)?ИБ|Дирекция\s+ИБ|Техническая\s+дирекция)(?=[\s.:])", re.I)
PERSONAL_START = re.compile(r"^(?:как|где|куда|что|какой)\s+мне\b", re.I)
CONTACT = re.compile(r"\bу\s+(?:этого человека|него|неё|нее)\b.*\b(?:номер|телефон)\b", re.I)
ROLE_SUBJECT = re.compile(r"\b(?:отвечает|ответственен)\s+за\s+([^?]+)", re.I)
DEPENDENT = re.compile(r"\b(?:затем|тогда|в таком случае|после этого|на этом основании|дожд\w*)\b", re.I)

# Boundary labels only. No answer, document ID or outcome from the key enters a model request.
ANNOTATED = {
    "F1-01": ("В ДБРЦТ", "кто отвечает за мониторинг технических рисков",
              "какой у этого человека внутренний номер?", (), (), "мониторинг технических рисков"),
    "F1-02": ("ДБРЦТ.", "Как мне выбрать билет на концерт", "кто отвечает за мониторинг технических рисков?",
              (0,), (), ""),
    "F1-03": ("Техническая дирекция.", "Кто отправляет заказчику финальное ТКП",
              "когда ровно закончится рассмотрение нашей заявки?", (), (), ""),
    "F1-04": ("В ДБРЦТ", "кто организует управление рисками", "кто отвечает за мониторинг технических рисков?",
              (), (), ""),
    "F1-05": ("ДБРЦТ.", "Если не знаем, подписан ли протокол ОПЭ, проверь это", "можно ли уже выпускать УПД?",
              (), (0,), ""),
    "F1-06": ("Техническая дирекция.", "Проверь, согласована ли сделка", "отправь ТКП на этом основании.",
              (), (0,), ""),
}


def make_plan(scope, texts, *, unscoped=(), dependency=(), subject=""):
    requests = [{"text": text, "context": [0] if scope and i not in unscoped else [],
                 "history": False, "depends_on": []} for i, text in enumerate(texts)]
    requests[1]["depends_on"] = list(dependency)
    if subject:
        requests[1]["subject"] = {"from": 0, "quote": subject}
    return {"context": [scope] if scope else [], "requests": requests}


def proposals(question):
    if original.CONDITION.search(question) or DEPENDENT.search(question):
        return []
    split = list(BOUNDARY.finditer(question))
    if len(split) != 1:
        return []
    boundary = split[0]
    left = question[:boundary.start()].strip()
    right = question[boundary.end():].strip()
    prefix = SCOPE.match(left)
    scope = ""
    if prefix:
        tail = left[prefix.end():]
        prefix_tail = re.match(r"\s*[.:]?\s*", tail)[0]
        cutoff = prefix.end() + len(prefix_tail)
        scope, left = left[:cutoff].strip(), left[cutoff:].strip()
    if not left or not right:
        return []
    scope_dirs = directories(scope)
    conflict = tuple(i for i, text in enumerate((left, right)) if directories(text) - scope_dirs)
    personal = [i for i, text in enumerate((left, right)) if PERSONAL_START.match(text) and i not in conflict]
    placements = [()] + [(i,) for i in personal] if scope else [()]
    # Plain elision for a contact question. Still a quote from the employee's
    # first request and never a role inferred from an assistant's answer.
    subject = ""
    if CONTACT.search(right) and (match := ROLE_SUBJECT.search(left)):
        span = match[1].strip()
        if 1 <= len(span.split()) <= 8:
            subject = span
    valid = []
    for unscoped in placements:
        # An explicit new directorate is authoritative for its own part.
        if scope and len(conflict) == 2:
            continue
        raw = make_plan(scope, (left, right), unscoped=(*conflict, *unscoped),
                        subject="" if conflict or unscoped else subject)
        try:
            context, parts = original.parse_plan(question, raw)
        except ValueError:
            continue
        valid.append((raw, context, parts, unscoped))
    return valid


def choose(trial, question, past, plans):
    options = [{"id": i, "requests": [original.part_query(ctx, p) for p in parts]}
               for i, (_, ctx, parts, _) in enumerate(plans, 1)]
    request = make_request({"question": {"employee": question,
        "history": [{"user": t["user"], "assistant": t["assistant"]} for t in past[-4:]]},
        "sources": options}, experiment.control.GEMMA, CHOOSE)
    request = dict(experiment.control.settings(request, "A"), max_tokens=32)
    text = trial.calls.generate(request, "F1b-choice")["response"]["text"].strip()
    if not re.fullmatch(r"\d{1,2}", text) or not 0 <= int(text) <= len(plans):
        trial.events.append({"stage": "F1b-invalid-choice"})
        return 0
    return int(text)


def oracle(cases):
    questions = {c["id"]: c["turns"][0]["question"] for c in cases if c["id"] in ANNOTATED}
    result = {}
    for key, (scope, left, right, unscoped, dependency, subject) in ANNOTATED.items():
        question = questions[key]
        raw = make_plan(scope, (left, right), unscoped=unscoped, dependency=dependency, subject=subject)
        context, parts = original.parse_plan(question, raw)
        result[question] = (raw, context, parts, unscoped)
    return result


def candidates(known):
    def run(arm, question, past, row, trial):
        if not original.can_recover(question, row["response"]):
            return None
        if arm == "oracle":
            plan = known.get(question)
            if plan is None:
                return None
        else:
            plans = proposals(question)
            if not plans:
                return None
            trial.events.append({"stage": "F1b-auto-proposals", "count": len(plans)})
            selected = choose(trial, question, past, plans)
            if not selected:
                trial.events.append({"stage": "F1b-no-split"})
                return None
            plan = plans[selected - 1]
        raw, context, parts, unscoped = plan
        return original.recover_parts(context, parts, past, trial, plan=raw,
                                      stage=f"F1b-{arm}-executed", unscoped=unscoped)
    return run


def check(corpus):
    cases = original.panel(corpus)
    known = oracle(cases)
    first_three = [c["turns"][0]["question"] for c in cases if c["id"] in ("F1-01", "F1-02", "F1-03")]
    for question in first_three:
        plans = proposals(question)
        assert plans and any(p[0] == known[question][0] for p in plans)
    for case in (c for c in cases if c["id"] in ("F1-05", "F1-06")):
        question = case["turns"][0]["question"]
        assert not proposals(question) and known[question][2][1]["depends_on"] == [0]
    assert not proposals("ДБРЦТ. Кто отвечает за риски?")
    assert not original.can_recover(first_three[0], ask.reply("escalate", "gap") | {"requests": [{}]})


def main():
    known = oracle(original.panel(None))
    experiment.main(original.panel, candidates(known), arms=("oracle", "auto"), panel_only_arms=("oracle",),
                    description=__doc__, check=check)


if __name__ == "__main__":
    main()
