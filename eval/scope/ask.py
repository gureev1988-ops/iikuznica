"""Run the linked-clause answer pipeline with topic-aware quality-service offers."""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL / "linked35"))
from variants import GEMMA, REPAIR, decision_body, draft, gate, index, linked, make_request, threshold
from policy import extended, full_corpus, load_dataset
from run import complete, decide

SCOPE = {
    "type": "choice",
    "instructions": (
        "Определи цель сотрудника по самому вопросу и контексту диалога. Служба качества разъясняет "
        "пресейл, проектную деятельность, роли, согласования, исполнение и завершение проектов. "
        "Профиль обращения и наличие ответа в найденных пунктах — два разных признака. "
        "Слабая выдача не выводит вопрос из профиля. Совпадение слов с регламентом, слово «регламент», "
        "должность автора или просьба переслать письмо сами по себе не делают его профильным."
    ),
    "criteria": {
        "project": "Вопрос о том, что делать в рабочем процессе пресейла или проекта: например, учёт ФОТ в смете.",
        "casual": "Явно бытовая тема, развлечение или личный карьерный совет, например переговоры о своей зарплате.",
        "other_service": "Конкретный кадровый, личный или иной чувствительный вопрос за пределами процессов проекта.",
        "unclear": "По описанной ситуации нельзя отличить задачу проекта от личной или другого процесса."
    }
}
MIN_SCOPE = 0.70  # Candidate threshold; calibrate on unseen scope questions.
PERSONAL_PAY = re.compile(
    r"(?:а )?как (?:мне )?(?:повысить|поднять|увеличить) "
    r"(?:себе |свою |свой |мой |мне )?(?:зарплату|оклад)(?: себе)?"
)
MESSAGES = {
    "casual": "У меня профессиональная деформация — во всём ищу пункт регламента 🙂 Могу помочь с вопросом про пресейл или проект.",
    "pay": "Если бы оклад рос от одного запроса в чат! 🙂 А я могу подсказать, как устроены пресейл и работа над проектом.",
    "other_service": "Я помогаю с пресейлом и проектной деятельностью. Этот вопрос выходит за их рамки.",
    "unclear": "Это вопрос о порядке работы над проектом или о другой ситуации? Если о проекте, какой шаг нужно выполнить?",
    "gap": "В доступных регламентах я не нашёл подтверждённого ответа на этот вопрос. Передать вопрос в отдел качества?"
}


def reply(action, kind):
    return {"action": action, "message": MESSAGES[kind], "sources": [],
            "offer_quality_request": action == "escalate"}


def pay_shortcut(question):
    words = re.findall(r"[\w-]+", question.lower().replace("ё", "е"))
    return bool({"мне", "себе", "свою", "свой", "мой"}.intersection(words)) and PERSONAL_PAY.fullmatch(" ".join(words)) is not None


def scoped_result(verdict, current, sources, fact=None):
    scope = verdict["answers"]["scope"]
    choice, confidence = scope["choice"], scope["confidence"]
    if (scope.get("type") != "choice" or choice not in SCOPE["criteria"] or
            type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("Invalid scope decision")
    if confidence < MIN_SCOPE or choice == "unclear":
        return "clarify", reply("clarify", "unclear")
    if choice != "project":
        return "out_of_scope", reply("out_of_scope", choice)
    needed = verdict["answers"].get("needed_fact", {}).get("choice")
    if fact and (needed == "company" or (needed != "employee" and not any(fact.matches(s) for s in sources))):
        from procedures import gap_message
        return "escalate", gap_message(fact)
    route, final = gate(verdict, current, sources)
    if final is None:
        return route, None
    final = threshold(final, verdict)
    if final["action"] in ("escalate", "out_of_scope"):
        if fact:
            from procedures import gap_message
            return "escalate", gap_message(fact)
        return "escalate", reply("escalate", "gap")
    return final["action"], dict(final, offer_quality_request=False)


def retrieve(question, model_dir, python):
    with tempfile.TemporaryDirectory() as temp:
        questions, pool, ranks = (Path(temp) / name for name in ("questions", "pool", "ranks"))
        questions.write_text(json.dumps({"id": "request", "question": question}, ensure_ascii=False) + "\n")
        subprocess.run(["node", str(EVAL / "retrieval/search.mjs"), "--questions", str(questions),
                        "--output", str(pool), "--limit", "30"], check=True, stdout=sys.stderr)
        subprocess.run([python, str(EVAL / "rerank/rerank.py"), "--pool", str(pool), "--model-dir",
                        str(model_dir), "--out", str(ranks)], check=True, stdout=sys.stderr)
        result = json.loads(ranks.read_text())
        return result["methods"]["hybrid_condition"], result["pool"]


def generate(question, sources, instruction):
    request = make_request({"question": question, "sources": sources}, GEMMA, instruction)
    row = complete("request", request, "scope")
    if not row["ok"]:
        raise RuntimeError(f"Gemma request failed (HTTP {row['http']})")
    return draft(row["text"])


def answer(question, search, previous_question="", generate_answer=generate, judge=decide,
           add_sources=None, instruction=None, procedure_mode=False, history=(), faq=None, faq_scope=None):
    question = question.strip()
    if not question:
        raise ValueError("Введи вопрос")
    if faq is not None:
        published = faq.lookup(question, faq_scope)
        if published:
            return published
    if not previous_question and not history and pay_shortcut(question):
        return reply("out_of_scope", "pay")
    if previous_question:
        question = f"Исходный вопрос: {previous_question}\nУточнение сотрудника: {question}"
    process, fact = None, None
    if procedure_mode:
        import procedures
        prior = [text for role, text in history if role == "user"]
        if history and (history[-1][0] != "assistant" or not history[-1][1].endswith("?")
                        or re.search(r"новый вопрос|отдельный вопрос|сменим тему", question, re.I)
                        or ("?" in question and (procedures.operation(question) or procedures.requested_fact(question)))):
            prior = []
        if prior:
            question = "\n".join(prior + [question])
        fact = procedures.requested_fact(question)
        process = procedures.operation(question) if fact is None else None
        if process:
            missing = procedures.pending_facets(process, question)
            if missing:
                clarification = procedures.clarify(missing)
                if prior and (sum(role == "assistant" for role, _ in history) >= 2
                              or re.search(r"не знаю|не могу определить", question, re.I)):
                    clarification["message"] = ("Чтобы выбрать применимый регламент, сначала уточни для себя: "
                                                 + clarification["message"][0].lower() + clarification["message"][1:-1] + ".")
                return clarification
    corpus = full_corpus()
    matched_facts = procedures.fact_sources(fact, question, corpus) if fact else []
    if fact and procedures.specific_project_gap(fact, question, matched_facts):
        return procedures.gap_message(fact)
    seeds, pool = search(question)
    sources = linked([corpus[tuple(key)] for key in seeds], corpus, index())
    if add_sources is not None:
        sources = add_sources(sources, pool, corpus)
    if procedure_mode:
        sources = procedures.augment_sources(sources, process, question, matched_facts, corpus)
    selection_mode = instruction is not None
    if instruction is None:
        instruction = load_dataset(EVAL / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")["system"]
    expanded = False
    for attempt in range(2):
        current = generate_answer(question, sources, instruction)
        body = decision_body(question, current, sources, expanded)
        body["questions"] = dict(body["questions"], scope=SCOPE)
        verdict = judge(body)["response"]
        route, final = scoped_result(verdict, current, sources, fact)
        if final is not None:
            return final
        if not attempt and route == "retrieve":
            additional = extended(sources, pool, corpus)
            if procedure_mode:
                sources = procedures.augment_sources(procedures.include_sources(sources, additional),
                                                    process, question, matched_facts, corpus)
            else:
                sources = additional
            expanded = True
        if selection_mode:
            instruction = ("Пересмотри выбор с учётом предыдущего черновика и решения проверяющего. "
                           + json.dumps({"draft": current, "verdict": verdict["answers"]}, ensure_ascii=False))
        else:
            instruction = REPAIR
    return procedures.gap_message(fact) if fact else reply("escalate", "gap")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--previous-question", default="")
    parser.add_argument("--model-dir", type=Path, help="Pinned MiniLM ONNX model and tokenizer")
    parser.add_argument("--python", default=sys.executable, help="Python with ONNX Runtime and tokenizers")
    parser.add_argument("--evidence", action="store_true", help="Select source-bound registry statements")
    parser.add_argument("--model", default=GEMMA, help="OpenRouter generator ID for evidence mode")
    parser.add_argument("--faq-file", type=Path, help="Approved publications exported by the quality service")
    parser.add_argument("--faq-scope", help="Trusted scope of this conversation")
    args = parser.parse_args()
    def search(question):
        if not args.model_dir or not os.getenv("OPENROUTER_API_KEY"):
            parser.error("Для полного поиска укажи --model-dir и OPENROUTER_API_KEY")
        return retrieve(question, args.model_dir, args.python)
    kwargs = {}
    if args.evidence:
        import evidence
        kwargs = dict(generate_answer=lambda *a: evidence.generate(*a, model=args.model), judge=evidence.judge,
                      add_sources=evidence.alternatives, instruction="", procedure_mode=True)
    if args.faq_file:
        from quality import ApprovedFAQ
        kwargs.update(faq=ApprovedFAQ.from_file(args.faq_file), faq_scope=args.faq_scope)
    print(json.dumps(answer(args.question, search, args.previous_question, **kwargs), ensure_ascii=False))
