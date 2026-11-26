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


def scoped_result(verdict, current, sources):
    scope = verdict["answers"]["scope"]
    choice, confidence = scope["choice"], scope["confidence"]
    if (scope.get("type") != "choice" or choice not in SCOPE["criteria"] or
            type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("Invalid scope decision")
    if confidence < MIN_SCOPE or choice == "unclear":
        return "clarify", reply("clarify", "unclear")
    if choice != "project":
        return "out_of_scope", reply("out_of_scope", choice)
    route, final = gate(verdict, current, sources)
    if final is None:
        return route, None
    final = threshold(final, verdict)
    if final["action"] in ("escalate", "out_of_scope"):
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


def answer(question, search, previous_question="", generate_answer=generate, judge=decide):
    question = question.strip()
    if not question:
        raise ValueError("Введи вопрос")
    if not previous_question and pay_shortcut(question):
        return reply("out_of_scope", "pay")
    if previous_question:
        question = f"Исходный вопрос: {previous_question}\nУточнение сотрудника: {question}"
    seeds, pool = search(question)
    corpus = full_corpus()
    sources = linked([corpus[tuple(key)] for key in seeds], corpus, index())
    instruction = load_dataset(EVAL / "iikuznitsa_run12/IIkuznitsa_Run_12x3.py")["system"]
    expanded = False
    for attempt in range(2):
        current = generate_answer(question, sources, instruction)
        body = decision_body(question, current, sources, expanded)
        body["questions"] = dict(body["questions"], scope=SCOPE)
        verdict = judge(body)["response"]
        route, final = scoped_result(verdict, current, sources)
        if final is not None:
            return final
        if not attempt and route == "retrieve":
            sources = extended(sources, pool, corpus)
            expanded = True
        instruction = REPAIR
    return reply("escalate", "gap")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--previous-question", default="")
    parser.add_argument("--model-dir", type=Path, help="Pinned MiniLM ONNX model and tokenizer")
    parser.add_argument("--python", default=sys.executable, help="Python with ONNX Runtime and tokenizers")
    args = parser.parse_args()
    def search(question):
        if not args.model_dir or not os.getenv("OPENROUTER_API_KEY"):
            parser.error("Для полного поиска укажи --model-dir и OPENROUTER_API_KEY")
        return retrieve(question, args.model_dir, args.python)
    print(json.dumps(answer(args.question, search, args.previous_question), ensure_ascii=False))
