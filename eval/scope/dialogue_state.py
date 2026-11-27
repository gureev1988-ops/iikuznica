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

PROCEDURE_CONTEXT = (
    "\nЕсли в истории есть полная процедура без текста ответа, используй вопрос сотрудника "
    "и ранее восстановленную тему. Уточнение участника или условия само по себе не меняет объект действия. "
    "Укажи конкретный объект и известную дирекцию выбранной темы. "
    "Различай исходный процесс и событие в последней реплике. Условия и стадию бери из последней реплики, "
    "а вопрос формулируй только о том действии, сроке или условии, о котором сотрудник спрашивает сейчас. "
    "Для короткого follow укажи также subject: одну дословную фразу до восьми слов из исходной "
    "реплики выбранного хода, которая называет нужный объект. Если объект уже назван, верни subject пустым. "
    "В subject оставь только объект, без прошлых просьб или шагов."
)
RETRY_PROCEDURE_CONTEXT = (
    "\nПерепроверь связь с единственным предложенным прошлым ходом. Короткое уточнение роли "
    "или ссылка «туда», «там» могут продолжать его, даже если прежний ответ отсутствует. "
    "Если это продолжение, в subject скопируй из прошлого вопроса короткое название объекта, "
    "к которому относится текущее действие. Не копируй участника из последней реплики. "
    "Если вопрос самостоятельный или связь неясна, верни new или ambiguous."
)
REPLY_REFERENCE = re.compile(
    r"\b(?:в (?:тво[её]м|предыдущем) ответе|ты (?:сказал|написал|упомянул)\w*|"
    r"(?:шаг|пункт)\w*\s+(?:№\s*)?\d+|(?:перв\w*|втор\w*|треть\w*|последн\w*)\s+(?:шаг|пункт)\w*)\b", re.I
)
ROLE_FOLLOWUP = re.compile(r"^а\s+(?:кто|кого|кому|что|како\w*)\s+именно\b", re.I)
DIRECTORATE_NAMES = {"IB": "Дирекция ИБ", "DB": "ДБРЦТ", "TECH": "Техническая дирекция"}


def _subject_from(subject, text):
    return (isinstance(subject, str) and 1 <= len(subject.split()) <= 8
            and not re.search(r"[.!?;\n]", subject) and subject.strip().casefold() in text.casefold())


def _focus_followup(question, query, topic, subject):
    if subject is not None:
        if not isinstance(subject, str):
            raise ValueError("Invalid follow-up subject")
        subject = subject.strip()
        if subject:
            if not _subject_from(subject, topic):
                raise ValueError("Follow-up subject must be a short quote from the employee's topic")
            return f"Контекст — {subject}. {question}"
        return question
    return query


def _resolution(question, sources, model, call, instruction):
    body = make_request({"question": question, "sources": sources}, model, instruction)
    row = call("resolve", body, "dialogue-state")
    if not row["ok"]:
        raise RuntimeError(f"Topic resolution failed (HTTP {row['http']})")
    raw = row["text"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3]
    decision = json.loads(raw)
    if not isinstance(decision, dict):
        raise ValueError("Invalid topic resolution")
    return decision


def _retry_topic(decision, question, visible, procedure_context, past):
    if not procedure_context or procedures.full_procedure(question):
        return None
    relation, anchor = decision.get("relation"), decision.get("turn")
    if relation == "follow" and type(anchor) is int and anchor in procedure_context:
        topic = next(turn for turn in visible if turn["turn"] == anchor)
        subject = decision.get("subject")
        if _subject_from(subject, question) and not _subject_from(subject, topic["user"]):
            return topic
    if (relation == "new" and anchor is None and procedure_context == {visible[-1]["turn"]}
            and past[-1]["action"] == "escalate" and len(question.split()) <= 16
            and not procedures.directories(question) and ROLE_FOLLOWUP.match(question)):
        return visible[-1]
    return None


def restore(question, past, model, call):
    history = [(role, t[key]) for t in past for role, key in (("user", "user"), ("assistant", "assistant"))]
    visible = conversation.visible_history(history, question)
    procedure_context = set()
    refers_to_reply = REPLY_REFERENCE.search(question)
    for turn in visible:
        prior = past[turn["turn"] - 1]
        turn["resolved"] = prior["resolved"]
        if (not refers_to_reply and prior["action"] in ("answer", "escalate")
                and procedures.full_procedure(turn["user"]) and procedures.full_procedure(turn["resolved"])):
            # The employee's topic survives a blocked draft or an offer to contact Quality.
            turn["assistant"] = ""
            procedure_context.add(turn["turn"])
    instruction = RESOLVE + PROCEDURE_CONTEXT if procedure_context else RESOLVE
    try:
        obj = _resolution(question, visible, model, call, instruction)
        topic = _retry_topic(obj, question, visible, procedure_context, past)
        if topic is not None:
            try:
                corrected = _resolution(question, [topic], model, call, instruction + RETRY_PROCEDURE_CONTEXT)
            except (RuntimeError, ValueError):
                corrected = {}
            if (corrected.get("relation") == "follow" and type(corrected.get("turn")) is int
                    and corrected["turn"] == topic["turn"] and _subject_from(corrected.get("subject"), topic["user"])):
                obj = corrected
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
        focused = anchor in procedure_context and not procedures.full_procedure(question)
        if focused:
            query = _focus_followup(question, query, by_id[anchor]["user"], obj.get("subject"))
        query_dirs = procedures.directories(query)
        if query_dirs - dirs or set(re.findall(r"\d+", query)) - set(re.findall(r"\d+", context)):
            raise ValueError
        known_dirs = dirs - {"PRE"}
        if (anchor in procedure_context and not query_dirs and len(known_dirs) == 1
                and not re.search(r"\b\w*дирекц", question, re.I)):
            query = DIRECTORATE_NAMES[next(iter(known_dirs))] + ". " + query.strip()
        if focused and (procedures.full_procedure(query) or len(query.split()) > len(question.split()) + 12):
            raise ValueError("Follow-up reintroduced a full procedure")
        return "follow", query.strip()
    except (TypeError, KeyError, ValueError):
        return "ambiguous", "О каком процессе или документе ты спрашиваешь?"


def answer(question, search, *, past=(), model=GEMMA, planner=complete, generate_answer=evidence.generate,
           judge=evidence.judge, faq=None, faq_scope=None, experiment=None):
    """past holds the caller's completed turns, scoped to the same authorized dialogue."""
    options = dict(generate_answer=generate_answer, judge=judge, faq=faq, faq_scope=faq_scope)
    query = question.strip()
    if not query:
        raise ValueError("Введи вопрос")
    personal = lambda q: ask.pay_shortcut(conversation.LEADING_CONNECTOR.sub("", q))
    pair = conversation.explicit_pair(query)
    if not past and faq and (hit := faq.lookup(query, faq_scope)):
        return dict(hit, resolved_question=query)
    if experiment:
        def respond(part, history):
            return answer(part, search, past=history, model=model, planner=planner, **options)

        split = experiment.split(query, past, respond)
        if split is not None:
            return dict(split, resolved_question=query)
    if pair:
        # The first part could be a follow-up; the second explicitly establishes its own scope.
        parts = []
        for i, q in enumerate(pair):
            response = answer(q, search, past=past if i == 0 else (), model=model, planner=planner,
                              experiment=experiment, **options)
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
            if experiment:
                relation, resolved = experiment.restore(query, past, relation, resolved)
            if relation == "ambiguous":
                return dict(action="clarify", message=resolved, sources=[], offer_quality_request=False,
                            resolved_question=query)
            query = resolved
    if past and faq and (hit := faq.lookup(query, faq_scope)):
        return dict(hit, resolved_question=query)
    verified = handoff.verified_parts(query, search, ask.full_corpus(), faq, faq_scope)
    if verified:
        for part in verified:
            if part["result"] is None:
                part["result"] = conversation.answer_explicit(part["query"], search, **options)
        result = conversation.assemble(verified)
    else:
        result = conversation.answer_explicit(query, search, **options)
    return dict(result, resolved_question=query)
