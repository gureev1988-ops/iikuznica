"""Score BM25 candidates locally, without loading evaluation labels."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

CONDITION = re.compile(r"^\d+(?:\.\d+)*\.\s*(?:в случа[ея]|при условии|при привлечении|за исключением|исключени|если|однако|дополнительно)", re.I)
WEIGHTS_SHA256 = "1825907d6c1a9001ff78124780bbde20a614a8c3df3b63409cf3c72c6fe5c8b4"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    weights = args.model_dir / "model_qint8_arm64.onnx"
    with weights.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WEIGHTS_SHA256:
            parser.error("The checkpoint differs from the pinned INT8 export")
    tokenizer_path = args.model_dir / "tokenizer.json"
    with tokenizer_path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626":
            parser.error("The tokenizer differs from the pinned revision")
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_padding()
    tokenizer.enable_truncation(max_length=512, strategy="only_second")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(weights), options, providers=["CPUExecutionProvider"])
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
