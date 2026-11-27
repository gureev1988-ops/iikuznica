"""Plan employee requests before retrieving evidence. Previous answers are never policy sources."""
import json
import re

import ask
import evidence
import procedures
from run import complete
from variants import GEMMA, make_request


PLANNER = """Разбери сообщение сотрудника. История нужна только для понимания ссылок и уточнений.
Ответ пользователю здесь не давай. На входе пронумерованы фрагменты одного сообщения и прошлые ходы.
Верни JSON {"parts":[{"through":1,"relation":"new","anchor":null,"kind":"fact","query":"..."}]}.
through — номер последнего фрагмента в группе. Группы идут подряд, охватывают все фрагменты.
relation=new для новой просьбы с собственными обстоятельствами; shared для дополнительной просьбы
об уже описанном в ЭТОМ сообщении объекте; history для продолжения или возврата к теме, тогда
anchor — номер выбранной реплики пользователя из истории. Во всех остальных случаях anchor=null.
Не наследуй дирекцию соседней независимой просьбы. Отдельно обработай кадровый или личный вопрос.
Один процесс с условными ветвями, перечнем шагов или сравнением — одна группа kind=procedure.
Но если сотрудник дополнительно спрашивает отдельный реквизит (например, срок конкретной передачи),
выдели его в самостоятельную группу, если по нему возможен отдельный результат. kind=fact для точного вопроса.
Для history/shared составь самодостаточный короткий поисковый вопрос: сохрани заданные дирекцию, этап,
объект и важные условия, подставь конкретный предмет вместо местоимения. Учти действительное последнее уточнение.
Используй только сказанное сотрудником. Из реплики бота можно восстановить предмет вопроса, но не нормативный факт.
Если две прошлые темы одинаково правдоподобны, kind=clarify и query — короткий вопрос с конкретными вариантами.
Явный новый вопрос с названным предметом может прервать уточнение бота. Не дополняй вопрос ответом, цифрой,
новой ролью, решением или догадкой о подразделении. Не делай из перечисления шагов несколько разных процессов."""
STEP_INSTRUCTION = ("Собери применимые шаги процедуры по порядку из оригиналов. Сохрани обязательные роли, "
                    "условия допуска, сроки и действия при исключениях. Покажи только нужные ветви. "
                    "Не называй варианты последовательными обязанностями.")
QUESTION_WORD = r"(?:кто|кому|где|куда|как|когда|зачем|почему|сколько|како\w*|каки\w*|что|можно ли|нужно ли|в как\w*|через сколько)"
TOPIC_MARKER = r"(?:(?:и|а) ещ[её]|заодно|отдельный вопрос|а также)"
BOUNDARY = re.compile(r"(?<=[?!])\s+|;\s*|(?:,\s*|\s+)(?P<marker>" + TOPIC_MARKER +
                      r"\b|(?:и|а)\s+" + QUESTION_WORD + r"\b)|,\s*(?P<bare>кто|кому|куда|когда|где|как|"
                      r"сколько|что (?:делать|нужно)|в как\w*|како\w*|каки\w*)\b", re.I)
LEADING_CONNECTOR = re.compile(r"^(?:(?:а|и)\s+(?:ещё|еще)\b[, ]*|заодно\s+|(?:а|и)\s+)", re.I)
WORKFLOW = re.compile(r"по шагам|пошагов|порядок действий|как (?:довести|оформить|подписать|подготовить|"
                      r"зарегистрировать|пройти)|какие согласования|опиши путь|расскажи маршрут|проведи меня|"
                      r"от .{1,48} до |в каком порядке", re.I)


def fragments(question):
    cuts = [0]
    for m in BOUNDARY.finditer(question):
        cuts.append(m.start(m.lastgroup) if m.lastgroup else m.end())
    cuts.append(len(question))
    chunks = [question[a:b].strip(" ,;") for a, b in zip(cuts, cuts[1:]) if question[a:b].strip(" ,;")]
    for i in range(len(chunks) - 2, -1, -1):
        if re.fullmatch(r"(?:" + TOPIC_MARKER + r")(?:\s+(?:подскажи|скажи))?[, ]*", chunks[i], re.I):
            chunks[i:i+2] = [chunks[i] + " " + chunks[i+1]]
    # Keep a contextual preamble attached to the first actual request.
    if len(chunks) > 1 and not re.search(QUESTION_WORD + r"\b|\?|подскажи|расскажи|опиши|прошу", chunks[0], re.I):
        chunks[:2] = [" ".join(chunks[:2])]
    return chunks


def visible_history(history, question):
    turns = []
    for role, text in history:
        if role == "user":
            turns.append({"turn": len(turns) + 1, "user": text, "assistant": ""})
        elif role == "assistant" and turns:
            turns[-1]["assistant"] = text[:2000]
    if len(turns) <= 8:
        return turns
    terms = lambda s: {w[:5] for w in re.findall(r"[а-яa-z]{4,}", s.casefold())}
    query = terms(question)
    older = sorted(turns[:-6], key=lambda t: (len(query & terms(t["user"])), t["turn"]), reverse=True)
    chosen = older[:2]
    if re.search(r"перв(?:ому|ый|ого) вопрос", question, re.I):
        chosen = [turns[0], next(t for t in older if t["turn"] != 1)]
    return sorted(chosen + turns[-6:], key=lambda t: t["turn"])


def parse_plan(raw, pieces, history):
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3]
    items = json.loads(raw)["parts"]
    if not isinstance(items, list) or not 1 <= len(items) <= len(pieces):
        raise ValueError("Invalid part count")
    by_turn = {h["turn"]: h for h in history}
    parts, start = [], 0
    for item in items:
        end, rel, anchor, kind = (item[k] for k in ("through", "relation", "anchor", "kind"))
        if type(end) is not int or not start < end <= len(pieces) or kind not in ("fact", "procedure", "clarify"):
            raise ValueError("Invalid span or kind")
        if rel not in ("new", "shared", "history") or (rel == "shared" and not start):
            raise ValueError("Invalid relation")
        if rel == "history":
            if type(anchor) is not int or anchor not in by_turn:
                raise ValueError("Invalid history anchor")
        elif anchor is not None:
            raise ValueError("Unexpected history anchor")
        literal = LEADING_CONNECTOR.sub("", " ".join(pieces[start:end])).strip(" ,;")
        query = literal if rel == "new" and kind != "clarify" else item["query"]
        if not isinstance(query, str) or not query.strip() or len(query) > max(1100, len(literal)):
            raise ValueError("Unusable standalone question")
        if kind == "clarify" and not query.rstrip().endswith("?"):
            raise ValueError("Expected a clarification question")
        if rel in ("shared", "history") and kind != "clarify":
            context = " ".join(pieces[:end]) if rel == "shared" else " ".join(
                t["user"] for t in history if t["turn"] <= anchor) + " " + literal
            dirs = procedures.directories(literal) or procedures.directories(context)
            if (not procedures.directories(query) <= dirs or
                    set(re.findall(r"\d+", query)) - set(re.findall(r"\d+", context))):
                raise ValueError("Added a directorate or numerical premise")
        parts.append({"text": literal, "query": query.strip(), "kind": kind, "relation": rel, "anchor": anchor})
        start = end
    if start != len(pieces):
        raise ValueError("Plan omitted part of the incoming message")
    return parts


def plan(question, history, call):
    pieces = fragments(question)
    if not history and (len(pieces) == 1 or ask.pay_shortcut(question)):
        return [{"text": question, "query": question, "kind": "procedure" if WORKFLOW.search(question) else "fact",
                 "relation": "new", "anchor": None}]
    past = visible_history(history, question)
    data = {"question": [{"n": i, "text": s} for i, s in enumerate(pieces, 1)], "sources": past}
    body = make_request(data, GEMMA, PLANNER)
    row = call("plan", body, "conversation-plan")
    if not row["ok"]:
        raise RuntimeError(f"Planner HTTP {row['http']}")
    try:
        result = parse_plan(row["text"], pieces, past)
        if len(result) == 1 and result[0]["relation"] == "new" and result[0]["kind"] != "clarify":
            result[0].update(text=question, query=question)
        return result
    except (ValueError, KeyError, TypeError):
        return [{"text": question, "kind": "clarify", "query": "Можешь уточнить, к какому процессу относится "
                 "каждая часть вопроса?", "relation": "unclear", "anchor": None}]


def assemble(parts):
    if len(parts) == 1:
        return dict(parts[0]["result"], requests=parts)
    sources, paragraphs = [], []
    for part in parts:
        result = part["result"]
        mapping = {}
        for i, source in enumerate(result["sources"], 1):
            if source not in sources:
                sources.append(source)
            mapping[i] = sources.index(source) + 1
        def number(match):
            local = int(match[1])
            return "[" + str(mapping[local]) + "]" if local in mapping else match[0]
        body = re.sub(r"\[(\d+)\]", number, result["message"]) if mapping else result["message"]
        paragraphs.append(part["text"] + "\n" + body)
    actions = {p["result"]["action"] for p in parts}
    primary = next(a for a in ("answer", "clarify", "escalate", "out_of_scope") if a in actions)
    return {"action": primary, "message": "\n\n".join(paragraphs), "sources": sources, "requests": parts,
            "offer_quality_request": any(p["result"].get("offer_quality_request") for p in parts)}


def explicit_pair(question):
    """Only split two complete questions when the second establishes its own scope."""
    chunks = re.fullmatch(r"([^?]+\?)\s+([^?]+\?)", question.strip(), re.S)
    if not chunks:
        return []
    first, second = chunks.groups()
    second = LEADING_CONNECTOR.sub("", second).strip()
    refers_back = re.search(r"\b(?:ему|его|её|ее|им|их|него|неё|ней|них|там|тогда|этот|этой|этого|"
                            r"этим|этом|таком|такой|предыдущ\w*|указанн\w*)\b", second, re.I)
    if not ask.pay_shortcut(second) and (not procedures.directories(second) or refers_back):
        return []
    return [first, second]


def answer_explicit(question, search, *, history=(), generate_answer=evidence.generate,
                    judge=evidence.judge, faq=None, faq_scope=None, respond=None):
    """Apply explicit splitting on standalone messages, preserving the baseline elsewhere."""
    def run(text, prior):
        return (respond or ask.answer)(text, search, history=prior, procedure_mode=True, instruction="",
                          add_sources=evidence.alternatives, generate_answer=generate_answer,
                          judge=judge, faq=faq, faq_scope=faq_scope)
    pair = [] if history else explicit_pair(question)
    if not pair:
        return run(question, history)
    if faq is not None:
        direct = faq.lookup(question.strip(), faq_scope)
        if direct:
            return direct
    return assemble([{"text": q, "query": q, "result": run(q, ())} for q in pair])


def answer(question, search, *, history=(), planner=complete, generate_answer=evidence.generate,
           judge=evidence.judge, faq=None, faq_scope=None):
    question = question.strip()
    if not question:
        raise ValueError("Введи вопрос")
    if faq is not None:
        direct = faq.lookup(question, faq_scope)
        if direct:
            return direct
    parts = plan(question, history, planner)
    for part in parts:
        if part["kind"] == "clarify":
            result = dict(action="clarify", message=part["query"], sources=[], offer_quality_request=False)
        else:
            workflow = part["kind"] == "procedure"
            result = ask.answer(part["query"], search, generate_answer=generate_answer, judge=judge,
                                add_sources=evidence.alternatives, instruction=STEP_INSTRUCTION if workflow else "",
                                procedure_mode=True, workflow=workflow, faq=faq, faq_scope=faq_scope)
            if workflow and result["action"] == "answer" and "\n\n" in result["message"] and not result.get("discrepancy") and all(
                    "doc" in src for src in result["sources"]):
                steps = result["message"].split("\n\n")
                result = dict(result, message="\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
        part["result"] = result
    return assemble(parts)
