"""Compare three OpenRouter models on the private start12 benchmark (Python 3.9+).

    python3 eval/run_12x3.py --input /path/to/IIkuznitsa_Run_12x3.py
    python3 eval/run_12x3.py --input /path/to/IIkuznitsa_QA_Bank_50.zip --check

The uploaded standalone runner is treated as a data container. Its code is never
executed. Only the question and its source passages are sent to the provider.
"""

import argparse
import ast
import base64
import csv
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib import error, request
from zipfile import ZipFile
import zlib


MODELS = ("qwen/qwen3-14b", "google/gemma-4-26b-a4b-it", "qwen/qwen3.5-122b-a10b")
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def load_dataset(path):
    if path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            def rows(name):
                return [json.loads(line) for line in archive.read("start12/" + name).splitlines()]

            guide = archive.read("README.md").decode()
            system = guide.split("## Единая инструкция для модели", 1)[1].split("\n## ", 1)[0].strip()
            return {"cases": rows("questions_with_sources.jsonl"), "gold": rows("answer_key.jsonl"), "system": system}

    # The original single-file export embeds exactly one compressed JSON string in DATA.
    for node in ast.parse(path.read_bytes()).body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["DATA"]:
            literals = [x.value for x in ast.walk(node.value) if isinstance(x, ast.Constant) and isinstance(x.value, str)]
            if len(literals) == 1:
                return json.loads(zlib.decompress(base64.b64decode(literals[0], validate=True)))
    raise ValueError("Expected the private bank ZIP or its standalone Python export")


def fingerprint(body):
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_request(case, model, system):
    user = json.dumps({"question": case["question"], "sources": case["sources"]}, ensure_ascii=False)
    return {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": 1536, "stream": True, "stream_options": {"include_usage": True}}


def complete(case_id, body, dataset_id):
    started = time.monotonic()
    row = {"id": case_id, "model": body["model"], "dataset": dataset_id, "request_hash": fingerprint(body),
           "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "ok": False, "http": None,
           "text": "", "ttft_s": None, "usage": {}, "provider": None, "served_model": None,
           "completion_id": None, "finish": None}
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        headers["Authorization"] = "Bearer " + key
    req = request.Request(ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
    try:
        with request.urlopen(req, timeout=150) as response:
            row["http"] = response.status
            if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                raise ValueError("Expected a streamed response")
            done = False
            for line in response:
                if not line.startswith(b"data:"):
                    continue
                frame = line[5:].strip()
                if frame == b"[DONE]":
                    done = True
                    break
                obj = json.loads(frame)
                if obj.get("error"):
                    row["stream_error_code"] = obj["error"].get("code")
                    raise ValueError("OpenRouter stream error")
                for dest, src in (("completion_id", "id"), ("served_model", "model"), ("provider", "provider")):
                    if obj.get(src):
                        row[dest] = obj[src]
                if obj.get("usage"):
                    row["usage"] = obj["usage"]
                for choice in obj.get("choices") or []:
                    part = (choice.get("delta") or {}).get("content")
                    if isinstance(part, str) and part:
                        if row["ttft_s"] is None and part.strip():
                            row["ttft_s"] = round(time.monotonic() - started, 3)
                        row["text"] += part
                    if choice.get("finish_reason"):
                        row["finish"] = choice["finish_reason"]
            row["ok"] = bool(done and row["text"].strip() and row["finish"] == "stop")
            if not row["ok"]:
                row["error"] = "Empty, truncated or incomplete response"
    except error.HTTPError as exc:
        row["http"] = exc.code
        try:
            message = str(json.loads(exc.read())["error"]["message"])
            row["error"] = re.sub(r"sk-[\w-]+", "[redacted]", message)[:320]
        except (ValueError, KeyError, TypeError):
            row["error"] = "HTTP error"
    except (OSError, ValueError) as exc:
        row["error"] = type(exc).__name__ + ": " + str(exc)[:240]
    row["latency_s"] = round(time.monotonic() - started, 3)
    return row


def score(row, gold):
    raw = row["text"].strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = re.sub(r"^\x60{3}(?:json)?\s*", "", raw, flags=re.I)[:-3].strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    obj = parsed if isinstance(parsed, dict) else {}
    sources = obj.get("sources")
    cited = {(str(x.get("doc")), str(x.get("ref", "")).strip(".")) for x in sources if isinstance(x, dict)} if isinstance(sources, list) else set()
    expected = {(x["doc"], x["ref"]) for x in gold["evidence"]}
    usage = row["usage"]
    return {"model": row["model"], "id": row["id"], "http": row["http"], "complete": row["ok"],
            "expected_action": gold["expected_action"], "actual_action": obj.get("action"),
            "action_ok": row["ok"] and obj.get("action") == gold["expected_action"],
            "cites_expected_ref": bool(cited & expected) if gold["expected_action"] == "answer" else None,
            "ttft_s": row["ttft_s"], "latency_s": row["latency_s"], "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"), "cost_usd_openrouter": usage.get("cost")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("iikuznitsa_run12"))
    parser.add_argument("--check", action="store_true", help="Validate the bank without network requests")
    args = parser.parse_args()
    data = load_dataset(args.input)
    cases = data["cases"]
    gold = {g["id"]: g for g in data["gold"]}
    expected_ids = {f"Q{i:02}" for i in range(1, 13)}
    if (len(cases) != 12 or len(data["gold"]) != 12 or {c["id"] for c in cases} != expected_ids
            or set(gold) != expected_ids or not data["system"].strip()):
        parser.error("Expected 12 matching start12 questions and answers")
    if args.check:
        print("Validated 12 questions and 3 models. No network calls.")
        return 0

    dataset_id = fingerprint({"cases": cases, "system": data["system"]})
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("A benchmark is already running in this output folder")
        return run_cases(args.out, cases, data["system"], gold, dataset_id)


def run_cases(output, cases, system, gold, dataset_id):
    log = output / "attempts.jsonl"
    latest = {}
    if log.exists():
        for line in log.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("dataset") != dataset_id:
                    raise SystemExit("Output folder contains another or unversioned run. Choose a separate --out.")
                latest[row["id"], row["model"]] = row
    skip = set()
    for case in cases:
        for model in MODELS:
            body = make_request(case, model, system)
            previous = latest.get((case["id"], model), {})
            if model in skip or (previous.get("ok") and previous.get("request_hash") == fingerprint(body)):
                continue
            row = complete(case["id"], body, dataset_id)
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            latest[case["id"], model] = row
            print(case["id"], model, row["http"], row["ok"], row.get("error", ""), flush=True)
            if row["http"] in (401, 402):
                skip.update(MODELS)
            elif row["http"] in (400, 403, 404):
                skip.add(model)
            if row["http"] == 429:
                time.sleep(3)
    result = [score(row, gold[row["id"]]) for row in latest.values()]
    if result:
        with (output / "scores.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(result[0]))
            writer.writeheader()
            writer.writerows(result)
    n = sum(row["ok"] for row in latest.values())
    print(f"Completed {n} of 36")
    return 0 if n == 36 else 2


if __name__ == "__main__":
    sys.exit(main())
