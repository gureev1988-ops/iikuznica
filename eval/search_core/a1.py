"""Selected A1 procedure check from the final paired run."""
import json
import re

import ask
import evidence
from policy import fingerprint

procedures = evidence.procedures

# Narrow applicability: the same branch described in the cited originals.
# These are pilot annotations. An independent reader still needs to approve them.
A1_RULES = (
    (r"техническ\w+ дирекц", r"конкурс.*(?:договор|документац)|договор.*конкурс",
     ("TECH:7.4:1", "TECH:7.5:1", "TECH:7.6:1", "TECH:7.7:1"),
     (("TECH:7.4:1", "TECH:7.5:1"), ("TECH:7.5:1", "TECH:7.6:1"), ("TECH:7.6:1", "TECH:7.7:1"))),
    (r"дбрцт", r"(?:заранее|ранн\w+ старт|до подписания договора|без договора)",
     ("DB:8.4:1",), (("DB:8.5:1", "DB:8.6:1"),)),
    (r"дбрцт", r"выход\w* из зелено\w+ зон\w*",
     ("DB:16.10:1", "DB:16.11:1"), (("DB:16.11:1", "DB:16.10:1"),)),
    (r"техническ\w+ дирекц", r"пересч[её]т\w*.*ткп|ткп.*пересч[её]т",
     ("TECH:6.1:1", "TECH:6.2:1", "TECH:6.8:1", "TECH:6.4:1"),
     (("TECH:6.1:1", "TECH:6.2:1"), ("TECH:6.8:1", "TECH:6.4:1"))),
)

def violations(ids, required, before):
    missing = [key for key in required if key not in ids]
    positions = {key: pos for pos, key in enumerate(ids)}
    misplaced = [(first, second) for first, second in before
                 if first in positions and second in positions and positions[first] >= positions[second]]
    return {"missing": missing, "misplaced": misplaced}


def order_review(question, sources, ids, rules=A1_RULES):
    if not procedures.full_procedure(question):
        return None
    text = procedures.normalized(question)
    for directorate, branch, required, before in rules:
        if not (re.search(directorate, text) and re.search(branch, text)):
            continue
        _, allowed = evidence.options(sources)
        finding = violations(ids, required, before)
        refs = {key for pair in before for key in pair}.union(required)
        # Link the annotation to the registry's verbatim quote and the current original.
        finding["basis"] = {key: fingerprint(allowed[key]["quote"]) for key in sorted(refs & allowed.keys())}
        finding["unavailable"] = sorted(set(required) - allowed.keys())
        return finding
    return None

class A1:
    def __init__(self):
        self.ids = None

    def selection(self, selection):
        self.ids = selection["ids"] if selection["action"] == "answer" else None

    def review(self, q, sources, pool, draft, verdict, route, final, attempt, fact):
        ids, self.ids = self.ids, None
        if not final or final["action"] != "answer" or ids is None:
            return route, final, None, None
        detail = order_review(q, sources, ids)
        if not detail or not any(detail[k] for k in ("missing", "misplaced", "unavailable")):
            return route, final, None, None
        finding = ("Проверь наличие обязательного условия и порядок зависимых шагов: "
                   + json.dumps(detail, ensure_ascii=False))
        if attempt == 0:
            return "revise", None, finding, None
        return "escalate", ask.reply("escalate", "gap"), finding, None
