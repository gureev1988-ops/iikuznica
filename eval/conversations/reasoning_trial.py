"""Pinned, blinded first-turn experiment and end-to-end validation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from pathlib import Path
import re
import secrets
from shutil import copyfile
import time

import check
import dialogue_state
import procedure_trial as trial

ask, evidence = trial.ask, trial.evidence
GEMMA = "google/gemma-4-26b-a4b-it"
QWEN = "qwen/qwen3.5-122b-a10b"
ARMS = {
    "A": (GEMMA, "deepinfra/fp8", False),
    "B": (GEMMA, "deepinfra/fp8", True),
    "C": (QWEN, "alibaba", False),
    "D": (QWEN, "alibaba", True),
    "control": (GEMMA, "deepinfra/fp8", False),
}


def settings(body, arm):
    model, provider, enabled = ARMS[arm]
    return dict(body, model=model, max_tokens=8192, reasoning={"enabled": enabled, "exclude": True},
                provider={"only": [provider], "allow_fallbacks": False, "require_parameters": True})


def save(path, data):
    check.save(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def lookup_factory(model_dir=None, python=None):
    corpus = ask.full_corpus()
    cache = {}
    for name in ("search.json.gz", "search-parts.json.gz", "search-procedure-parts.json.gz"):
        packet = json.loads(gzip.decompress((check.HERE / name).read_bytes()))
        if packet["corpus"] != check.fingerprint(list(corpus.values())):
            raise ValueError("Corpus differs from the search snapshot")
        cache.update(packet["inputs"])

    def lookup(q):
        key = check.fingerprint(q)
        if key not in cache and model_dir:
            cache[key] = ask.retrieve(q, model_dir, python)
        return cache[key]

    return corpus, lookup, cache


def expand(q, lookup):
    seeds, _ = lookup(q)

    def extra(sources, pool, corpus):
        candidates = evidence.alternatives(sources, pool, corpus)
        candidates = trial.procedures.augment_sources(candidates, trial.procedures.operation(q), q, [], corpus)
        return trial.expand(candidates, seeds, q, lookup, corpus)[1]

    return extra


def freeze(cases, lookup):
    frozen = {}
    for case in cases:
        q = case["turns"][0]["question"]
        sets = {}
        for arm in ("expanded", "control"):
            class Captured(Exception):
                pass

            def capture(question, sources, instruction):
                sets[arm] = {"sources": sources, "hash": check.fingerprint([question, sources, instruction])}
                raise Captured

            try:
                ask.answer(q, lookup, generate_answer=capture, instruction=trial.COVER, procedure_mode=True,
                           add_sources=expand(q, lookup) if arm == "expanded" else evidence.alternatives)
            except Captured:
                pass
        if set(sets) != {"expanded", "control"}:
            raise ValueError(f"Could not freeze {case['id']}")
        frozen[case["id"]] = dict(question=q, instruction=trial.COVER, **sets)
    return frozen


class Calls:
    def __init__(self, out, cid):
        self.out = out
        self.cid = cid
        self.trace = []
        (out / "calls").mkdir(parents=True, exist_ok=True)

    def generate(self, body, dataset, arm=None):
        if arm:
            body = settings(body, arm)
        metadata = {k: body[k] for k in ("model", "provider", "reasoning", "max_tokens") if k in body}
        return self.record(dataset, body, lambda: {"response": check.complete(self.cid, body, dataset)}, metadata)

    def judge(self, body):
        return self.record("judge", body, lambda: check.decide(body), {"model": body["model"]})

    def record(self, stage, body, fetch, metadata):
        digest = check.fingerprint(body)
        path = self.out / "calls" / f"{self.cid}-{stage}-{digest}.json"
        if not path.exists():
            save(path, fetch())
        item = json.loads(path.read_text())
        response = item["response"]
        data = response if stage != "judge" else item
        self.trace.append(dict(stage=stage, hash=digest, requested=metadata, http=data["http"],
                               finish=data.get("finish"), actual_model=response.get("served_model") or response.get("model"),
                               provider=response.get("provider"), usage=data.get("usage"), seconds=data["latency_s"]))
        if stage != "judge" and not response["ok"]:
            raise RuntimeError(f"{stage}: HTTP {data['http']}, finish={data.get('finish')}")
        return item


def case_run(out, cid, q, lookup, arm, frozen):
    calls = Calls(out / arm, cid)
    first = True

    def generate(question, sources, instruction):
        nonlocal first
        if first:
            first = False
            mode = "control" if arm == "control" else "expanded"
            assert check.fingerprint([question, sources, instruction]) == frozen[cid][mode]["hash"]
        return evidence.generate(question, sources, instruction,
                                 call=lambda _, body, dataset: calls.generate(body, dataset, arm)["response"])

    start = time.monotonic()
    error = None
    try:
        answer = ask.answer(q, lookup, generate_answer=generate, instruction=trial.COVER, procedure_mode=True,
                            add_sources=evidence.alternatives if arm == "control" else expand(q, lookup),
                            judge=lambda body: evidence.judge(body, call=calls.judge))
    except (RuntimeError, OSError, ValueError) as exc:
        answer, error = None, str(exc)
    return dict(case=cid, arm=arm, response=answer, error=error, trace=calls.trace,
                wall_seconds=round(time.monotonic() - start, 3))


def probe(out):
    from variants import make_request

    results = {}
    for arm in "ABCD":
        body = make_request({"question": "Сколько будет 17 умножить на 23? Верни только число.",
                             "sources": []}, ARMS[arm][0], "Ответь кратко.")
        request = settings(body, arm)
        row = check.complete("probe", request, "probe")
        row.pop("text", None)
        results[arm] = {"request": {k: request[k] for k in ("provider", "reasoning", "max_tokens")},
                        "response": row}
    save(out / "probe.json", results)


def experiment(out):
    _, lookup, _ = lookup_factory()
    cases = [c for c in check.load_bank() if c["id"].startswith("P")]
    frozen = freeze(cases, lookup)
    assert len(frozen) == 10
    pin = out / "frozen.json"
    if pin.exists() and json.loads(pin.read_text()) != frozen:
        raise ValueError("The frozen inputs differ; choose a fresh output folder")
    if (out / "blind.json").exists():
        raise ValueError("Results already blinded; keep this directory immutable")
    save(pin, frozen)
    jobs = [(a, c, x["question"]) for a in ARMS for c, x in frozen.items()]
    with ThreadPoolExecutor(max_workers=4) as workers:
        rows = list(workers.map(lambda j: case_run(out, j[1], j[2], lookup, j[0], frozen), jobs))
    secrets.SystemRandom().shuffle(rows)
    save(out / "unblind.json", {i: r["arm"] for i, r in enumerate(rows, 1)})
    save(out / "diagnostics.json", {i: {k: v for k, v in r.items() if k != "response"}
                                    for i, r in enumerate(rows, 1)})
    save(out / "blind.json", [dict(id=i, case=r["case"], response=r["response"])
                              for i, r in enumerate(rows, 1)])


def full_procedure(question):
    if re.search(r"\b(?:отпуск\w*|зарплат\w*|оклад\w*)\b", question, re.I):
        return False
    explicit = re.search(
        r"по шагам|пошагов|порядок действий|в каком порядке|проведи меня|расскажи маршрут|"
        r"опиши путь|какие согласования", question, re.I)
    coordinated_steps = re.search(
        r"\bкак\s+(?:[А-Я]{2,5}\s+)?\w+(?:ть|ти)\b[^?]*(?:,\s*|\bи\s+)(?:где\s+)?\w+(?:ть|ти)\b",
        question, re.I)
    return len(question.split()) >= 12 and bool(explicit or coordinated_steps)


def validate(args):
    if args.reuse_calls:
        dest = args.out / "calls"
        dest.mkdir(exist_ok=True)
        for file in args.reuse_calls.glob("*.json"):
            row = json.loads(file.read_text())
            stage = row["response"].get("dataset") or row["stage"]
            name = dest / f'{row["id"]}-{stage}-{row["request_hash"]}.json'
            if not name.exists():
                copyfile(file, name)
    _, lookup, cache = lookup_factory(args.model_dir, args.python)
    cases = check.load_bank()
    if args.working50:
        from run50 import blind_inputs
        working, _ = blind_inputs(args.working50)
        cases = [{"id": cid, "turns": [{"question": q}]} for cid, (q, _, _) in working.items()]
        cache.update({check.fingerprint(q): [seeds, pool] for q, seeds, pool in working.values()})

    original = ask.answer
    rows = []
    for case in cases:
        past = []
        for i, turn in enumerate(case["turns"], 1):
            cid, q = f'{case["id"]}.{i}', turn["question"]
            calls = Calls(args.out, cid)
            route = False

            def routed(question, search, **kw):
                nonlocal route
                # Route only a request for a multi-step procedure, with no benchmark IDs or labels.
                if full_procedure(q) and full_procedure(question):
                    route = True
                    kw.update(instruction=trial.COVER, add_sources=expand(question, search),
                              generate_answer=lambda q, s, prompt: evidence.generate(
                                  q, s, prompt, call=lambda _, b, d: calls.generate(b, d, args.winner)["response"]))
                return original(question, search, **kw)

            ask.answer = routed
            try:
                answer = dialogue_state.answer(q, lookup, past=past, model=QWEN,
                                               planner=lambda _, b, d: calls.generate(b, d)["response"],
                                               generate_answer=lambda q, s, prompt: evidence.generate(
                                                   q, s, prompt, call=lambda _, b, d: calls.generate(b, d)["response"]),
                                               judge=lambda b: evidence.judge(b, call=calls.judge))
            finally:
                ask.answer = original
            past.append(dict(user=q, assistant=answer["message"], resolved=answer["resolved_question"],
                             action=answer["action"]))
            rows.append(dict(turn=cid, response=answer, routed=route, trace=calls.trace))
            save(args.out / "validation.json", rows)
            print(cid, answer["action"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--winner", choices="ABCD")
    modes.add_argument("--probe", action="store_true")
    parser.add_argument("--working50", type=Path)
    parser.add_argument("--reuse-calls", type=Path, help="Exact request checkpoints from check.py")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--python", default="python3")
    args = parser.parse_args()
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.probe:
        probe(args.out)
    elif args.winner:
        validate(args)
    else:
        experiment(args.out)
