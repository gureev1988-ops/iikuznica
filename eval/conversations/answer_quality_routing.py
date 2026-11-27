"""Isolated variants for FAQ applicability, request coverage, topic and search."""
import re

from check import ask, evidence
from answer_quality_gates import key, quote_matches
import conversation
import dialogue_state
from quality import ApprovedFAQ, canonical

procedures = evidence.procedures
SPLIT = """Разметь дословные интервалы запроса сотрудника. Отдельные части — самостоятельные
просьбы, общие обстоятельства и условия. Верни интервал дословным фрагментом исходного
запроса, с неизменной пунктуацией между словами. Для каждой просьбы укажи индексы общих обстоятельств из context
и индексы предшествующих просьб, от ответа на которые зависит действие. Продолжение
одного требования не выделяй в самостоятельную просьбу. Команда и вопрос в одном
предложении могут быть самостоятельными. Не переписывай текст исходной реплики.
Верни JSON {"context":["..."],"requests":[{"span":"...","context":[0],"depends_on":[]}]}."""

OBJECT = """Проверь привязку последней реплики к операции и объекту. Используй слова сотрудника
и ранее восстановленные темы. Ответ бота используй лишь при явной ссылке на его шаг.
Явно названные новые объект и дирекция заменяют прежние. Изменение условия само по себе
не меняет объект. При продолжении сформулируй вопрос о последнем действии, без повторения
старого действия. Два чтения указывай только если действия различаются и оба чтения
обоснованы. Для каждого дай номер хода от 1 и дословный фрагмент вопроса сотрудника о
связанном объекте. Для новой темы верни пустой массив. Не добавляй нормативных фактов.
JSON {"options":[{"turn":1,"quote":"","object":"","query":""}]}."""

GAP = """Определи, что именно мешает ответить на просьбу. Если нужно узнать обстоятельство
проекта, которое может сообщить сотрудник, задай один конкретный короткий вопрос.
Приведи два возможных значения этого поля и два применимых пункта для них, из которых
следуют разные действия. Если обстоятельство уже известно, укажи дословную фразу
сотрудника. Если отсутствует именно официальное правило, реквизит или срок, передай
только этот пробел службе качества. Без достаточного основания верни unchanged.
JSON {"kind":"employee|company|unchanged","field":"","question":"","known":"",
"branches":[{"value":"","action":"","doc":"","ref":"","quote":""}]}."""

SEARCH = """Выдели один конкретный недостающий пункт, нужный для ответа. Если нужное правило
уже есть в оригиналах, верни kind present с точной цитатой и doc/ref. Если не хватает
обстоятельства, которое знает сотрудник, верни kind employee. Для отсутствующего
известного типа нормы предложи короткий поисковый запрос, сохраняя известные дирекцию,
операцию, объект и запрашиваемый реквизит, не предполагая новые числа или дирекции.
JSON {"kind":"missing|present|employee|unchanged","query":"","doc":"","ref":"","quote":""}."""

COMPOUND = re.compile(r";|\b(?:а также|заодно|ещ[её]|кроме того|и (?:кто|где|как|кому|когда|какой|проверь|скажи))\b", re.I)
FOLLOW = re.compile(r"\b(?:он|она|они|ему|им|там|туда|этот|эта|том|тот|по нему|а если|если|тогда|может начинать)\b", re.I)


def simple_reply(action, message):
    return {"action": action, "message": message, "sources": [], "offer_quality_request": action == "escalate"}


def split_requests(trial, q, past, respond):
    if len(q.split()) < 10 or (q.count("?") < 2 and not COMPOUND.search(q)):
        return None
    try:
        plan = trial.model_json("A3-split", q, [], SPLIT)
        def position(text):
            if not isinstance(text, str) or not text.strip() or q.count(text) != 1:
                raise ValueError("The request span does not identify a unique verbatim fragment")
            start = q.index(text)
            return start, start + len(text)

        pieces = [dict(piece, span=position(piece["span"])) for piece in plan["requests"]]
        common = list(map(position, plan["context"]))
        if not 1 <= len(pieces) <= 6 or len(common) > 6:
            raise ValueError("Invalid number of requests or context spans")
        spans = [(start, end) for start, end in [*common, *(p["span"] for p in pieces)]]
        last = 0
        residual = []
        for start, end in sorted(spans):
            if type(start) is not int or type(end) is not int or not last <= start < end <= len(q):
                raise ValueError("Overlapping or invalid request spans")
            residual.append(q[last:start])
            last = end
        residual.append(q[last:])
        meaningful = [s.strip(" \t\n,.;:?!") for s in residual]
        remainder = " ".join(s for s in meaningful if s and canonical(s) not in ("и", "а", "а также", "заодно"))
        if len(pieces) == 1 and not remainder:
            return None
        parts = []
        for i, piece in enumerate(pieces):
            start, end = piece["span"]
            if any(type(j) is not int or not 0 <= j < len(common) for j in piece["context"]):
                raise ValueError("Invalid shared context reference")
            if any(type(j) is not int or not 0 <= j < i for j in piece["depends_on"]):
                raise ValueError("Invalid dependency")
            if i and start < pieces[i-1]["span"][1]:
                raise ValueError("Requests out of order")
            context = " ".join(q[common[j][0]:common[j][1]].strip() for j in piece["context"])
            text = q[start:end].strip()
            query = (context + " " + text).strip()
            if any(parts[j]["result"]["action"] != "answer" for j in piece["depends_on"]):
                result = simple_reply("clarify", "Эта часть зависит от ответа на предыдущую просьбу.")
            else:
                result = respond(query, past if i == 0 else ())
                if piece["depends_on"] and result["action"] == "answer":
                    # Confirmation that an earlier request can be answered is not
                    # confirmation that the employee completed that prerequisite.
                    result = simple_reply("clarify", "Подтверди условие из предыдущей части перед этим действием.")
            parts.append({"text": text, "query": query, "result": result})
        if remainder:
            parts.append({"text": remainder, "query": remainder,
                          "result": simple_reply("escalate", "Эту часть не удалось однозначно разобрать. Передать её в отдел качества?")})
        trial.events.append({"stage": "A3", "plan": plan, "remainder": remainder})
        return conversation.assemble(parts)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        trial.events.append({"stage": "A3", "error": str(exc)})
        return simple_reply("clarify", "Какие действия нужно разобрать отдельно и какие из них зависят друг от друга?")


def restore_object(trial, q, past, relation, resolved):
    if not (FOLLOW.search(q) or dialogue_state.REPLY_REFERENCE.search(q) or relation in ("ambiguous", "follow")):
        return relation, resolved
    history = [dict(turn=i, user=t["user"], resolved=t["resolved"],
                    **({"assistant": t["assistant"]} if dialogue_state.REPLY_REFERENCE.search(q) else {}))
               for i, t in enumerate(past, 1)][-5:]
    try:
        plan = trial.model_json("A4-object", {"employee": q, "relation": relation, "resolved": resolved},
                                history, OBJECT)
        options = plan["options"]
        if not isinstance(options, list) or len(options) > 2:
            raise ValueError("Invalid object options")
        turns = {t["turn"]: t for t in history}
        for item in options:
            t = turns[item["turn"]]
            if not item["quote"].strip() or item["quote"] not in t["user"] or not item["object"].strip():
                raise ValueError("Object is missing its employee anchor")
            known = procedures.directories(q) or procedures.directories(q + " " + t["user"] + " " + t["resolved"])
            if (procedures.directories(item["query"]) - known
                    or set(re.findall(r"\d+", item["query"])) - set(re.findall(r"\d+", q + t["user"] + t["resolved"]))):
                raise ValueError("Object query adds an unsupported condition")
        trial.events.append({"stage": "A4", "plan": plan, "baseline": [relation, resolved]})
        if len(options) == 2 and key(options[0]["object"]) != key(options[1]["object"]):
            return "ambiguous", f'Ты имеешь в виду «{options[0]["object"]}» или «{options[1]["object"]}»?'
        if options and (query := options[0]["query"].strip()):
            return "follow", query
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        trial.events.append({"stage": "A4", "error": str(exc)})
    return relation, resolved


def gap_route(trial, q, sources, pool, draft, verdict, route, final, attempt, fact):
    unchanged = route, final, None, None
    if trial.variant == "K2":
        if route not in ("clarify", "escalate") and not (attempt and not final):
            return unchanged
        plan = trial.model_json("K2-gap", {"employee": q, "draft": draft, "route": route}, sources, GAP)
        trial.events.append({"stage": "K2", "plan": plan})
        if plan.get("kind") == "company" and (field := plan.get("field", "").strip()):
            return "escalate", simple_reply(
                "escalate", f"В регламенте не нашёл подтверждения по этому вопросу: {field}. Передать его в отдел качества?"), None, None
        branches = plan.get("branches", [])
        question = plan.get("question", "")
        if (plan.get("kind") == "employee" and question.endswith("?") and question.count("?") == 1
                and not plan.get("known") and isinstance(branches, list) and len(branches) == 2
                and all(quote_matches(branch, sources) and branch.get("value") and branch.get("action")
                        for branch in branches)
                and key(branches[0]["value"]) != key(branches[1]["value"])
                and key(branches[0]["action"]) != key(branches[1]["action"])
                and branches[0]["quote"] != branches[1]["quote"]
                and not any(key(branch["value"]) in key(q) for branch in branches)):
            return "clarify", simple_reply("clarify", question), None, None
        return unchanged
    if route != "retrieve" or attempt:
        return unchanged
    plan = trial.model_json("K3-query", {"employee": q, "draft": draft, "verdict": verdict["answers"]},
                            sources, SEARCH)
    trial.events.append({"stage": "K3", "plan": plan})
    dirs = procedures.directories(q)
    applicable = [s for s in sources if not dirs or s["doc"] in dirs]
    if plan.get("kind") == "present" and quote_matches(plan, applicable):
        return "revise", None, plan["quote"], None
    query = plan.get("query", "")
    if (plan.get("kind") != "missing" or not isinstance(query, str) or not query.strip()
            or procedures.directories(query) != dirs
            or set(re.findall(r"\d+", query)) - set(re.findall(r"\d+", q))):
        return unchanged
    _, target = trial.lookup(query)
    corpus = ask.full_corpus()
    present = {evidence.reference(s) for s in sources}
    ranked = sorted(target, key=lambda row: -row["logit"])
    additions = [corpus[k] for row in ranked if (k := evidence.reference(row)) in corpus
                 and k not in present and (not dirs or k[0] in dirs)][:5]
    trial.events.append({"stage": "K3-search", "question": query, "pool": target, "additions": additions})
    return "retrieve", None, None, [*sources, *additions]


class ApplicableFAQ(ApprovedFAQ):
    """Consume the quality service's trusted snapshot and the caller's authorized context."""
    def __init__(self, publications, context):
        super().__init__(publications)
        self.context = context

    def lookup(self, question, scope):
        account = self.context
        matched, missing = [], set()
        q = canonical(question)
        for item in self.publications:
            source = item["snapshot"]
            meta = item.get("applicability", {})
            if (not scope or not meta.get("mailbox") or meta.get("mailbox") != account.get("mailbox")
                    or meta.get("current") is not True or account.get("role") not in meta.get("roles", [])
                    or source.get("status") != "published"):
                continue
            keys = meta.get("terms", ())
            alias = q in {canonical(s) for s in (source["question"], *item.get("approved_aliases", ()))}
            if not alias and (question.count("?") > 1 or COMPOUND.search(q)
                              or re.search(r"\bи в какой срок\b", q)
                              or not (keys and all(any(canonical(word) in q for word in words) for words in keys))):
                continue
            criteria = meta.get("conditions", {})
            if any(k in account and account[k] != val for k, val in criteria.items()):
                continue
            proposed = ApprovedFAQ([item]).lookup(source["question"], scope)
            if not proposed:
                continue
            if unknown := criteria.keys() - account.keys():
                missing.update(unknown)
            else:
                matched.append((source["answer"], proposed))
        if missing:
            return simple_reply("clarify", "Уточни " + sorted(missing)[0] + "?")
        if not matched:
            return None
        if len({canonical(answer) for answer, _ in matched}) > 1:
            return simple_reply("escalate", "Действующие разъяснения расходятся. Передать вопрос в отдел качества?")
        return matched[0][1]
