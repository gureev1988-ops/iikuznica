"""Score BM25 candidates locally, without loading evaluation labels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from threading import BoundedSemaphore
import time

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

CONDITION = re.compile(r"^\d+(?:\.\d+)*\.\s*(?:в случа[ея]|при условии|при привлечении|за исключением|исключени|если|однако|дополнительно)", re.I)
TOKENIZER_SHA256 = "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626"
WEIGHTS_SHA256 = "1825907d6c1a9001ff78124780bbde20a614a8c3df3b63409cf3c72c6fe5c8b4"


def cpu_budget(cgroups=Path("/sys/fs/cgroup"), membership=Path("/proc/self/cgroup")):
    """Honor both CPU affinity and cgroup quotas, including parent limits."""
    try:
        capacity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        capacity = os.cpu_count() or 1
    limits = {(cgroups, False), (cgroups / "cpu", True), (cgroups / "cpu,cpuacct", True)}
    try:
        scopes = membership.read_text().splitlines()
    except OSError:
        scopes = ()
    for scope in scopes:
        _, controllers, member = scope.split(":", 2)
        path = Path(member.lstrip("/"))
        if ".." in path.parts or (controllers and "cpu" not in controllers.split(",")):
            continue
        for group in (path, *path.parents):
            if not controllers:
                limits.add((cgroups / group, False))
            else:
                limits.update((cgroups / mount / group, True) for mount in ("cpu", "cpu,cpuacct"))
    for group, legacy in limits:
        try:
            if legacy:
                quota = (group / "cpu.cfs_quota_us").read_text().strip()
                period = (group / "cpu.cfs_period_us").read_text().strip()
            else:
                quota, period = (group / "cpu.max").read_text().split()
            if quota != "max" and int(quota) > 0 and int(period) > 0:
                capacity = min(capacity, max(1, int(quota) // int(period)))
        except (OSError, ValueError):
            continue
    return capacity


_CPU_BUDGET = cpu_budget()
_RANKERS = min(4, max(1, _CPU_BUDGET // 2))
# Each active caller participates in inference, alongside one shared ORT pool.
_INTRA_THREADS = min(4, _CPU_BUDGET - _RANKERS + 1)
_RANK_SLOTS = BoundedSemaphore(_RANKERS)


def clause_key(source):
    return source["doc"], source["ref"]


def include_condition(selected, sources):
    # Keep five slots. Only consider the next sibling from the original pool.
    if len(selected) < 5:
        return selected
    anchor = sources[0]
    prefix, _, number = anchor["ref"].rpartition(".")
    target = f"{prefix}.{int(number)+1}" if prefix else str(int(number)+1)
    for i, s in enumerate(sources):
        if (s["doc"] == anchor["doc"] and s["section"] == anchor["section"]
                and s["ref"] == target and i not in selected and CONDITION.match(s["text"])):
            return selected[:4] + [i]
    return selected


def score_pairs(session, tokenizer, question, sources):
    if not sources:
        return []
    with _RANK_SLOTS:
        return _score_pairs(session, tokenizer, question, sources)


def _score_pairs(session, tokenizer, question, sources):
    pairs = [(question, f'{s["title"]}\n{s["section"]}\n{s["text"]}') for s in sources]
    encoded = tokenizer.encode_batch(pairs)
    by_length = sorted(range(len(encoded)), key=lambda i: len(encoded[i].ids))
    pad = tokenizer.token_to_id("<pad>")
    names = {v.name for v in session.get_inputs()}
    scores = np.empty(len(encoded), dtype=np.float32)
    for start in range(0, len(by_length), 6):
        idx = by_length[start:start+6]
        size = max(len(encoded[i].ids) for i in idx)
        ids = np.full((len(idx), size), pad, dtype=np.int64)
        mask = np.zeros_like(ids)
        types = np.zeros_like(ids)
        for j, i in enumerate(idx):
            n = len(encoded[i].ids)
            ids[j, :n] = encoded[i].ids
            mask[j, :n] = 1
            types[j, :n] = encoded[i].type_ids
        feeds = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
        logits = session.run(None, {k: v for k, v in feeds.items() if k in names})[0].reshape(-1)
        if len(logits) != len(idx) or not np.isfinite(logits).all():
            raise ValueError("Expected one finite relevance logit per passage")
        scores[idx] = logits
    return scores.tolist()


def load_model(model_dir):
    weights = model_dir / "model_qint8_arm64.onnx"
    with weights.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WEIGHTS_SHA256:
            raise ValueError("The checkpoint differs from the pinned INT8 export")
    tokenizer_path = model_dir / "tokenizer.json"
    with tokenizer_path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != TOKENIZER_SHA256:
            raise ValueError("The tokenizer differs from the pinned revision")
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_padding()
    tokenizer.enable_truncation(max_length=512, strategy="only_second")
    options = ort.SessionOptions()
    options.intra_op_num_threads = _INTRA_THREADS
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    session = ort.InferenceSession(str(weights), options, providers=["CPUExecutionProvider"])
    return session, tokenizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    session, tokenizer = load_model(args.model_dir)
    outputs = []
    for line in args.pool.read_text().splitlines():
        case = json.loads(line)
        sources = case["sources"]
        if len(sources) > 30 or len(set(map(clause_key, sources))) != len(sources):
            raise ValueError(f'{case["id"]} contains an invalid candidate pool')
        start = time.perf_counter()
        scores = score_pairs(session, tokenizer, case["question"], sources)
        elapsed = (time.perf_counter() - start) * 1000
        neural = sorted(range(len(scores)), key=lambda i: -scores[i])
        base = list(range(min(5, len(sources))))
        hybrid = base[:2] + [i for i in neural if i > 1][:3]
        selected = {"bm25": base, "bm25_condition": include_condition(base, sources),
                    "minilm": neural[:5], "hybrid": hybrid,
                    "hybrid_condition": include_condition(hybrid, sources)}
        outputs.append({"id": case["id"], "rerank_ms": round(elapsed, 2),
                        "pool": [dict(doc=s["doc"], ref=s["ref"], logit=score)
                                 for s, score in zip(sources, scores)],
                        "methods": {k: [clause_key(sources[i]) for i in ix] for k, ix in selected.items()}})
        print(case["id"], round(elapsed), "ms", flush=True)
    args.out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in outputs))


if __name__ == "__main__":
    main()
