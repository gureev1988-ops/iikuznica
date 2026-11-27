"""Check the requested property against an exact original before publishing a disputed answer."""
import re

from answer_quality_panels import A5_CASES, fact_frames
from check import ask, load_bank
import final_quality_trial as experiment
import judge_pair
from procedures import directories, full_procedure

PROOF = """Проверь, отвечает ли показанный черновик на конкретный реквизит, о котором спрашивает
сотрудник. Сначала определи запрошенное свойство и операцию по вопросу и восстановленному контексту.
Проверь действие, роль, получателя, форму, срок, условие, запрет и исключение только в той мере,
в которой они нужны для выполнения запрошенного действия. Соседняя стадия, реквизит или дирекция
не подтверждают ответ. Корректный пересказ засчитывай; наличие ссылки само по себе ничего не доказывает.
Верни JSON {"decision":"keep|repair|missing","asked":"дословная часть исходного вопроса","excerpts":[]}.
keep: черновик уже передаёт норму по смыслу или спор нельзя разрешить из источников.
repair: черновик пропустил или подменил существенное, а указанные выдержки целиком отвечают
на эту просьбу, включая применимые условия и исключения. В excerpts дай от одного до трёх
различных ID. Если для полноценного ответа нужно домыслить условие или дать ещё одну инструкцию,
не выбирай repair. missing: сама ситуация ясна, но ни одна выдержка не устанавливает запрошенный
реквизит. Не используй missing при неясной ситуации, наличии применимого правила или сложной
просьбе, которую нельзя безопасно проверить целиком. Для keep и missing верни пустой excerpts.
Текст ответа не пиши."""

TARGET = re.compile(r"\b(?:кому|адресат\w*|срок\w*|услови\w*|формат\w*|способ\w*|канал\w*|"
                    r"обязатель\w*|можно|достаточно|в каком|каким|куда|сколько|когда|после чего)\b", re.I)


def excerpt_catalog(sources):
    result = {}
    for source in sources:
        text = source["text"]
        # Preserve contiguous excerpts and the full clause as a choice for conditions
        # or lists. The proof model cannot author an employee-facing sentence.
        lines = [line.strip() for line in re.split(r"\n|(?<=[.!?])\s+(?=[А-ЯЁA-Z])", text) if line.strip()]
        choices = [text, *[line for line in lines if len(line) > 20]]
        for quote in dict.fromkeys(choices):
            if quote not in text:
                raise ValueError("Excerpt differs from the original")
            key = str(len(result) + 1)
            result[key] = {"doc": source["doc"], "ref": source["ref"], "quote": quote,
                           "context": source["section"]}
    return result


def validate(question, plan, catalog):
    decision, asked, keys = (plan[k] for k in ("decision", "asked", "excerpts"))
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
        raise ValueError("Proof must use excerpt IDs")
    if decision == "keep" and not keys:
        return decision, []
    if not isinstance(asked, str) or len(asked.strip()) < 4 or asked not in question:
        raise ValueError("The proof must anchor the actual requested property")
    if decision == "missing" and not keys:
        return decision, []
    if decision != "repair" or not 1 <= len(keys) <= 3 or len(set(keys)) != len(keys) or any(k not in catalog for k in keys):
        raise ValueError("Invalid evidence selection")
    return decision, [catalog[k] for k in keys]


def quote_draft(excerpts):
    refs, lines = [], []
    for excerpt in excerpts:
        ref = {"doc": excerpt["doc"], "ref": excerpt["ref"]}
        if ref not in refs:
            refs.append(ref)
        lines.append(f'{excerpt["quote"]} [{refs.index(ref)+1}]')
    return {"action": "answer", "message": "\n\n".join(lines), "sources": refs}


def candidate(arm, question, past, row, trial):
    frozen = "diagnostics" not in row
    if (row["response"].get("requests") or full_procedure(question) or question.count("?") > 1
            or (not frozen and (not TARGET.search(question) or row["response"]["action"] == "out_of_scope"))):
        return None
    gate = next((e for e in reversed(trial.events) if e["stage"] == "gate" and e["draft"]["action"] == "answer"), None)
    if gate is None or full_procedure(gate["question"]):
        return None
    dirs = directories(gate["question"])
    if len(dirs - {"PRE"}) != 1:
        return None
    pool = [p for p in gate["sources"] if p["doc"] in dirs]
    catalog = excerpt_catalog(pool)
    if not catalog:
        return None
    raw = trial.model_json("F2-proof", {"question": question, "resolved": gate["question"],
        "draft": gate["draft"]["message"]}, [dict(id=k, **v) for k, v in catalog.items()], PROOF)
    decision, excerpts = validate(question, raw, catalog)
    trial.events.append({"stage": "F2-proof", "decision": decision, "proof": raw, "selected": excerpts})
    if decision == "keep":
        return None
    if decision == "missing":
        return dict(ask.reply("escalate", "gap"),
                    message=f'В проверенных пунктах не нашёл подтверждения по этой части: «{raw["asked"]}». Передать её в отдел качества?')
    draft = quote_draft(excerpts)
    # The candidate still has to pass applicability and completeness review.
    result = experiment.reviewed(trial, gate["question"], draft, gate["sources"])
    return result if result and result["action"] == "answer" else ask.reply("escalate", "gap")


FORM_CASES = (
    ("form", "PRE:3.4:2", "Пресейл ИБ. Через какой канал первая линия запрашивает недостающие данные у автора заявки?",
     "заметкой в CRM", "письмом"),
    ("form", "DB:11.4:1", "ДБРЦТ. В какой системе публикуют результаты внутренней установочной встречи?",
     "в системе управления проектами", "в мессенджере"),
    ("branch", "TECH:17.6:1", "Техническая дирекция. При каком условии АМ сам запрашивает отчёт по поставкам?",
     "При отсутствии Менеджера по управлению поставками", "При наличии Менеджера по управлению поставками"),
    ("condition", "DB:9.3:1", "ДБРЦТ. При каком условии на инициации требуется подбор субподрядчика?",
     "При отсутствии внутренних ресурсов", "При наличии внутренних ресурсов"),
)


def panel(corpus):
    question, sources, correct, short = judge_pair.inputs()
    examples = [("F2-Q04-correct", question, correct, sources, False),
                ("F2-Q04-missing", question, short, sources, True)]
    frames = fact_frames(corpus, (*FORM_CASES, *(A5_CASES[i] for i in (0, 5, 9, 12, 17, 20, 22))),
                        "property_pairs")
    examples.extend(("F2-" + f["id"], f["question"], f["draft"], f["sources"], f["mutated"]) for f in frames)
    pinned = [{"id": name, "turns": [{"question": q, "frozen": [draft, src],
              "expect": {"mutated_draft": mutated, "criterion": "answer the requested property using the cited original"}}]}
              for name, q, draft, src, mutated in examples]
    return pinned + [c for c in load_bank() if c["id"] in {"P01", "P02", "P03", "P07", "D05"}]


def check(corpus):
    src = corpus["PRE", "3.4"]
    catalog = excerpt_catalog([src])
    question = "Через какой канал запрашивают данные?"
    key = next(k for k, p in catalog.items() if "через заметку в CRM" in p["quote"])
    plan = {"decision": "repair", "asked": "какой канал", "excerpts": [key]}
    _, selected = validate(question, plan, catalog)
    assert selected[0]["quote"] in src["text"]
    assert quote_draft(selected)["sources"] == [{"doc": "PRE", "ref": "3.4"}]
    try:
        validate(question, {**plan, "excerpts": ["unseen"]}, catalog)
    except ValueError:
        return
    raise AssertionError("Accepted a non-corpus excerpt")


if __name__ == "__main__":
    experiment.main(panel, candidate, description=__doc__, check=check)
