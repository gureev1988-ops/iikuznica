"""Offline diagnostic for source-bound replies. Does not decide what a question requires."""
import json
import re

import judge_pair
import evidence


def check_plan(draft, required_ids, sources):
    """Return verbatim evidence in this draft for each required registry statement."""
    _, allowed = evidence.options(sources)
    if not required_ids or any(key not in allowed for key in required_ids):
        return {"matches_plan": False, "proof": {}}
    expected = evidence.render({"action": "answer", "ids": list(required_ids)}, allowed)
    if draft["action"] != "answer":
        return {"matches_plan": False, "proof": {}}
    citations = draft["sources"]
    required = {(allowed[key]["statement"], evidence.reference(allowed[key])): key for key in required_ids}
    proof = {}
    for block in draft["message"].split("\n\n"):
        part = re.fullmatch(r"(.+) \[([1-9][0-9]*)\]", block, re.S)
        if not part or int(part[2]) > len(citations):
            continue
        ref = citations[int(part[2]) - 1]
        key = required.get((part[1], evidence.reference(ref)))
        if key is not None:
            proof[key] = {"text": part[1], "source": ref}
    return {"matches_plan": all(draft[k] == v for k, v in expected.items()), "proof": proof}


def experiment():
    question, sources, full, short = judge_pair.inputs()
    plan = evidence.SUPPLY_HANDOFF_CLAIMS
    handoff = plan[-1]
    line, = [s for doc, ref, s in judge_pair.fragments(full, sources).values()
             if (doc, ref) == ("PRE", "1.3") and "явно обозначил" in s and "отправляет его АМ." in s]
    wrong = dict(full, message=full["message"].replace("направляет его АМ.", "направляет его SS."))
    results = {}
    for name, draft in (("full", full), ("missing_recipient", short), ("wrong_recipient", wrong)):
        check = check_plan(draft, plan, sources)
        results[name] = {"matches_plan": check["matches_plan"], "handoff_proof": check["proof"].get(handoff)}

    extra = question + " И в какой срок нужно передать ТКП?"
    class NeedsGeneration(Exception):
        pass

    def offline(*args):
        raise NeedsGeneration

    try:
        expanded_draft = evidence.generate(extra, sources, "", call=offline)
    except NeedsGeneration:
        deadline = {"status": "requires_generation"}
    else:
        deadline = {"status": "rendered", "same_draft": expanded_draft == full,
                    "matches_plan": check_plan(expanded_draft, plan, sources)["matches_plan"]}
    return {"source_line": line, "drafts": results, "with_deadline_question": deadline}


if __name__ == "__main__":
    print(json.dumps(experiment(), ensure_ascii=False, indent=2))
