"""Pinned, blinded first-turn experiment and end-to-end validation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from pathlib import Path
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
        item = json.loads(path.read_text()) if path.exists() else None
        if item is None or (stage != "judge" and not item["response"]["ok"]):
            item = fetch()
            if stage == "judge" or item["response"]["ok"]:
                save(path, item)
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


def reuse_calls(args):
    if args.reuse_calls:
        dest = args.out / "calls"
        dest.mkdir(exist_ok=True)
        for file in args.reuse_calls.glob("*.json"):
            row = json.loads(file.read_text())
            stage = row["response"].get("dataset") or row["stage"]
            name = dest / f'{row["id"]}-{stage}-{row["request_hash"]}.json'
            if not name.exists():
                copyfile(file, name)


def run_turn(out, cid, q, past, lookup, winner, *, diagnose=False):
    calls = Calls(out, cid)
    original = ask.answer
    routed_procedure = False
    details = {"resolution": [], "search": [], "generator_sources": [], "verdicts": []}

    def search(question):
        seeds, pool = lookup(question)
        if diagnose:
            details["search"].append({"question": question, "seeds": seeds})
        return seeds, pool

    def generate(question, sources, prompt, arm=None):
        if diagnose:
            details["generator_sources"].append([[source["doc"], source["ref"]] for source in sources])
        return evidence.generate(question, sources, prompt,
                                 call=lambda _, b, d: calls.generate(b, d, arm)["response"])

    def plan(_, body, dataset):
        response = calls.generate(body, dataset)["response"]
        if diagnose:
            details["resolution"].append(response["text"])
        return response

    def judge(body):
        result = calls.judge(body)
        if diagnose:
            details["verdicts"].append(result["response"]["answers"])
        return result

    def routed(question, lookup, **kw):
        nonlocal routed_procedure
        # Both the employee's request and its resolved form must ask for a full procedure.
        if trial.procedures.full_procedure(q) and trial.procedures.full_procedure(question):
            routed_procedure = True
            kw.update(instruction=trial.COVER, add_sources=expand(question, lookup),
                      generate_answer=lambda q, s, prompt: generate(q, s, prompt, winner))
        return original(question, lookup, **kw)

    ask.answer = routed
    try:
        answer = dialogue_state.answer(q, search if diagnose else lookup, past=past, model=QWEN,
                                       planner=plan, generate_answer=generate,
                                       judge=lambda body: evidence.judge(body, call=judge))
    finally:
        ask.answer = original
    row = dict(turn=cid, response=answer, routed=routed_procedure, trace=calls.trace)
    if diagnose:
        details["directorate_documents"] = sorted(trial.procedures.directories(answer["resolved_question"]))
        row["diagnostics"] = details
    return row


def prior_turn(question, response):
    return dict(user=question, assistant=response["message"], resolved=response["resolved_question"],
                action=response["action"])


def compare_histories(args):
    def load_replies(path):
        text = path.read_text()
        rows = [json.loads(line) for line in text.splitlines()] if path.suffix == ".jsonl" else json.loads(text)
        return {row["turn"]: row["response"] for row in rows}

    replies = [load_replies(path) for path in args.compare_histories]
    cases = {case["id"]: case for case in check.load_bank() if case["id"].startswith("P")}
    unknown = set(args.case or ()) - cases.keys()
    if unknown:
        raise ValueError(f"Unknown procedure IDs: {sorted(unknown)}")
    _, lookup, _ = lookup_factory(args.model_dir, args.python)
    rows = {}
    for cid in args.case or cases:
        first, second = cases[cid]["turns"]
        rows[cid] = pair = {}
        for label, history in zip(("baseline", "candidate"), replies):
            previous = prior_turn(first["question"], history[f"{cid}.1"])
            pair[label] = run_turn(args.out, f"{cid}.2", second["question"], [previous],
                                   lookup, "C", diagnose=True)
        save(args.out / "history-comparison.json", rows)
    print(f"Compared {len(rows)} procedure continuations with both recorded histories.", flush=True)


def validate(args):
    _, lookup, cache = lookup_factory(args.model_dir, args.python)
    cases = check.load_bank()
    if args.working50:
        from run50 import blind_inputs
        working, _ = blind_inputs(args.working50)
        cases = [{"id": cid, "turns": [{"question": q}]} for cid, (q, _, _) in working.items()]
        cache.update({check.fingerprint(q): [seeds, pool] for q, seeds, pool in working.values()})

    rows = []
    for case in cases:
        past = []
        for i, turn in enumerate(case["turns"], 1):
            cid, q = f'{case["id"]}.{i}', turn["question"]
            row = run_turn(args.out, cid, q, past, lookup, args.winner)
            answer = row["response"]
            past.append(prior_turn(q, answer))
            rows.append(row)
            save(args.out / "validation.json", rows)
            print(cid, answer["action"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--winner", choices="ABCD")
    modes.add_argument("--probe", action="store_true")
    modes.add_argument("--compare-histories", nargs=2, type=Path, metavar=("BASELINE", "CANDIDATE"),
                       help="Replay second turns with both real first-response histories on this kernel")
    parser.add_argument("--case", action="append", help="Restrict the crossed history replay to a procedure ID")
    parser.add_argument("--working50", type=Path)
    parser.add_argument("--reuse-calls", type=Path, help="Exact request checkpoints from check.py")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--python", default="python3")
    args = parser.parse_args()
    if args.case and not args.compare_histories:
        parser.error("--case requires --compare-histories")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.winner or args.compare_histories:
        reuse_calls(args)
    if args.probe:
        probe(args.out)
    elif args.compare_histories:
        compare_histories(args)
    elif args.winner:
        validate(args)
    else:
        experiment(args.out)
