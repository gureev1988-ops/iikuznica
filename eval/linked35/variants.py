"""Experiment policies. The question IDs and scoring labels are unavailable here."""
import json
import math
import sys
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / "decider"), str(ROOT.parent / "registry")]
from policy import ASK, GEMMA, MESSAGES, MODEL, REPAIR, decision_body, draft, gate, make_request, reference
from registry import compile_records
from run import cached_call, complete, decide

# One hop, with fair allocation across all five initial search hits.
EXTRA = 15
BATCH = 24
LABELS = {
    "missing": "Норма применима, и пропуск или искажение её условия изменяет нужное сотруднику действие или срок.",
    "covered": "Норма применима. Ответ уже верно учитывает существенное для этого вопроса условие, в том числе словами с тем же смыслом.",
    "irrelevant": "Для ответа на этот вопрос норма не требуется. Она относится к другой ситуации, нерелевантной стадии или не влияет на запрошенное действие.",
}


def index():
    norms = defaultdict(list)
    for row in compile_records():
        norms[reference(row)].append(row)
    return norms


def linked(sources, corpus, norms):
    seed = [reference(p) for p in sources]
    groups = [list(dict.fromkeys((doc, ref) for row in norms[(doc, clause)] for ref in row["related_refs"]))
              for doc, clause in seed]
    keys = dict.fromkeys(seed)
    for level in zip_longest(*groups):
        for key in level:
            if key is not None:
                keys[key] = None
    return [corpus[k] for k in list(keys)[:len(seed) + EXTRA]]


def threshold(final, verdict):
    if final["action"] != "answer":
        return final
    route = verdict["answers"]["route"]
    confidence = route["confidence"]
    if route["choice"] != "answer" or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Missing or invalid confidence for published answer")
    if confidence >= 0.5:
        return final
    scores = route["probabilities"]
    options = ("clarify", "retrieve", "escalate", "out_of_scope")
    if any(not math.isfinite(scores[k]) or not 0 <= scores[k] <= 1 for k in options):
        raise ValueError("Invalid alternative route probabilities")
    alternative = max(options, key=scores.__getitem__)
    missing = verdict["answers"]["missing"]["choice"]
    if alternative == "clarify" and missing != "other":
        return {"action": "clarify", "message": ASK[missing], "sources": []}
    # This rule runs at publication time. It never loops or drops a borderline request.
    return {"action": "escalate", "message": MESSAGES["escalate"], "sources": []}


def generation(out, cid, stage, case, system):
    body = make_request(case, GEMMA, system)

    def call():
        row = complete(cid, body, "linked35")
        if not row["ok"]:
            raise RuntimeError(f"{cid} {stage}: HTTP {row['http']} {row.get('error', 'incomplete response')}")
        return {"response": row, "http": row["http"], "usage": row["usage"], "latency_s": row["latency_s"]}

    return cached_call(out, cid, stage, body, call, lambda r: draft(r["text"]))


def check_body(question, answer, group):
    # Every indexed statement is shown with the exact source text. Multiple statements
    # may refer to the same paragraph, so the source text is included only once.
    sources = {r["doc"] + ":" + r["ref"]: r["quote"] for r in group}
    state = {"question": question, "answer": answer, "originals": sources}
    checks = {}
    for i, r in enumerate(group):
        checks[f"rule_{i}"] = {"type": "choice", "criteria": LABELS,
                               "instructions": f"Проверь только утверждение «{r['statement']}» из {r['doc']} {r['ref']}. "
                                               "Сверь с оригиналом. Учитывай, о какой дирекции, операции и этапе спросил сотрудник. "
                                               "Если он спрашивает об условиях действия в целом, существенные исключения и обязательные "
                                               "согласования применимы. При иной теме не требуй пересказа всей процедуры."}
    return {"model": MODEL, "state": json.dumps(state, ensure_ascii=False), "questions": checks}


def audited_rules(out, case, answer, norms, phase):
    rules = [r for p in case["sources"] for r in norms[reference(p)]]
    missing, calls = [], []
    for start in range(0, len(rules), BATCH):
        group = rules[start:start + BATCH]
        body = check_body(case["question"], answer, group)

        def validate(result):
            decisions = result["answers"]
            if any(decisions[k]["type"] != "choice" or decisions[k]["choice"] not in LABELS for k in body["questions"]):
                raise ValueError("Incomplete per-rule audit")

        row = cached_call(out, case["id"], f"audit{phase}_{start // BATCH}", body, lambda: decide(body), validate)
        calls.append(row)
        for i, r in enumerate(group):
            if row["response"]["answers"][f"rule_{i}"]["choice"] == "missing":
                missing.append({k: r[k] for k in ("id", "doc", "ref", "statement")})
    return missing, calls


def condition_arm(out, case, final, last_judge, norms, expanded):
    calls, flags = [], []
    if final["action"] != "answer":
        return final, last_judge, calls, flags
    missing, calls = audited_rules(out, case, final, norms, 1)
    flags.append(missing)
    if not missing:
        return final, last_judge, calls, flags
    system = (REPAIR + " Проверь замечания к предыдущему черновику по оригиналам. Перенеси только применимые "
              "условия и явно назови обязательные согласования.\n" + json.dumps(
                  {"previous_answer": final, "review": missing}, ensure_ascii=False))
    row = generation(out, case["id"], "condition_repair", case, system)
    calls.append(row)
    revised = draft(row["response"]["text"])
    body = decision_body(case["question"], revised, case["sources"], expanded)
    row = cached_call(out, case["id"], "condition_route", body, lambda: decide(body),
                      lambda result: gate(result, revised, case["sources"]))
    calls.append(row)
    last_judge = row["response"]
    verdict, final = gate(last_judge, revised, case["sources"])
    if verdict == "publish":
        still_missing, more_calls = audited_rules(out, case, final, norms, 2)
        calls.extend(more_calls)
        flags.append(still_missing)
        if still_missing:
            final = None
    if final is None:
        final = {"action": "escalate", "message": MESSAGES["escalate"], "sources": []}
    return final, last_judge, calls, flags
