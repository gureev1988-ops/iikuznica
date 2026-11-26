"""Resolve a turn against recent topics; only retrieved documents can supply an answer."""
import json
import re

import ask
import conversation
import evidence
import handoff
import procedures
from run import complete
from variants import GEMMA, make_request

RESOLVE = """Помоги поиску по корпоративным регламентам понять последнее сообщение сотрудника.
В context предыдущие ходы. У каждого есть исходный вопрос, его ранее восстановленная формулировка и фактический ответ бота.
Ответ сотруднику здесь не давай. Определи, начинает ли он новую тему (new), продолжает прежнюю
(follow, в том числе возврат), или ссылается на несколько равноправных тем (ambiguous).
Верни JSON {"relation":"new|follow|ambiguous","turn":null,"query":"..."}.
Для new оставь turn=null и исходный вопрос. Для follow укажи номер одного прошлого хода, составь
полный вопрос для нового поиска. Сохрани дирекцию, предмет и подтверждённые сотрудником обстоятельства.
Учитывай, какую часть предыдущего вопроса меняет сотрудник. Не переноси отвергнутое условие, старого исполнителя или ответ на вопрос.
Не добавляй нормативных фактов. Ответ бота помогает понять предмет ссылки. Его содержание нельзя использовать как источник норм.
Для неоднозначной ссылки на разные документы верни ambiguous, turn=null и короткий вопрос с названиями вариантов.
Короткий ответ на конкретное уточнение бота дополняет прежний вопрос. Новый самостоятельный вопрос может прервать уточнение."""


def restore(question, past, model, call):
    history = [(role, t[key]) for t in past for role, key in (("user", "user"), ("assistant", "assistant"))]
    visible = conversation.visible_history(history, question)
    for turn in visible:
        turn["resolved"] = past[turn["turn"] - 1]["resolved"]
    body = make_request({"question": question, "sources": visible}, model, RESOLVE)
    row = call("resolve", body, "dialogue-state")
    if not row["ok"]:
        raise RuntimeError(f"Topic resolution failed (HTTP {row['http']})")
    raw = row["text"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3]
    try:
        obj = json.loads(raw)
        relation, query, anchor = obj["relation"], obj["query"], obj["turn"]
        if relation == "new" and anchor is None:
            return "new", question
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 1200:
            raise ValueError
        if relation == "ambiguous" and anchor is None and query.rstrip().endswith("?"):
            return "ambiguous", query.strip()
        by_id = {t["turn"]: t for t in visible}
        if relation != "follow" or type(anchor) is not int or anchor not in by_id:
            raise ValueError
        context = by_id[anchor]["resolved"] + " " + by_id[anchor]["user"] + " " + question
        dirs = procedures.directories(question) or procedures.directories(context)
        if procedures.directories(query) - dirs or set(re.findall(r"\d+",query)) - set(re.findall(r"\d+",context)):
            raise ValueError
        return "follow", query.strip()
    except (TypeError, KeyError, ValueError):
        return "ambiguous", "О каком процессе или документе ты спрашиваешь?"


def answer(question, search, *, past=(), model=GEMMA, planner=complete, generate_answer=evidence.generate,
           judge=evidence.judge, faq=None, faq_scope=None):
    """past holds the caller's completed turns, scoped to the same authorized dialogue."""
    options = dict(generate_answer=generate_answer, judge=judge, faq=faq, faq_scope=faq_scope)
    query = question.strip()
    if not query:
        raise ValueError("Введи вопрос")
    personal = lambda q: ask.pay_shortcut(conversation.LEADING_CONNECTOR.sub("", q))
    pair = conversation.explicit_pair(query)
    if faq and (hit := faq.lookup(query, faq_scope)):
        return dict(hit, resolved_question=query)
    if pair:
        # The first part could be a follow-up; the second explicitly establishes its own scope.
        parts = []
        for i, q in enumerate(pair):
            response = answer(q, search, past=past if i == 0 else (), model=model, planner=planner, **options)
            parts.append({"text": q, "query": response["resolved_question"], "result": response})
        return dict(conversation.assemble(parts), resolved_question=query)
    if personal(query):
        return dict(ask.reply("out_of_scope", "pay"), resolved_question=query)
    if past:
        previous = past[-1]
        pending = procedures.operation(previous["resolved"])
        # A short literal answer to a known missing field can be applied exactly.
        missing = procedures.pending_facets(pending, previous["resolved"]) if pending else ()
        if (previous["action"] == "clarify" and "?" not in query and len(query.split()) <= 5
                and any(procedures.known(facet, query) for facet in missing)):
            query = previous["resolved"] + " " + query
        else:
            relation, resolved = restore(query, past, model, planner)
            if relation == "ambiguous":
                return dict(action="clarify", message=resolved, sources=[], offer_quality_request=False,
                            resolved_question=query)
            query = resolved
    verified = handoff.verified_parts(query, search, ask.full_corpus(), faq, faq_scope)
    if verified:
        for part in verified:
            if part["result"] is None:
                part["result"] = conversation.answer_explicit(part["query"], search, **options)
        result = conversation.assemble(verified)
    else:
        result = conversation.answer_explicit(query, search, **options)
    return dict(result, resolved_question=query)
