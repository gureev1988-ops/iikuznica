"""Procedure retrieval, conversation-context probes and live replay on the working bank."""
import argparse
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
EVAL = HERE.parent
sys.path.insert(0, str(EVAL / "scope"))
import ask
import evidence
from procedures import section_neighbors
from policy import fingerprint, rows
from run import cached_call, complete, decide, save


def load_bank():
    corpus = ask.full_corpus()
    cases = [case for group in ("procedures", "dialogues", "compound")
             for case in rows(HERE / (group + ".jsonl"))]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Repeated case ID")
    for case in cases:
        for turn in case["turns"]:
            for part in turn["expect"]:
                for ref, description in part["steps"]:
                    if tuple(ref.split()) not in corpus or not description:
                        raise ValueError(f"Invalid reference in {case['id']}: {ref}")
    return cases


def independent_parts(cases):
    """Controlled ablation: the fixture tells us that the two questions are independent."""
    singles = []
    for case in cases:
        turn = case["turns"][0]
        if turn.get("relation") != "independent":
            continue
        first, mark, second = turn["question"].partition("?")
        if not mark or not first or not second.strip().endswith("?") or second.count("?") != 1:
            raise ValueError(f"Expected two explicit questions in {case['id']}")
        second = re.sub(r"^(?:и ещё,? |и еще,? |и |а |заодно )", "", second.strip(), count=1, flags=re.I)
        for suffix, question in (("a", first + mark), ("b", second)):
            singles.append({"id": case["id"] + suffix, "turns": [{"question": question}]})
    return singles


class Boundary(Exception):
    """Stop at an observed pipeline boundary before a paid model call."""


def history_fixture(prior, punctuation):
    return [entry for q in prior for entry in (("user", q), ("assistant", punctuation))]


def input_trace(question, history):
    def search(effective):
        raise Boundary(effective)
    try:
        result = ask.answer(question, search, procedure_mode=True, history=history)
    except Boundary as stop:
        return {"query": stop.args[0]}
    return {"local": result}


def make_cache(cases, python, model_dir, output):
    queries = {}
    for case in cases:
        prior = []
        for turn in case["turns"]:
            q = turn["question"]
            # The baseline consults previous assistant punctuation, never its wording.
            for punctuation in (".", "?"):
                trace = input_trace(q, history_fixture(prior, punctuation))
                if "query" in trace:
                    queries.setdefault(trace["query"], str(len(queries)))
            prior.append(q)
    with TemporaryDirectory() as tmp:
        inputs, pool, ranks = [Path(tmp) / name for name in ("questions", "pool", "ranks")]
        inputs.write_text("".join(json.dumps(dict(id=i, question=q), ensure_ascii=False) + "\n"
                                  for q, i in queries.items()))
        subprocess.run(["node", str(EVAL / "retrieval/search.mjs"), "--questions", str(inputs),
                        "--output", str(pool), "--limit", "30"], check=True)
        subprocess.run([python, str(EVAL / "rerank/rerank.py"), "--model-dir", model_dir,
                        "--pool", str(pool), "--out", str(ranks)], check=True)
        ranks_by_id = {r["id"]: r for r in rows(ranks)}
        cache = {fingerprint(q): [ranks_by_id[i]["methods"]["hybrid_condition"], ranks_by_id[i]["pool"]]
                 for q, i in queries.items()}
    payload = {"corpus": fingerprint(list(ask.full_corpus().values())), "inputs": cache}
    output.write_bytes(gzip.compress(json.dumps(payload, sort_keys=True).encode(), mtime=0))


def retrieval_probe(cases, lookup):
    corpus = ask.full_corpus()
    report = []
    for case in (c for c in cases if c["id"].startswith("P")):
        turn = case["turns"][0]
        captured = {}
        def capture(q, sources, instruction):
            seeds, _ = lookup(q)
            enlarged = section_neighbors(sources, seeds, corpus)
            needed = {k for part in turn["expect"] for k, _ in part["steps"]}
            for key, refs in (("top5", [tuple(r) for r in seeds]),
                              ("current", [evidence.reference(p) for p in sources]),
                              ("sections", [evidence.reference(p) for p in enlarged])):
                have = {" ".join(r) for r in refs}
                captured[key] = {"n": len(refs), "missing": sorted(needed - have)}
            raise Boundary
        try:
            reply = ask.answer(turn["question"], lookup, procedure_mode=True, history=(),
                               generate_answer=capture, add_sources=evidence.alternatives, instruction="")
        except Boundary:
            pass
        else:
            captured["local"] = reply
        report.append(dict(id=case["id"], **captured))
    return report


def probe(cases, lookup):
    traces = {}
    for case in cases:
        prior = []
        for i, turn in enumerate(case["turns"], 1):
            prev_asked = i > 1 and any(p["action"] == "clarify" for p in case["turns"][i-2]["expect"])
            trace = input_trace(turn["question"], history_fixture(prior, "?" if prev_asked else "."))
            traces[f"{case['id']}.{i}"] = trace
            prior.append(turn["question"])
    followups = []
    for case in (c for c in cases if c["id"].startswith("P")):
        start, follow = case["turns"]
        needed = {ref for part in follow["expect"] for ref, _ in part["steps"]}
        comparison = {"id": case["id"]}
        # A controlled retrieval comparison on known continuations only. The
        # classifier must independently decide when this context applies in use.
        for label, q in (("alone", follow["question"]),
                         ("with_prior", start["question"] + "\n" + follow["question"])):
            seeds, _ = lookup(q)
            comparison[label] = sorted(needed - {" ".join(r) for r in seeds})
        followups.append(comparison)
    return {"retrieval": retrieval_probe(cases, lookup), "routing": traces, "followups_top5": followups}


def live(cases, lookup, out, *, candidate=False):
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("The live run requires OPENROUTER_API_KEY")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out / "calls").mkdir(exist_ok=True)
    (out / ".gitignore").write_text("*\n")
    records = []
    for case in cases:
        history = []
        for index, turn in enumerate(case["turns"], 1):
            cid = f"{case['id']}.{index}"
            def generation(_, body, dataset):
                def fetch():
                    result = complete(cid, body, dataset)
                    if not result["ok"]:
                        raise RuntimeError(f"{cid} generator HTTP {result['http']}")
                    return {"response": result}
                stage = "planner" if dataset == "conversation-plan" else "generator"
                return cached_call(out, cid, stage, body, fetch, lambda r: r["text"])["response"]
            def judge(body):
                return cached_call(out, cid, "judge", body, lambda: decide(body), lambda r: r["answers"])
            # Only user turns, real prior replies and actual search results enter the model.
            options = dict(history=history, generate_answer=lambda *a: evidence.generate(*a, call=generation),
                           judge=lambda b: evidence.judge(b, call=judge))
            if candidate:
                import conversation
                response = conversation.answer(turn["question"], lookup, planner=generation, **options)
            else:
                response = ask.answer(turn["question"], lookup, procedure_mode=True, instruction="",
                                      add_sources=evidence.alternatives, **options)
            history.extend([("user", turn["question"]), ("assistant", response["message"])])
            records.append({"turn": cid, "question": turn["question"], "response": response})
            save(out / "replies.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
            print(cid, response["action"], flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    modes = cli.add_subparsers(dest="command", required=True)
    prep = modes.add_parser("prepare")
    prep.add_argument("--model-dir", required=True)
    prep.add_argument("--python", default=sys.executable)
    prep.add_argument("--cache", type=Path)
    prep.add_argument("--independent-parts", action="store_true")
    for name in ("probe", "live", "candidate"):
        mode = modes.add_parser(name)
        mode.add_argument("--cache", type=Path)
        if name == "live":
            mode.add_argument("--independent-parts", action="store_true")
        if name == "candidate":
            mode.add_argument("--working50", type=Path, help="Private packet with the original working bank")
            mode.add_argument("--model-dir", type=Path, help="Pinned MiniLM for newly contextualized queries")
            mode.add_argument("--python", default=sys.executable)
        mode.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    cases = load_bank()
    parts = getattr(args, "independent_parts", False)
    if parts:
        cases = independent_parts(cases)
    if args.cache is None:
        args.cache = HERE / ("search-parts.json.gz" if parts else "search.json.gz")
    if args.command == "prepare":
        make_cache(cases, args.python, args.model_dir, args.cache)
    else:
        cache = json.loads(gzip.decompress(args.cache.read_bytes()))
        if cache["corpus"] != fingerprint(list(ask.full_corpus().values())):
            cli.error("Corpus changed; regenerate the search snapshot")
        if args.command == "candidate":
            extra = json.loads(gzip.decompress((HERE / "search-parts.json.gz").read_bytes()))
            if extra["corpus"] != cache["corpus"]:
                cli.error("Incompatible independent-question snapshot")
            cache["inputs"].update(extra["inputs"])
            if args.working50:
                from run50 import blind_inputs
                working, _ = blind_inputs(args.working50)
                cases = []
                for cid, (q, seeds, pool) in working.items():
                    cases.append({"id": cid, "turns": [{"question": q}]})
                    cache["inputs"][fingerprint(q)] = [seeds, pool]
        def lookup(q):
            key = fingerprint(q)
            if key not in cache["inputs"] and args.command == "candidate" and args.model_dir:
                cache["inputs"][key] = ask.retrieve(q, args.model_dir, args.python)
            return cache["inputs"][key]
        if args.command == "probe":
            save(args.out, json.dumps(probe(cases, lookup), ensure_ascii=False, indent=2))
        else:
            live(cases, lookup, args.out, candidate=args.command == "candidate")
