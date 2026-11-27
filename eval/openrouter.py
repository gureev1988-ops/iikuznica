"""OpenRouter transport for the selected answer core."""

import datetime
import hashlib
import json
import os
import re
import time
from urllib import error, request

SEARCH_MODELS = ("google/gemma-4-26b-a4b-it", "qwen/qwen3.5-122b-a10b")

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def fingerprint(body):
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_request(case, model, system):
    user = json.dumps({"question": case["question"], "sources": case["sources"]}, ensure_ascii=False)
    return {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": 1536, "stream": True, "stream_options": {"include_usage": True}}


def complete(case_id, body, dataset_id, *, timeout=150):
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
        with request.urlopen(req, timeout=timeout) as response:
            row["http"] = response.status
            if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                raise ValueError("Expected a streamed response")
            done = False
            for line in response:
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("Model deadline exceeded")
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
