"""Source links and publication threshold for the selected answer pipeline."""
import math
import sys
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / "decider"), str(ROOT.parent / "registry")]
from policy import ASK, GEMMA, MESSAGES, MODEL, REPAIR, decision_body, draft, gate, make_request, reference
from registry import compile_records

# One hop, with fair allocation across all five initial search hits.
EXTRA = 15


def index():
    norms = defaultdict(list)
    for row in compile_records():
        norms[reference(row)].append(row)
    return norms


def linked(sources, corpus, norms):
    seed = [reference(p) for p in sources]
    groups = [list(dict.fromkeys((doc, ref) for row in norms.get((doc, clause), ()) for ref in row["related_refs"]))
              for doc, clause in seed]
    keys = dict.fromkeys(seed)
    for level in zip_longest(*groups):
        for key in level:
            if key is not None and key in corpus:
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
