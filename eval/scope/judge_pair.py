"""A blinded two-draft diagnostic using the public Q04 and the existing Decisions client."""
import argparse
import json
from pathlib import Path

import ask
import evidence
from policy import MODEL, fingerprint, rows
from run import cached_call, decide, save

HERE = Path(__file__).resolve().parent
GAPS = {
    "action": "Отсутствует само требуемое действие или его результат.",
    "actor": "Не указан требуемый исполнитель.",
    "recipient": "Не указан требуемый получатель результата.",
    "condition": "Упущено применимое условие, согласование или исключение.",
    "time": "Упущен применимый срок.",
    "other": "Существенный пропуск другого вида.",
    "none": "Ответ покрывает все существенные просьбы сотрудника.",
}


def inputs():
    question = next(r["question"] for r in rows(ask.EVAL / "rerank/questions35.jsonl") if r["id"] == "Q04")
    rank = next(r for r in rows(ask.EVAL / "rerank/rankings.jsonl") if r["id"] == "Q04")
    captured = {}

    class Captured(Exception):
        pass

    def capture(body):
        captured.update(json.loads(body["state"]))
        raise Captured

    def unexpected_call(*args):
        raise RuntimeError("The pinned complete draft must be recoverable without generation")

    try:
        ask.answer(question, lambda _: (rank["methods"]["hybrid_condition"], rank["pool"]),
                   generate_answer=lambda *args: evidence.generate(*args, call=unexpected_call),
                   judge=capture, add_sources=evidence.alternatives, instruction="", procedure_mode=True)
    except Captured:
        pass
    original = captured["draft"]
    old, new = "направляет его АМ.", "направляет его."
    if original["action"] != "answer" or original["message"].count(old) != 1:
        raise ValueError("Unexpected complete answer; do not silently change the manipulation")
    shortened = dict(original, message=original["message"].replace(old, new))
    return question, captured["sources"], original, shortened


def fragments(draft, sources):
    cited = {(s["doc"], s["ref"]) for s in draft["sources"]}
    result = {}
    for source in sources:
        if (source["doc"], source["ref"]) in cited:
            for line in source["text"].splitlines():
                if line.strip():
                    result[f"p{len(result) + 1}"] = (source["doc"], source["ref"], line.strip())
    return result


def request_for(question, draft, sources, spans):
    quotes = {k: f"{doc} §{ref} — {line}" for k, (doc, ref, line) in spans.items()}
    choices = dict(quotes, none="Оснований для замечания нет.", elsewhere="Основание вне перечисленных строк.")
    return {
        "model": MODEL,
        "state": json.dumps(dict(employee_question=question, draft=draft, sources=sources), ensure_ascii=False),
        "questions": {
            "gap": {"type": "choice", "criteria": GAPS, "instructions":
                    "Сопоставь каждую просьбу сотрудника с текстом ответа и применимой ветвью регламента. "
                    "Выбери один существенный пропуск. Если все просьбы закрыты, выбери none."},
            "basis": {"type": "choice", "criteria": choices, "instructions":
                      "Если ответ упускает требование, выбери точную строку применимой ветви, которая "
                      "устанавливает пропущенное требование. Для полного ответа выбери none."}
        }
    }


def verdict(data, body):
    chosen = {}
    for key, spec in body["questions"].items():
        value = data["answers"][key]
        if value.get("type") != "choice" or value.get("choice") not in spec["criteria"]:
            raise ValueError(f"Invalid decision for {key}")
        chosen[key] = value["choice"]
    return chosen


def experiment(out, check=False):
    q, sources, full, short = inputs()
    spans = fragments(full, sources)
    target, = [k for k, (doc, ref, line) in spans.items()
               if (doc, ref) == ("PRE", "1.3") and "явно обозначил" in line and "отправляет его АМ" in line]
    cases = [(full, {"gap": "none", "basis": "none"}), (short, {"gap": "recipient", "basis": target})]
    # Content hashes choose a reproducible order. No case IDs, expected labels or other draft enter the request.
    cases.sort(key=lambda item: fingerprint(item[0]["message"]))
    if check:
        return {"cases": len(cases), "source_count": len(sources), "cited_fragments": len(spans),
                "target": list(spans[target]), "mode": "fixture only"}
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out / ".gitignore").write_text("*\n")
    (out / "calls").mkdir(exist_ok=True)
    results = []
    for draft, expected in cases:
        body = request_for(q, draft, sources, spans)
        req_hash = fingerprint(body)
        name = out / "calls" / f"pair-judge-{req_hash}.json"
        cached = name.exists()
        rec = cached_call(out, "pair", "judge", body, lambda: decide(body), lambda data: verdict(data, body))
        actual = verdict(rec["response"], body)
        results.append(dict(draft="full" if draft == full else "recipient_removed", verdict=actual,
                            quote=spans.get(actual["basis"]), passed=actual == expected,
                            confidence={k: v.get("confidence") for k, v in rec["response"]["answers"].items()},
                            request_hash=req_hash, reused=cached, seconds=rec["latency_s"], usd=rec["usage"].get("cost")))
    report = {"model": MODEL, "passed": sum(r["passed"] for r in results),
              "total": len(results), "results": results}
    save(out / "summary.json", json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify actual drafts and gold span without any API calls")
    parser.add_argument("--out", type=Path, default=Path("judge-pair-private"))
    args = parser.parse_args()
    print(json.dumps(experiment(args.out, args.check), ensure_ascii=False, indent=2))
