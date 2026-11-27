"""Exercise both local protocols over HTTP, independent of provider credentials or proxies."""
from collections import deque
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace
from time import sleep

import pytest

from backend.assembly import models
from backend.assembly.models import DECIDER, GEMMA, QWEN, ModelCalls
from backend.assembly.transport import complete
from backend.contracts.engine import ModelAttemptFailure

QUESTIONS = {"coverage": {"type": "noul"}, "route": {"type": "choice", "criteria": {"answer": "", "hold": ""}}}
ANSWERS = {"coverage": {"type": "noul", "noul": .8}, "route": {
    "type": "choice", "choice": "answer", "confidence": .8, "probabilities": {"answer": .8, "hold": .2}}}


def stream(model=None, *, finished=True, usage=True):
    chunk = {"choices": [{"delta": {"content": "Проверено"}, "finish_reason": "stop"}]}
    if model is not None:
        chunk["model"] = model
    parts = ["data: " + json.dumps(chunk)]
    if usage:
        parts.append('data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}')
    if finished:
        parts.append("data: [DONE]")
    return (200, "text/event-stream", "\n\n".join(parts) + "\n\n")


def decision(model=None, *, answers=ANSWERS):
    data = {"answers": answers, "usage": {"input_tokens": 5, "output_tokens": 2}}
    if model is not None:
        data["model"] = model
    return 200, "application/json", json.dumps(data)


@pytest.fixture
def local(monkeypatch):
    replies, seen = deque(), []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization"), body))
            response = replies.popleft()
            if callable(response):
                response = response()
            status, content_type, text = response
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if status == 307:
                self.send_header("Location", "/unapproved")
            self.end_headers()
            try:
                self.wfile.write(text.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("II_MODEL_PROFILE", "local")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for role in ("gemma", "qwen", "decider"):
        prefix = "II_LOCAL_" + role.upper()
        protocol = "decisions" if role == "decider" else "v1/chat/completions"
        monkeypatch.setenv(prefix + "_URL", f"{base}/{role}/{protocol}")
        monkeypatch.setenv(prefix + "_MODEL", role + "-pinned")
        monkeypatch.delenv(prefix + "_TOKEN", raising=False)
    monkeypatch.setenv("II_LOCAL_GEMMA_TOKEN", "gemma-only")
    monkeypatch.setenv("II_LOCAL_DECIDER_TOKEN", "decider-only")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        yield SimpleNamespace(replies=replies, seen=seen, base=base)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def calls(log):
    def record(invoke, **requested):
        try:
            result = invoke()
        except ModelAttemptFailure as error:
            log.append((requested, error.result))
            raise
        log.append((requested, result))
        return result.value

    return ModelCalls(SimpleNamespace(deadline=datetime.now(UTC) + timedelta(seconds=15),
                                      attempts=SimpleNamespace(call=record)))


def test_local_chat_decisions_and_direct_faq_use_physical_role_targets(local, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "cloud-only")
    monkeypatch.setattr(models, "charge", lambda *args: pytest.fail("local calls must not price via OpenRouter"))
    attempts = []
    client = calls(attempts)
    local.replies.extend([stream("gemma-pinned"), stream("qwen-pinned"), decision("decider-pinned"),
                          stream("gemma-pinned")])
    chat = {"model": GEMMA, "messages": [{"role": "user", "content": "OK"}], "temperature": 0,
            "max_tokens": 7, "reasoning": {"enabled": False}, "provider": {"only": ["never-use"]}}
    assert client.chat("one", chat, "evidence-core")["text"] == "Проверено"
    assert client.chat("two", dict(chat, model=QWEN), "clarification")["ok"]
    assert client.decisions({"model": DECIDER, "questions": QUESTIONS, "state": "context"})["response"]["answers"] == ANSWERS
    assert complete("quality", chat, "faq_review", timeout=2)["ok"]
    assert [item[:2] for item in local.seen] == [
        ("/gemma/v1/chat/completions", "Bearer gemma-only"), ("/qwen/v1/chat/completions", None),
        ("/decider/decisions", "Bearer decider-only"), ("/gemma/v1/chat/completions", "Bearer gemma-only")]
    for index, role in [(0, "gemma"), (1, "qwen"), (3, "gemma")]:
        assert local.seen[index][2] == dict(model=role + "-pinned", messages=chat["messages"], temperature=0,
                                            max_tokens=7, stream=True, stream_options={"include_usage": True})
    assert local.seen[2][2] == dict(model="decider-pinned", questions=QUESTIONS, state="context")
    for (requested, result), role, protocol, input_tokens in zip(
        attempts, ("gemma", "qwen", "decider"), ("chat", "chat", "decisions"), (10, 10, 5)
    ):
        assert requested["route"] == f"local/{protocol}/{role}@{local.base.removeprefix('http://')}"
        assert requested["model"] == result.model == role + "-pinned"
        assert result.input_tokens == input_tokens and result.output_tokens == 2 and result.cost is None


@pytest.mark.parametrize("code,count", [(429, 2), (503, 2), (401, 1), (307, 1)])
def test_local_http_failures_retry_only_the_same_local_target(local, monkeypatch, code, count):
    monkeypatch.setenv("II_GEMMA_RESERVE_PROVIDER", "external-reserve")
    local.replies.extend([(code, "text/plain", "private upstream detail")] * 2)
    attempts = []
    with pytest.raises(ModelAttemptFailure, match=f"Local model HTTP {code}") as failure:
        calls(attempts).chat("failure", {"model": GEMMA}, "answer")
    assert "private" not in str(failure.value)
    assert [request[0] for request in local.seen] == ["/gemma/v1/chat/completions"] * count
    assert len(attempts) == count


def test_local_incomplete_stream_and_wrong_model_never_publish_a_partial_answer(local):
    attempts = []
    local.replies.extend([stream("gemma-pinned", finished=False), stream("wrong-model")])
    with pytest.raises(ModelAttemptFailure, match="different model ID"):
        calls(attempts).chat("failed", {"model": GEMMA}, "answer")
    assert len(attempts) == 2 and attempts[-1][1].model == "wrong-model"


def test_local_decisions_reject_invalid_verdict_and_reported_identity(local):
    attempts = []
    client = calls(attempts)
    body = {"model": DECIDER, "questions": QUESTIONS, "state": "context"}
    local.replies.extend([decision("decider-pinned", answers={}), decision("other")])
    with pytest.raises(ModelAttemptFailure, match="malformed"):
        client.decisions(body)
    with pytest.raises(ModelAttemptFailure, match="different model ID"):
        client.decisions(body)
    assert [(result.model, result.input_tokens) for _, result in attempts] == [("decider-pinned", 5), ("other", 5)]


def test_local_unknown_usage_and_identity_and_timeout(local, monkeypatch, caplog):
    attempts = []
    client = calls(attempts)
    local.replies.append(stream(usage=False))
    with caplog.at_level("INFO", logger="backend.assembly.latency"):
        client.chat("unknown", {"model": GEMMA}, "evidence")
    actual = attempts[0][1]
    assert actual.input_tokens is None and actual.cost is None and actual.model is None
    assert "requested_model=gemma-pinned served_model=None" in caplog.text
    assert "gemma-only" not in caplog.text

    def slow():
        sleep(.25)
        return stream()

    monkeypatch.setitem(models.ATTEMPT_SECONDS, GEMMA, (.08, .08))
    local.replies.extend([slow, slow])
    with pytest.raises(ModelAttemptFailure, match="deadline"):
        client.chat("slow", {"model": GEMMA}, "answer")
    assert len(attempts) == 3


def test_local_service_unavailable_records_only_internal_failures(local, monkeypatch):
    import socket

    attempts = []
    with socket.socket() as unavailable:
        # Reserve a TCP port without a listener, producing a real connection refusal.
        unavailable.bind(("127.0.0.1", 0))
        host = f"127.0.0.1:{unavailable.getsockname()[1]}"
        monkeypatch.setenv("II_LOCAL_QWEN_URL", f"http://{host}/v1/chat/completions")
        with pytest.raises(ModelAttemptFailure, match="Local model connection interrupted"):
            calls(attempts).chat("failed", {"model": QWEN}, "procedure")
    assert len(attempts) == 2
    assert all(request["route"] == f"local/chat/qwen@{host}" for request, _ in attempts)
    assert not local.seen


def test_local_typed_decision_cannot_turn_a_malformed_choice_into_an_answer(local):
    attempts = []
    malformed = {**ANSWERS, "route": {**ANSWERS["route"], "choice": ["answer"]}}
    local.replies.append(decision("decider-pinned", answers=malformed))
    with pytest.raises(ModelAttemptFailure, match="malformed"):
        calls(attempts).decisions({"model": DECIDER, "questions": QUESTIONS, "state": "context"})
    assert len(attempts) == 1 and attempts[0][1].model == "decider-pinned"
