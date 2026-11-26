"""Blind inputs and decision policy for the paired Gemma audit."""
import json
import math
import re
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "retrieval"))
from extract import TITLES, clauses
from run_12x3 import SEARCH_MODELS, fingerprint, load_dataset, make_request

GEMMA = SEARCH_MODELS[0]
MODEL = "perplexity/pplx-decider-flash-v1.3:free"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MIN_PROB = 0.5
ASK = {
    "directorate": "Какая дирекция ведёт проект: информационной безопасности (ИБ), техническая или бизнес-решений и цифровой трансформации (ДБРЦТ)?",
    "stage": "На каком этапе находится проект — пресейл, инициация или реализация?",
    "directorate_stage": "Какая дирекция ведёт проект и на каком он этапе?",
    "directorate_request": "Какая дирекция ведёт запрос и что нужно рассчитать — поставку или услуги?",
    "request_kind": "Что требуется рассчитать — поставку, услуги или оба направления?",
    "meeting_kind": "Это внутренняя встреча команды или встреча с заказчиком?",
    "directorate_meeting": "Какая дирекция ведёт проект и встреча будет внутренней или с заказчиком?",
    "other": "Уточни, пожалуйста, какая ситуация или операция имеется в виду?",
}
ROUTES = {
    "answer": "Из вопроса и источников можно вывести единственный полный применимый ответ. Черновик при этом может нуждаться в правке.",
    "clarify": "Выбор правила зависит от неизвестного факта о ситуации сотрудника. Он может его сообщить.",
    "retrieve": "Ситуация понятна, но источников недостаточно, виден фрагмент процесса или отсутствует связанное условие. Стоит поискать в других пунктах.",
    "escalate": "Вопрос по процессу, а достоверное разъяснение должен дать отдел качества. Или повторный поиск не дал достаточного основания.",
    "out_of_scope": "Вопрос находится за пределами описанных корпоративных процессов.",
}
QUESTIONS = {
    "route": {"type": "choice", "instructions": "Выбери следующее действие по запросу сотрудника. Сначала определи применимость правила к его дирекции, этапу и условиям, если они влияют на ответ. Уже указанный факт повторно не спрашивай. Присутствие одного регламента в выдаче само по себе не определяет дирекцию сотрудника. Выбирай по содержанию, независимо от поля action черновика.", "criteria": ROUTES},
    "grounded": {"type": "noul", "instructions": "Каждое утверждение и предлагаемое действие черновика прямо подтверждены подходящим пунктом из sources для ситуации сотрудника? Любая произвольная процедура, неверная роль или неподходящее подразделение означают false."},
    "complete": {"type": "noul", "instructions": "В черновике отражены все существенные для вопроса условия, обязательные согласования и исключения, видимые в источниках, а его действие соответствует тексту? Неполный ответ, способный изменить дальнейшие действия сотрудника, означает false."},
    "missing": {"type": "choice", "instructions": "Если нужна реплика сотрудника, выбери самый короткий подходящий уточняющий вопрос, покрывающий недостающие признаки. Если уточнять нечего, выбери other.", "criteria": ASK},
}
MESSAGES = {
    "escalate": "По доступным регламентам я не могу дать однозначный ответ. Направить запрос в службу качества?",
    "out_of_scope": "Этот вопрос выходит за рамки процессов, описанных в доступных регламентах.",
}
REPAIR = ("Ты отвечаешь сотруднику на русском по предоставленным пунктам. Перепроверь условия и исключения, "
          "способные изменить ответ. Учитывай только подходящую дирекцию, роль и этап. Вывод из фрагмента "
          "процедуры не выдавай за полную процедуру. Убери неподтверждённые советы. Если сотруднику нужно "
          "уточнить ситуацию, задай только один вопрос. При отсутствии основания предложи запрос в службу "
          "качества. Верни только JSON с action (answer, clarify, escalate, out_of_scope), message и sources "
          "(массив объектов doc и ref с точными идентификаторами).")


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def draft(text):
    text = text.strip()
    if text.startswith(chr(96)*3):
        text = re.sub(r"^\x60{3}(?:json)?\s*", "", text, flags=re.I).removesuffix(chr(96)*3).strip()
    obj = json.loads(text)
    if (not isinstance(obj, dict) or obj.get("action") not in ("answer", "clarify", *MESSAGES)
            or not isinstance(obj.get("message"), str) or not obj["message"].strip()
            or not isinstance(obj.get("sources"), list)):
        raise ValueError("Invalid Gemma draft")
    return {k: obj[k] for k in ("action", "message", "sources")}


def inputs():
    base = HERE.parent / "rerank"
    cases = rows(base / "contexts35.jsonl")
    attempts = [r for r in rows(base / "results/attempts.jsonl") if r["model"] == GEMMA and r["ok"]]
    by_id = {r["id"]: r for r in attempts}
    ranks = {r["id"]: r["pool"] for r in rows(base / "rankings.jsonl")}
    if len(cases) != 35 or len(attempts) != 35 or {c["id"] for c in cases} != set(by_id) or set(by_id) != set(ranks):
        raise ValueError("Expected 35 paired questions and completed Gemma answers")
    # Only the common historical system instruction enters this integrity check.
    system = load_dataset(HERE.parent / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")["system"]
    for case in cases:
        row = by_id[case["id"]]
        if fingerprint(make_request(case, GEMMA, system)) != row["request_hash"]:
            raise ValueError("Stored Gemma draft used different sources or settings")
    return cases, by_id, ranks


def reference(p):
    return p["doc"], p["ref"]


def citations(obj, sources):
    """Canonicalize only exact, unambiguous document aliases at the cited clause."""
    canonical = []
    for item in obj["sources"]:
        if isinstance(item, dict):
            doc, ref = item.get("doc"), item.get("ref")
        elif isinstance(item, list) and len(item) == 2:
            doc, ref = item
        else:
            return None
        matches = [p for p in sources if p["ref"] == ref and doc in
                   (p["doc"], p["title"], p["doc"] + " " + p["title"], p["doc"] + ", " + p["title"])]
        if len(matches) != 1:
            return None
        canonical.append({k: matches[0][k] for k in ("doc", "ref")})
    return canonical


def extended(sources, pool, corpus):
    # Keep five originals, parent and nearby sibling clauses, then semantic alternatives.
    groups = [[reference(s) for s in sources]]
    for offset in (0, -1, 1, 2, -2, 3, -3):
        group = []
        for s in sources:
            prefix, _, number = s["ref"].rpartition(".")
            n = int(number) + offset
            ref = prefix if offset == 0 else (f"{prefix}.{n}" if prefix else str(n))
            key = (s["doc"], ref)
            if key in corpus and corpus[key]["section"] == s["section"]:
                group.append(key)
        groups.append(group)
    nearby = list(dict.fromkeys(k for group in groups for k in group))[:28]
    more = [reference(s) for s in sorted(pool, key=lambda s: -s["logit"]) if reference(s) not in nearby][:4]
    return [corpus[k] for k in nearby + more]


def decision_body(question, obj, sources, expanded):
    state = {"employee_question": question, "draft": obj, "sources": sources, "extra_search_already_done": expanded}
    return {"model": MODEL, "state": json.dumps(state, ensure_ascii=False), "questions": QUESTIONS}


def gate(reply, obj, sources):
    answers = reply["answers"]
    if any(answers[k].get("type") != spec["type"] for k, spec in QUESTIONS.items()):
        raise ValueError("Incomplete or refused decision")
    route = answers["route"]["choice"]
    missing = answers["missing"]["choice"]
    if route not in ROUTES or missing not in ASK:
        raise ValueError("Unknown categorical answer")
    values = [answers[k]["noul"] for k in ("grounded", "complete")]
    if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        raise ValueError("Invalid decision probability")
    if route == "answer":
        refs = citations(obj, sources)
        if obj["action"] == "answer" and refs and min(values) >= MIN_PROB:
            return "publish", dict(obj, sources=refs)
        return "revise", None
    if route == "retrieve":
        return route, None
    message = ASK[missing] if route == "clarify" else MESSAGES[route]
    return route, {"action": route, "message": message, "sources": []}


def full_corpus():
    return {reference(s): s for doc, title in TITLES.items() for s in clauses(doc, title)}
