"""Select genuinely overlapping rule candidates before a conflict review."""
from itertools import combinations
import re


WORD = re.compile(r"[а-яёa-z]{3,}", re.I)
STOP = {"должен", "должна", "должны", "данный", "этом", "быть", "если", "также", "этого", "течение",
        "для", "при", "или", "как", "том", "его", "она", "это", "все", "кто", "который", "после"}
UNIVERSAL = re.compile(r"для всех (?:дирекци|подразделен)|независимо от (?:дирекци|подразделен)", re.I)
CORPUS_SCOPES = {"DB", "IB", "PRE", "TECH"}


def terms(text):
    return {w[:5] for w in WORD.findall(text.casefold().replace("ё", "е")) if w not in STOP}


def competition(question, candidates, cited):
    """Return a bounded set of clause pairs sharing both action and conditions."""
    query = terms(question)
    scored = []
    items = {key: (terms(p["text"]), terms(p.get("section", ""))) for key, p in candidates.items()}
    for left, right in combinations(candidates, 2):
        if left[0] == right[0] or (cited and left not in cited and right not in cited):
            continue
        a, a_heading = items[left]
        b, b_heading = items[right]
        common = a & b
        if not (query & (a | a_heading)) or not (query & (b | b_heading)):
            continue
        overlap = len(common) / max(1, min(len(a), len(b)))
        same_heading = bool(a_heading) and a_heading == b_heading
        if not ((len(common) >= 3 and overlap >= .32) or (len(common) >= 2 and overlap >= .68)
                or (same_heading and len(common) >= 2)):
            continue
        if {left[0], right[0]} <= CORPUS_SCOPES:
            # These four named regulations cover distinct directorates and project phases.
            # Allow direct parallel rules or an explicit cross-directorate scope.
            if (overlap < .78 and not same_heading and
                    not any(UNIVERSAL.search(candidates[k]["text"]) for k in (left, right))):
                continue
        scored.append((overlap + .1 * len(common & query), left, right))
    scored.sort(reverse=True)
    return [(left, right) for _, left, right in scored[:3]]
