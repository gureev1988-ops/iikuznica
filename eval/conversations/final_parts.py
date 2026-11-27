"""Recover independent parts of a compound question after a whole-turn abstention."""
import re

import final_quality_trial as experiment
from check import ask, load_bank
import conversation
from procedures import directories, full_procedure

PLAN = """Разметь несколько просьб сотрудника, если на них можно отвечать по отдельности.
Используй историю только для понимания ссылок. Числа, нормы и ответы не добавляй.
Верни {"context":["дословный фрагмент"],"requests":[{"text":"дословная просьба",
"context":[0],"history":true,"depends_on":[0],"subject":{"from":0,"quote":"объект"}}]}.
Пронумеруй контекст и просьбы от 0. В text и context копируй непересекающиеся фразы текущей
реплики. Каждая содержательная часть реплики должна быть либо контекстом, либо просьбой.
Общие условия оставляй в context и привязывай ко всем затронутым просьбам. history=true, если
для понимания просьбы нужен предыдущий диалог. Для личной просьбы history=false.
Если следующая просьба опускает объект, с которым связана, в subject укажи индекс предыдущей
просьбы и скопируй из неё до восьми слов о самом объекте. Иначе subject=null.
Отметь depends_on, если выполнение действия зависит от неизвестного результата предыдущей
просьбы. Если зависимость нельзя выразить, если порядок меняет суть или это один процесс с
несамостоятельными шагами, верни {"context":[],"requests":[]}. Не создавай отдельную просьбу
из вводной фразы, союза или условия. Не переписывай вопрос и не составляй ответ."""

COMPOUND = re.compile(r";|\?|\b(?:заодно|(?:а|и)\s+ещ[её]|а также|и\s+"
                      r"(?:кто|кому|как|когда|где|сколько|како\w*|подскажи|скажи|назови|можно))\b", re.I)
CONDITION = re.compile(r"\b(?:если|только после|после того как|при условии|на этом основании)\b", re.I)
PERSONAL = re.compile(r"\b(?:мне|себе|мо[йю])\b", re.I)
CONNECTOR = re.compile(r"(?:(?:и|а|заодно|также|ещ[её])(?:\s+(?:и|а|заодно|также|ещ[её]))*)?", re.I)


def should_try(question, reply):
    return (reply["action"] in {"clarify", "escalate", "out_of_scope"} and not reply.get("requests")
            and not full_procedure(question)
            and bool(COMPOUND.search(question.rstrip("?"))))


def parse_plan(question, raw):
    def span(text):
        if not isinstance(text, str) or not text.strip() or question.count(text) != 1:
            raise ValueError("An excerpt must be unique and verbatim")
        start = question.index(text)
        return start, start + len(text)

    context, parts = raw["context"], raw["requests"]
    if not isinstance(context, list) or not isinstance(parts, list) or len(context) > 4 or not 2 <= len(parts) <= 3:
        raise ValueError("Expected two or three separable requests")
    requests = [(span(part["text"]), part) for part in parts]
    if sorted(x[0] for x in requests) != [x[0] for x in requests]:
        raise ValueError("Requests changed order")
    covered = sorted([span(text) for text in context] + [x[0] for x in requests])
    end = 0
    for start, stop in [*covered, (len(question), len(question))]:
        remainder = question[end:start].strip(" \t\n,.?!:;—-")
        if start < end or not CONNECTOR.fullmatch(remainder):
            raise ValueError("Plan overlaps excerpts or loses substantive text")
        end = stop
    for i, (_, part) in enumerate(requests):
        links, depends = part["context"], part["depends_on"]
        if (type(part["history"]) is not bool or not isinstance(links, list) or not isinstance(depends, list)
                or any(type(j) is not int or not 0 <= j < len(context) for j in links)
                or any(type(j) is not int or not 0 <= j < i for j in depends)
                or len(links) != len(set(links)) or len(depends) != len(set(depends))):
            raise ValueError("Invalid context or prerequisite references")
        subject = part.get("subject")
        if subject is not None:
            previous, quote = subject["from"], subject["quote"]
            if (type(previous) is not int or not 0 <= previous < i or not isinstance(quote, str)
                    or not 1 <= len(quote.split()) <= 8 or re.search(r"[.!?;\n]", quote)
                    or quote not in parts[previous]["text"]):
                raise ValueError("The omitted subject must come from an earlier request")
        own_dirs = directories(part["text"])
        common_dirs = directories(" ".join(context[j] for j in links))
        subject_dirs = directories(subject["quote"]) if subject else set()
        if own_dirs and (common_dirs | subject_dirs) - own_dirs:
            raise ValueError("The plan carried a different directorate into an explicitly scoped question")
    if set(j for part in parts for j in part["context"]) != set(range(len(context))):
        raise ValueError("Plan dropped shared text")
    if CONDITION.search(question) and not any(part["depends_on"] for part in parts):
        raise ValueError("A conditional question needs an explicit prerequisite")
    return context, parts


def candidate(arm, question, past, row, trial):
    if not should_try(question, row["response"]):
        return None
    raw = trial.model_json("F1-plan", {"question": question,
        "history": [{"user": t["user"], "assistant": t["assistant"]} for t in past[-6:]]}, [], PLAN)
    try:
        context, parts = parse_plan(question, raw)
    except (KeyError, TypeError, ValueError) as exc:
        trial.events.append({"stage": "F1-plan-rejected", "reason": str(exc)})
        return None
    results = []
    for part in parts:
        text = part["text"]
        shared = " ".join(context[j] for j in sorted(part["context"]))
        subject = part.get("subject")
        context_text = f"Контекст — {subject['quote']}. " if subject else ""
        query = f"{shared} {context_text}{text}".strip()
        if part["depends_on"]:
            prereq = " ".join(parts[j]["text"] for j in part["depends_on"])
            reply = {"action": "clarify", "message": f"Сначала нужно уточнить условие из предыдущей части. {prereq}",
                     "sources": [], "offer_quality_request": False}
        else:
            # A fragment with no own scope can still refer to an earlier project.
            # A standalone first-person personal request gets a chance to route without it.
            child_past = past if part["history"] or (not directories(query) and not PERSONAL.search(text)) else ()
            sub = experiment.control.run_turn(trial.calls.out, trial.calls.cid, query, child_past,
                                               trial.lookup, "C", diagnose=True, observer=trial)
            reply = sub["response"]
        results.append({"text": text, "query": query, "result": reply})
    trial.events.append({"stage": "F1-parts", "plan": raw, "actions": [p["result"]["action"] for p in results]})
    if not any(part["result"]["action"] == "answer" for part in results):
        return None
    return conversation.assemble(results)


def panel(corpus):
    work = [case for case in load_bank() if case["id"].startswith(("M", "C")) or case["id"] in {"P01", "P02", "P03"}]
    prompts = (
        ("В ДБРЦТ кто отвечает за мониторинг технических рисков, и какой у этого человека внутренний номер?",
         ["answer DB §16.2", "escalate only the phone number"]),
        ("ДБРЦТ. Как мне выбрать билет на концерт и кто отвечает за мониторинг технических рисков?",
         ["out_of_scope", "answer DB §16.2"]),
        ("Техническая дирекция. Кто отправляет заказчику финальное ТКП и когда ровно закончится рассмотрение нашей заявки?",
         ["answer TECH §6.4", "clarify or escalate only the unknown live status"]),
        ("В ДБРЦТ кто организует управление рисками и кто отвечает за мониторинг технических рисков?",
         ["answer both duties with the applicable sources"]),
        ("ДБРЦТ. Если не знаем, подписан ли протокол ОПЭ, проверь это и можно ли уже выпускать УПД?",
         ["clarify actual status", "no unconditional permission to issue UPD"]),
        ("Техническая дирекция. Проверь, согласована ли сделка, и отправь ТКП на этом основании.",
         ["do not claim that approval or submission happened", "keep the prerequisite"]),
    )
    return work + [{"id": f"F1-{i:02d}", "turns": [{"question": q, "expect": expected}]}
                   for i, (q, expected) in enumerate(prompts, 1)]


def check(corpus):
    text = "ДБРЦТ. Кто отвечает за риски, и какой у него телефон?"
    make = lambda excerpt, deps=(): {"text": excerpt, "context": [0], "history": False, "depends_on": list(deps)}
    plan = {"context": ["ДБРЦТ."], "requests": [make("Кто отвечает за риски"), make("какой у него телефон?")]}
    assert parse_plan(text, plan)
    assert should_try(text, ask.reply("escalate", "gap"))
    assert not should_try(text, {"action": "answer"})
    for broken in (
        {**plan, "requests": [*plan["requests"][:1], make("телефон?")]},
        {**plan, "requests": [make("Кто отвечает за риски", [1]), plan["requests"][1]]},
    ):
        try:
            parse_plan(text, broken)
        except ValueError:
            continue
        raise AssertionError("Unsafe plan was accepted")


if __name__ == "__main__":
    experiment.main(panel, candidate, description=__doc__, check=check)
