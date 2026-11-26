"""Challenger for the topic-aware answer pipeline. No evaluation labels at request time."""
from collections import defaultdict
import json
from functools import lru_cache

from variants import GEMMA, index, make_request, reference
from run import complete, decide
import procedures

REGISTRY = lru_cache(maxsize=1)(index)
SELECT = """Ты отвечаешь сотруднику по действующим процессам. Получишь оригинальные пункты и подготовленные по ним утверждения.
Для однозначного ответа верни {"action":"answer","ids":["..."]}. Выбери минимальный достаточный список утверждений в порядке действий.
Сотрудник получит их текст дословно. Сверь роли, объект, условия, срок и исключения с оригиналом. Каждое выбранное утверждение должно быть применимо.
Для процедур охвати обязательные шаги и обе ветки, если сотрудник не исключил одну из них. Не добавляй действия на смежном этапе.
Если решение зависит от неизвестной дирекции или другого факта, верни {"action":"clarify","ids":[]}.
Для вопроса о процессе без подтверждённого ответа выбери escalate, для постороннего — out_of_scope. Верни только JSON."""
NON_DIRECT = {
    "clarify": "Для выбора применимого правила нужно уточнение.",
    "escalate": "По доступным регламентам нет подтверждённого ответа. Передать вопрос в отдел качества?",
    "out_of_scope": "Это за пределами пресейла и проектной деятельности."
}
SUPPLY_HANDOFF_CLAIMS = ("PRE:1.3:1", "PRE:1.3:5")


def alternatives(sources, pool, corpus):
    """Add alternative directorates from the same BM25 pool; preserve every original."""
    groups = defaultdict(list)
    for row in pool:
        group = "IB" if row["doc"] in ("IB", "PRE") else row["doc"]
        groups[group].append(row)
    if len(groups) < 2:
        return sources
    ordered = {reference(s): s for s in sources}
    for rows in groups.values():
        semantic = sorted(rows, key=lambda r: -r["logit"])
        positions = {reference(row): i for i, row in enumerate(semantic, 1)}
        # Reciprocal rank fusion within a directorate. Corpus size does not give a group extra slots.
        scores = [(1 / (4 + i) + 1 / (4 + positions[reference(row)]), reference(row))
                  for i, row in enumerate(rows, 1)]
        for _, key in sorted(scores, key=lambda pair: -pair[0])[:2]:
            ordered.setdefault(key, corpus[key])
    return list(ordered.values())


def options(sources):
    norms = REGISTRY()
    enriched = []
    allowed = {}
    for source in sources:
        items = []
        for row in norms[reference(source)]:
            if row["quote"] != source["text"]:
                raise ValueError("Registry and original differ")
            allowed[row["id"]] = row
            items.append({"id": row["id"], "text": row["statement"]})
        enriched.append(dict(source, claims=items))
    return enriched, allowed


def render(selection, allowed):
    action, ids = selection["action"], selection["ids"]
    if not isinstance(ids, list) or not all(isinstance(i, str) and i in allowed for i in ids):
        raise ValueError("Unknown or malformed claim ID")
    if action != "answer":
        if ids or action not in NON_DIRECT:
            raise ValueError("Invalid selection")
        return {"action": action, "message": NON_DIRECT[action], "sources": []}
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Expected distinct evidence IDs for an answer")
    refs, statements = {}, []
    for claim_id in ids:
        row = allowed[claim_id]
        key = reference(row)
        citation = refs.setdefault(key, len(refs) + 1)
        statements.append(f"{row['statement']} [{citation}]")
    return {"action": "answer", "message": "\n\n".join(statements),
            "sources": [{"doc": doc, "ref": ref} for doc, ref in refs]}


def generate(question, sources, instruction, *, call=complete, model=GEMMA, observe=None):
    enriched, allowed = options(sources)
    # The vetted fork can supply a first draft. The ordinary judge still reviews the
    # full question and originals. On a repair, allow the model to reconsider it.
    if not instruction and procedures.supply_handoff(question) and all(k in allowed for k in SUPPLY_HANDOFF_CLAIMS):
        selection = {"action": "answer", "ids": list(SUPPLY_HANDOFF_CLAIMS)}
        if observe:
            observe(selection)
        return render(selection, allowed)
    request = make_request({"question": question, "sources": enriched}, model, SELECT + "\n" + instruction)
    row = call("request", request, "evidence-core")
    if not row["ok"]:
        raise RuntimeError(f"{model} HTTP {row['http']}")
    raw = row["text"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3]
    try:
        selection = json.loads(raw)
        if selection.get("action") == "answer" and isinstance(selection.get("ids"), list):
            selection["ids"] = procedures.complete_ids(selection["ids"], allowed, question)
        rendered = render(selection, allowed)
        if observe:
            observe(selection)
        return rendered
    except (AttributeError, KeyError, ValueError, TypeError):
        # An invalid selection has no employee-facing instruction. Let the regular judge
        # choose clarification, escalation or the single allowed repair.
        return {"action": "escalate", "message": NON_DIRECT["escalate"], "sources": []}


def judge(body, *, call=decide):
    questions = {k: v.copy() for k, v in body["questions"].items()}
    questions["route"]["instructions"] += (
        " Сравни положения о том же действии для разных дирекций. Если ответы различаются, "
        "а из вопроса неясно, какая дирекция ведёт проект, выбери clarify и missing=directorate. "
        "Если вопрос явно о процессе, отсутствие конкретной цифры или порядка — повод для escalate."
    )
    questions["complete"]["instructions"] += (
        " Проверь, может ли сотрудник действовать по этой реплике: указаны нужная ветка, роль и условия. "
        "Список похожих правил без выбора применимой ветки недостаточен."
    )
    questions["needed_fact"] = {"type": "choice", "instructions":
        "Оцени именно то, что нужно узнать сотруднику. Тематически похожий пункт не подтверждает "
        "номер, значение, замещение или иной запрошенный реквизит. Если известная ситуация требует "
        "официальных сведений, которых нет, сотрудник не может доопределить их своим уточнением.",
        "criteria": {"supported": "В источниках есть конкретное применимое сведение.",
                     "employee": "Сотрудник может сообщить собственное обстоятельство, меняющее применимое правило.",
                     "company": "Ситуация понятна, но официальное сведение должен предоставить владелец процесса.",
                     "irrelevant": "Вопрос вне проектной области или невозможно определить его предмет."}}
    return call(dict(body, questions=questions))
