"""Run the same 132 turns on A1 and A1 plus the verified automatic partial-answer route."""
import argparse
from hashlib import file_digest, sha256
from io import BytesIO
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import subprocess
from zipfile import ZipFile

import reasoning_trial as control
from answer_quality_trial import blind_bank, frozen_bank, pin
from check import fingerprint
from final_faq_policy import RegulationFirstFAQ, check_policy
from final_parts_retest import candidates
from final_quality_trial import metrics, pairs, run_arm, visible

WORKING_SHA = "deec61d90eca3ad3cf199823e0904c270fd7ee6995bdc434b8d5a94fc2595914"
BLIND_INPUT_SHA = "4280eaadc2a1db52b7c679664442facb56be6d6bdae281117cbce649c0622f54"
FINAL_ZIP_SHA = "f3dffeab4539303e5363f4b411b8827a5bb295ef201c323dd03474f915828ce2"
FINAL_INPUT_NAME = "IIkuznitsa_Final_Control20_Inputs.zip"
MODEL_SHA = {
    "model_qint8_arm64.onnx": "1825907d6c1a9001ff78124780bbde20a614a8c3df3b63409cf3c72c6fe5c8b4",
    "tokenizer.json": "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626",
}
COUNTS = {"working42": 42, "working50": 50, "blind20": 20, "final-control20": 20}
ARMS = ("a1", "a1-f1b-auto")
AUTO = candidates({})


def digest(data):
    return sha256(data).hexdigest()


def input_only(archive, *, final=False):
    packet = archive.read_bytes()
    with ZipFile(BytesIO(packet)) as top:
        if final and FINAL_INPUT_NAME in top.namelist():
            packet = top.read(FINAL_INPUT_NAME)
    if final and digest(packet) != FINAL_ZIP_SHA:
        raise ValueError("The new input-only archive differs from the one pinned in PR #32")
    with ZipFile(BytesIO(packet)) as source:
        if set(source.namelist()) - {"blind/inputs.json", "SHA256SUMS", "README.md", "blind/"}:
            raise ValueError("Use an inputs-only archive. Do not pass reviewer files.")
        inner = digest(source.read("blind/inputs.json"))
    if not final and inner != BLIND_INPUT_SHA:
        raise ValueError("The original Blind20 input checksum changed")
    return blind_bank(BytesIO(packet)), {"archive": digest(packet), "inputs": inner}


def review_reply(reply):
    result = visible(reply)
    if result.get("requests"):
        result["requests"] = [{"text": part["text"], "result": review_reply(part["result"])}
                              for part in result["requests"]]
    return result


def inputs(args, cache):
    if digest(args.working50.read_bytes()) != WORKING_SHA:
        raise ValueError("The original 50-question archive differs from the pinned working bank")
    old, old_pin = input_only(args.blind20)
    new, new_pin = input_only(args.final_control20, final=True)
    suites = {"working42": frozen_bank(None, cache),
              "working50": frozen_bank(args.working50, cache), "blind20": old, "final-control20": new}
    for name, cases in suites.items():
        if (sum(len(c["turns"]) for c in cases) != COUNTS[name]
                or len({c["id"] for c in cases}) != len(cases)):
            raise ValueError(f"{name} has an incorrect or ambiguous case set")
    pins = {"working50_archive": WORKING_SHA, "blind20": old_pin, "final-control20": new_pin,
            "suites": {k: fingerprint(cases) for k, cases in suites.items()}}
    return suites, pins


def files_for_review(name, target, data, seed):
    rng = random.Random(digest(f"{seed}:{name}".encode()))
    a1 = {row["turn"]: row for row in data["a1"]}
    auto = {row["turn"]: row for row in data["a1-f1b-auto"]}
    if set(a1) != set(auto):
        raise ValueError(f"Incomplete paired output for {name}")
    packet, mapping = [], []
    for turn, row in a1.items():
        variants = [("a1", row), ("a1-f1b-auto", auto[turn])]
        rng.shuffle(variants)
        choices = {}
        for i, (arm, result) in enumerate(variants, 1):
            label = str(i)
            choices[label] = review_reply(result["response"])
            choices[label]["technical_error"] = bool(result.get("error"))
            mapping.append({"turn": turn, "label": label, "arm": arm})
        past = [{"role": role, "content": part[key]} for part in row["past"]
                for role, key in (("user", "user"), ("assistant", "assistant"))]
        packet.append({"turn": turn, "question": row["question"], "history": past, "responses": choices})
    control.save(target / "review.json", packet)
    control.save(target / "private-arm-map.json", mapping)


def auto_only(_, question, past, row, trial):
    return AUTO("auto", question, past, row, trial)


def run_suite(name, cases, lookup, out, policy, seed, independent):
    target = out / name
    target.mkdir(mode=0o700, exist_ok=True)
    data = {}
    for arm in ARMS:
        reuse = target / "a1" if not independent and arm != "a1" else None
        data[arm] = run_arm(arm, None if arm == "a1" else auto_only, cases, lookup,
                            target / arm, reuse, postprocess=policy.merge)
    comparison = pairs(data, cases)
    if name in ("blind20", "final-control20"):
        files_for_review(name, target, data, seed)
    else:
        control.save(target / "pairs.json", comparison)
    summary = {"turns_per_arm": COUNTS[name],
               "changed_visible": sum(p["arms"][ARMS[0]] != p["arms"][ARMS[1]] for p in comparison),
               "arms": {arm: metrics(rows) for arm, rows in data.items()}}
    control.save(target / "summary.json", summary)
    return summary


def validate_models(path):
    for name, expected in MODEL_SHA.items():
        with (path / name).open("rb") as stream:
            if file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"{name} differs from the pinned MiniLM revision")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working50", type=Path, required=True)
    parser.add_argument("--blind20", type=Path, required=True, help="Original inputs-only archive")
    parser.add_argument("--final-control20", type=Path, required=True, help="New input-only archive or project bundle")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--python", default="python3")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true", help="Validate archives and FAQ policy without model calls")
    parser.add_argument("--independent-calls", action="store_true", help="Disable cross-arm exact cache reuse")
    parser.add_argument("--faq-snapshot", type=Path)
    parser.add_argument("--faq-scope")
    args = parser.parse_args()
    if bool(args.faq_snapshot) != bool(args.faq_scope):
        parser.error("--faq-snapshot and --faq-scope must be supplied together")
    if not args.check and (not args.model_dir or not args.out or not shutil.which(args.python)):
        parser.error("Live runs need --model-dir, --out and the ranking Python")
    if args.model_dir:
        validate_models(args.model_dir)
    corpus, lookup, cache = control.lookup_factory(args.model_dir, args.python)
    suites, pins = inputs(args, cache)
    faq_raw = args.faq_snapshot.read_bytes() if args.faq_snapshot else b"[]"
    policy = RegulationFirstFAQ(json.loads(faq_raw), scope=args.faq_scope, corpus=corpus)
    check_policy(corpus)
    if args.check:
        print(" ".join(f"{k}={v}" for k, v in COUNTS.items()))
        print(f"{sum(COUNTS.values())} turns per candidate. Input checksums and regulation-first cases passed.")
        return
    if not os.getenv("OPENROUTER_API_KEY"):
        parser.error("Live runs require OPENROUTER_API_KEY")
    out = args.out
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out / ".gitignore").write_text("*\n", encoding="utf-8")
    rev = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    pin(out / "manifest.json", {"revision": rev, "inputs": pins, "corpus": fingerprint(list(corpus.values())),
                               "faq_snapshot_sha256": digest(faq_raw), "faq_scope": args.faq_scope,
                               "configuration": {"models": {k: list(v) for k, v in control.ARMS.items()}, "reranker": MODEL_SHA,
                                                 "arms": list(ARMS), "independent_calls": args.independent_calls,
                                                 "python": args.python}})
    seed_file = out / "private-review-seed.json"
    if not seed_file.exists():
        control.save(seed_file, secrets.token_hex(16))
    seed = json.loads(seed_file.read_text(encoding="utf-8"))
    warm = not args.independent_calls or any(out.glob("*/*/calls/*.json"))
    summaries = {}
    for name, cases in suites.items():
        summaries[name] = run_suite(name, cases, lookup, out, policy, seed, args.independent_calls)
    control.save(out / "summary.json", {"candidate_turns": sum(COUNTS.values()),
        "total_turns": sum(COUNTS.values()) * len(ARMS), "warm_or_reused": warm, "suites": summaries})


if __name__ == "__main__":
    main()
