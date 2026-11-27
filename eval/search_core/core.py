"""Selected A1 core with a pre-publication coverage check for permission questions."""
import json
import sys
from time import sleep
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL / "scope"))

import ask
import dialogue_state
import evidence
import procedures
from run import complete
from variants import GEMMA, make_request

if __package__:
    from . import a1, permission, procedure
    from .faq_policy import RegulationFirstFAQ
else:
    import a1
    import permission
    import procedure
    from faq_policy import RegulationFirstFAQ

QWEN = "qwen/qwen3.5-122b-a10b"


def model_request(case, body, stage):
    for attempt in range(3):
        response = complete(case, body, stage)
        if response["ok"]:
            return response
        transient = (response["http"] in (429, 502, 503, 504) or
                     (response["http"] == 400 and response.get("error") == "Provider returned error"))
        if not transient or attempt == 2:
            raise RuntimeError(f"{stage} failed (HTTP {response['http']})")
        sleep(2 ** (attempt + 1))


def model_settings(body, model, provider, max_tokens=8192):
    return dict(body, model=model, max_tokens=max_tokens, reasoning={"enabled": False, "exclude": True},
                provider={"only": [provider], "allow_fallbacks": False, "require_parameters": True})


def review_permission(state, instruction, *, call=model_request):
    body = make_request({"question": state, "sources": []}, GEMMA, instruction)
    body = model_settings(body, GEMMA, "deepinfra/fp8", max_tokens=2048)
    raw = call("permission", body, "permission-review")["text"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1][:-3]
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def combine_search(search, query, focused):
    initial, pool = search(query)
    extra, extra_pool = search(focused)
    seeds = list(dict.fromkeys(tuple(ref) for ref in [*initial, *extra]))
    merged = {(p["doc"], p["ref"]): p for p in [*extra_pool, *pool]}
    return seeds, list(merged.values())


def answer(question, search, *, past=(), publications=(), scope=None, call=model_request,
           decide=ask.decide, review=None, corpus=None, norms=None):
    corpus = ask.full_corpus() if corpus is None else corpus
    cache = {}
    if review is None:
        def review(state, instruction):
            return review_permission(state, instruction, call=call)

    def lookup(query):
        if query not in cache:
            cache[query] = search(query)
        return cache[query]

    def plan(case, body, stage):
        return call(case, body, stage)

    def respond(query, lookup, **options):
        gate = a1.A1(norms)
        full = procedures.full_procedure(question) and procedures.full_procedure(query)

        def generate(text, sources, prompt):
            def invoke(case, body, stage):
                if full:
                    body = model_settings(body, QWEN, "alibaba")
                return call(case, body, stage)

            return evidence.generate(text, sources, prompt, call=invoke, observe=gate.selection, norms=norms,
                                     order=gate.order if full else None)

        options.update(generate_answer=generate, experiment=gate, corpus=corpus, norms=norms)
        if full:
            def expand(sources, pool, corpus):
                candidates = evidence.alternatives(sources, pool, corpus)
                candidates = procedures.augment_sources(candidates, procedures.operation(query), query, [], corpus)
                seeds, _ = lookup(query)
                return procedure.expand(candidates, seeds, query, lookup, corpus)[1]

            options.update(instruction=procedure.COVER, add_sources=expand)

        initial = ask.answer(query, lookup, **options)

        def retry(focused):
            def search_for_missing(text):
                return combine_search(lookup, text, focused)

            prompt = (options["instruction"] + "\nДай прямой ответ на вопрос о допустимости действия "
                      "в описанной ситуации. Назови обязательные условия и роли, о которых спросили. "
                      "Отрази и остальные подтверждённые части исходного вопроса. "
                      "Пропуски не заполняй догадками. Вопрос для проверки: " + focused)
            return ask.answer(query, search_for_missing, **dict(options, instruction=prompt))

        return permission.check_reply(query, initial, corpus, review, retry)

    result = dialogue_state.answer(question, lookup, past=past, model=QWEN, planner=plan,
                                   judge=lambda body: evidence.judge(body, call=decide), respond=respond, corpus=corpus, norms=norms)
    return RegulationFirstFAQ(publications, scope=scope, corpus=corpus).merge(question, result)
