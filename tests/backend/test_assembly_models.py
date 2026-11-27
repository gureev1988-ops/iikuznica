"""Model role contracts without remote calls."""
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from backend.assembly.models import DECIDER, GEMMA, ModelCalls
from backend.contracts.engine import ModelAttemptFailure


class Response(BytesIO):
    status = 200
    headers = {"Content-Type": "text/event-stream"}


def test_chat_and_decisions_use_separate_endpoints_and_count_attempts(monkeypatch):
    from eval.decider import run

    seen = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")

    def urlopen(req, timeout):
        seen.append((req.full_url, json.loads(req.data), timeout))
        if req.full_url.endswith("/decisions"):
            return Response(b'{"answers":{"r":{"type":"choice","choice":"yes"}},"usage":{"input_tokens":3}}')
        return Response(b'data: {"model":"served","provider":"DeepInfra","choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":2}}\n\ndata: [DONE]\n')

    monkeypatch.setattr(run.request, "urlopen", urlopen)
    calls = []

    def record(invoke, **context):
        value = invoke()
        calls.append((context, value))
        return value.value

    request = SimpleNamespace(deadline=datetime.now(UTC) + timedelta(seconds=20),
                              attempts=SimpleNamespace(call=record))
    models = ModelCalls(request)
    message = {"model": GEMMA, "messages": [{"role":"user","content":"OK"}], "stream": True}
    assert models.chat("test", message, "answer")["text"] == "OK"
    assert models.decisions({"model": DECIDER, "questions": {}, "state": "test"})["response"]["answers"]
    assert [item[0].split("openrouter.ai")[1] for item in seen] == ["/api/v1/chat/completions", "/api/alpha/decisions"]
    assert seen[0][1]["provider"]["only"] == ["deepinfra/fp8"]
    assert "messages" not in seen[1][1] and 0 < seen[1][2] <= 20
    assert calls[0][1].input_tokens == 2 and calls[0][1].route == "openrouter/chat/DeepInfra"
    assert calls[1][1].input_tokens == 3


@pytest.mark.parametrize("code", [429, 503, 401])
def test_failed_chat_is_technical_and_retries_bounded(monkeypatch, code):
    from eval import openrouter

    seen = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")

    def rejected(req, timeout):
        seen.append(req)
        raise HTTPError(req.full_url, code, "test", {}, BytesIO(b'{"error":{"message":"unavailable"}}'))

    monkeypatch.setattr(openrouter.request, "urlopen", rejected)
    request = SimpleNamespace(deadline=datetime.now(UTC) + timedelta(seconds=20), attempts=None)
    with pytest.raises(ModelAttemptFailure, match=f"HTTP {code}"):
        ModelCalls(request).chat("test", {"model": GEMMA}, "answer")
    assert len(seen) == (1 if code == 401 else 2)
