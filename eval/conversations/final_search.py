"""Compare the same Qwen with fixed sources and one constrained search."""
import json
import re

from check import evidence, load_bank
import final_quality_trial as experiment
from dialogue_state import DIRECTORATE_NAMES
from procedures import directories, full_procedure
from procedure_trial import COVER
from variants import make_request

PLAN = """Ты подбираешь один запрос к локальному поиску по действующим корпоративным регламентам.
Сотрудник уже описал ситуацию. По известной дирекции и вопросу найди формулировку для поиска
конкретного недостающего пункта. Сохрани действие, объект, стадию и значимые условия вопроса.
Не предполагай ответ, новую дирекцию, номера пунктов или новые факты проекта.
Если для выбора правила сначала нужны данные сотрудника или поисковый запрос изменить
невозможно, верни пустую строку. Иначе верни один короткий запрос.
Только JSON {"query":""}."""
MAX_CLAUSES = 12
MAX_CHARS = 14000
MORE = 4


def scope_of(query):
    dirs = directories(query)
    scope = dirs - {"PRE"}
    return (DIRECTORATE_NAMES[next(iter(scope))], dirs) if len(scope) == 1 else None


def scoped_query(raw, question, scope):
    query = raw["query"]
    if not isinstance(query, str) or len(query) > 350 or "\n" in query:
        raise ValueError("Expected a single bounded search query")
    query = query.strip()
    if not query:
        return ""
    name, allowed = scope
    if (directories(query) - allowed
            or set(re.findall(r"\d+", query)) - set(re.findall(r"\d+", question))):
        raise ValueError("Search changed known directorate or introduced a number")
    return f"{name}. {query}"


def limited(sources, dirs, slots=MAX_CLAUSES):
    found, seen, chars = [], set(), 0
    for row in sources:
        ref = evidence.reference(row)
        if row["doc"] not in dirs or ref in seen or chars + len(row["text"]) > MAX_CHARS:
            continue
        found.append(row)
        seen.add(ref)
        chars += len(row["text"])
        if len(found) == slots:
            break
    return found


def direct_sources(trial, question, query, fixed, scope):
    request = make_request({"question": question, "resolved": query,
        "sources": [{"doc": s["doc"], "ref": s["ref"], "section": s["section"]} for s in fixed]},
        experiment.control.QWEN, PLAN)
    text = trial.calls.generate(request, "F3-search-plan", "C")["response"]["text"].strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[-1][:-3]
    search_query = scoped_query(json.loads(text), question + " " + query, scope)
    if not search_query:
        trial.events.append({"stage": "F3-search", "query": "", "added": []})
        return fixed
    seeds, pool = trial.lookup(search_query)
    corpus = experiment.ask.full_corpus()
    used = {evidence.reference(s) for s in fixed}
    matches = [tuple(ref) for ref in seeds]
    matches.extend(evidence.reference(r) for r in sorted(pool, key=lambda r: -r["logit"]))
    new = [corpus[ref] for ref in dict.fromkeys(matches)
           if ref not in used and ref in corpus and ref[0] in scope[1]][:MORE]
    chosen = limited([*new, *fixed], scope[1], len(fixed))
    added = [evidence.reference(s) for s in chosen if evidence.reference(s) not in used]
    trial.events.append({"stage": "F3-search", "query": search_query, "added": added})
    return chosen


def candidate(arm, question, past, row, trial):
    if row["response"]["action"] not in {"clarify", "escalate"} or row["response"].get("requests"):
        return None
    if any(e["stage"] == "A1" and e.get("finding") for e in trial.events):
        return None
    gate = next((e for e in reversed(trial.events) if e["stage"] == "gate"), None)
    if gate is None:
        return None
    query = gate["question"]
    scope = scope_of(query)
    answers = gate["verdict"]["answers"]
    if not scope or answers.get("needed_fact", {}).get("choice") == "employee":
        return None
    fixed = limited(gate["sources"], scope[1])
    if not fixed:
        return None
    trial.events.append({"stage": "F3-eligible", "scope": scope[0]})
    chosen = direct_sources(trial, question, query, fixed, scope) if arm == "qwen-search" else fixed
    trial.events.append({"stage": "F3-sources", "refs": [evidence.reference(s) for s in chosen],
                         "characters": sum(len(s["text"]) for s in chosen)})
    instruction = COVER if full_procedure(question) and full_procedure(query) else ""
    draft = evidence.generate(query, chosen, instruction,
                              call=lambda _, body, __: trial.calls.generate(body, "F3-qwen-answer", "C")["response"],
                              model=experiment.control.QWEN, observe=trial.selection)
    if draft["action"] != "answer":
        return None
    result = experiment.reviewed(trial, query, draft, chosen)
    return result if result and result["action"] == "answer" else None


def panel(corpus):
    work = {"P01", "P02", "P03", "P07", "P08", "P09", "P10", "D03", "D05", "C01"}
    questions = (
        ("Техническая дирекция. Договор с подрядчиком уже подписан, а остальные условия допуска мне неизвестны. "
         "Что ещё обязательно проверить перед началом его работ?", "TECH §16.4; keep all start conditions"),
        ("ДБРЦТ. Если менеджер по поставкам не назначен, кто запрашивает отчёт и что именно входит в его содержание?",
         "answer only on DB evidence, identify any unavailable part"),
        ("ДБРЦТ. На проекте вышли из зелёной зоны. Как РП уведомляет руководство и кому направляет план возврата?",
         "DB §16.10–16.11; preserve applicable branch and recipients"),
        ("Пресейл ИБ. Автор заявки не прислал исходные данные. Каким каналом их запросить и с какой даты считать срок закрытия?",
         "PRE §3.4; preserve CRM and the date of creation"),
        ("В Технической дирекции изменили состав решения после подготовки ТКП. Какие согласования повторить до отправки финального документа?",
         "TECH §6.2–6.8; preserve order and applicable conditions"),
    )
    return [c for c in load_bank() if c["id"] in work] + [
        {"id": f"F3-{i:02d}", "turns": [{"question": q, "expect": expected}]}
        for i, (q, expected) in enumerate(questions, 1)]


def check(corpus):
    scope = scope_of("ДБРЦТ. Что делать с отчётом?")
    assert scope == ("ДБРЦТ", {"DB"})
    assert not scope_of("ДБРЦТ или Техническая дирекция")
    assert scoped_query({"query": "правила отчёта"}, "Что делать с отчётом?", scope) == "ДБРЦТ. правила отчёта"
    rows = [corpus["IB", "4.1"], corpus["DB", "16.2"], corpus["DB", "16.2"]]
    assert [evidence.reference(r) for r in limited(rows, scope[1])] == [("DB", "16.2")]
    for query in ("Искать в Дирекции ИБ", "Найти срок в 3 дня"):
        try:
            scoped_query({"query": query}, "Что делать с отчётом?", scope)
        except ValueError:
            continue
        raise AssertionError("Accepted a search that changed the employee's scope")


if __name__ == "__main__":
    experiment.main(panel, candidate, arms=("qwen-fixed", "qwen-search"), description=__doc__, check=check)
