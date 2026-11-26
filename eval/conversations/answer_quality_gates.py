"""Checks applied to the draft that the existing publication gate would admit."""
import json
import re

from check import ask, evidence, fingerprint

procedures = evidence.procedures

# Narrow applicability: the same branch described in the cited originals.
# These are pilot annotations. An independent reader still needs to approve them.
A1_RULES = (
    (r"техническ\w+ дирекц", r"конкурс.*(?:договор|документац)|договор.*конкурс",
     ("TECH:7.4:1", "TECH:7.5:1", "TECH:7.6:1", "TECH:7.7:1"),
     (("TECH:7.4:1", "TECH:7.5:1"), ("TECH:7.5:1", "TECH:7.6:1"), ("TECH:7.6:1", "TECH:7.7:1"))),
    (r"дбрцт", r"(?:заранее|ранн\w+ старт|до подписания договора|без договора)",
     ("DB:8.4:1",), (("DB:8.5:1", "DB:8.6:1"),)),
    (r"дбрцт", r"выход\w* из зелено\w+ зон\w*",
     ("DB:16.10:1", "DB:16.11:1"), (("DB:16.11:1", "DB:16.10:1"),)),
    (r"техническ\w+ дирекц", r"пересч[её]т\w*.*ткп|ткп.*пересч[её]т",
     ("TECH:6.1:1", "TECH:6.2:1", "TECH:6.8:1", "TECH:6.4:1"),
     (("TECH:6.1:1", "TECH:6.2:1"), ("TECH:6.8:1", "TECH:6.4:1"))),
)

EXTRACT = """Выдели только написанные в ответе критические предписания, относящиеся к вопросу:
получатель/роль, величина и вид срока, запрет/разрешение, условие/исключение, объект/стадия,
точка отсчета. Для каждого верни дословный span из черновика, короткий нейтральный field
(вопрос о поле, без предложенного значения), и value. Value — короткая единообразная
формулировка на русском, числа цифрами, для рабочих дней пиши «рабочих дней»,
для разрешения «можно» или «нельзя». Для роли укажи именно роль, не всё предложение.
Для условия сохрани отрицание. Не ищи пропуски. JSON {"facts":[{"span":"","field":"","value":""}]}."""

VERIFY = """Для каждого нейтрального вопроса о поле независимо выведи применимое значение
из оригиналов для ситуации сотрудника. Числа цифрами, рабочие дни «рабочих дней»,
разрешение «можно» или «нельзя». Для роли только роль. Возвращай короткую
формулировку, указывая отрицание, если оно существенно. Верни дословный фрагмент
и точную пару doc/ref, которая задаёт именно эту роль, условие или срок.
Если источники не позволяют вывести значение, верни value:null. Тот же порядок полей.
JSON {"facts":[{"value":"","doc":"","ref":"","quote":""}]}."""

REQUIRE = """Прочитай вопрос и оригиналы. Не составляй ответ. Выведи минимальные обязательные
для этого вопроса требования (включая необходимые условия или роли). Для каждого требования
верни допустимые равнозначные claims. Для каждого claim верни его ID и свою точную цитату
из пункта doc/ref. Разные ветви не равнозначны. Для семантически обязательного порядка дай ссылки
на индексы требований, от нуля. Если нужен неизвестный факт или отсутствует опора,
верни status:"unresolved". Используй только условия, которые назвал сотрудник.
JSON {"status":"supported","requirements":[{"alternatives":[{"id":"","doc":"","ref":"","quote":""}]}],"before":[[0,1]]}."""


def publication(draft, verdict, question, sources):
    fact = procedures.requested_fact(question)
    return ask.scoped_result(verdict, draft, sources, fact)[1]


def violations(ids, required, before):
    missing = [key for key in required if key not in ids]
    positions = {key: pos for pos, key in enumerate(ids)}
    misplaced = [(first, second) for first, second in before
                 if first in positions and second in positions and positions[first] >= positions[second]]
    return {"missing": missing, "misplaced": misplaced}


def order_review(question, sources, ids, rules=A1_RULES):
    if not procedures.full_procedure(question):
        return None
    text = procedures.normalized(question)
    for directorate, branch, required, before in rules:
        if not (re.search(directorate, text) and re.search(branch, text)):
            continue
        _, allowed = evidence.options(sources)
        finding = violations(ids, required, before)
        refs = {key for pair in before for key in pair}.union(required)
        # Link the annotation to the registry's verbatim quote and the current original.
        finding["basis"] = {key: fingerprint(allowed[key]["quote"]) for key in sorted(refs & allowed.keys())}
        finding["unavailable"] = sorted(set(required) - allowed.keys())
        return finding
    return None


def quote_matches(item, sources):
    quote = item.get("quote")
    return (isinstance(quote, str) and bool(quote.strip()) and
            any(s["doc"] == item.get("doc") and s["ref"] == item.get("ref")
                and quote in s["text"] for s in sources))


def key(text):
    # Keep negation, values, units and word order. Equivalence beyond typography needs review.
    folded = text.casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", folded))


def fact_review(question, sources, draft, model_json):
    text = draft["message"]
    written = model_json("A5-extract", {"question": question, "draft": text}, [], EXTRACT)["facts"]
    if not isinstance(written, list) or len(written) > 25:
        raise ValueError("Invalid critical facts")
    fields = []
    for item in written:
        if not all(isinstance(item.get(k), str) and key(item[k]) for k in ("span", "field", "value")):
            raise ValueError("Malformed critical fact")
        if item["span"] not in text or key(item["value"]) in key(item["field"]):
            raise ValueError("Critical fact has an invalid span or a leading field")
        fields.append(item["field"])
    if not fields:
        return {"written": written, "checked": [], "mismatches": []}
    checked = model_json("A5-verify", {"question": question, "fields": fields}, sources, VERIFY)["facts"]
    if not isinstance(checked, list) or len(checked) != len(written):
        raise ValueError("Mismatched number of verified fields")
    differences = []
    for claim, actual in zip(written, checked):
        value = actual.get("value")
        if not (isinstance(value, str) and quote_matches(actual, sources) and key(value) == key(claim["value"])):
            differences.append(claim["field"])
    return {"written": written, "checked": checked, "mismatches": differences}


def checklist_review(question, sources, ids, model_json):
    enriched, allowed = evidence.options(sources)
    result = model_json("K1-plan", question, enriched, REQUIRE)
    requirements = result["requirements"]
    if (result["status"] != "supported" or not isinstance(requirements, list)
            or not 1 <= len(requirements) <= 30):
        return {"plan": result, "problem": "Нет подтверждённого перечня обязательных действий."}
    positions = {key: pos for pos, key in enumerate(ids)}
    present = []
    for item in requirements:
        alternatives = item["alternatives"]
        if (not isinstance(alternatives, list) or not alternatives or
                any(a["id"] not in allowed or not quote_matches(a, sources)
                    or (allowed[a["id"]]["doc"], allowed[a["id"]]["ref"]) != (a["doc"], a["ref"])
                    for a in alternatives)):
            raise ValueError("Checklist has a requirement without a supported ID and quote")
        present.append([positions[a["id"]] for a in alternatives if a["id"] in positions])
    missing = [i for i, matches in enumerate(present) if not matches]
    misplaced = []
    for first, second in result["before"]:
        if (type(first) is not int or type(second) is not int or first == second
                or not (0 <= first < len(present) and 0 <= second < len(present))):
            raise ValueError("Invalid precedence in checklist")
        if present[first] and present[second] and min(present[first]) >= min(present[second]):
            misplaced.append([first, second])
    problem = "Отрази подтверждённые обязательные пункты и их порядок." if missing or misplaced else None
    return {"plan": result, "missing": missing, "misplaced": misplaced, "problem": problem}


def assess(variant, question, sources, draft, ids, model_json):
    if variant == "A1" and ids is not None:
        detail = order_review(question, sources, ids)
        problem = ("Проверь наличие обязательного условия и порядок зависимых шагов: "
                   + json.dumps(detail, ensure_ascii=False)
                   if detail and any(detail[k] for k in ("missing", "misplaced", "unavailable")) else None)
        return problem, detail
    if variant == "A5":
        detail = fact_review(question, sources, draft, model_json)
        return ("Перепроверь по оригиналам критические поля: " + ", ".join(detail["mismatches"])
                if detail["mismatches"] else None), detail
    if variant == "K1" and ids is not None:
        detail = checklist_review(question, sources, ids, model_json)
        return detail["problem"], detail
    return None, None
