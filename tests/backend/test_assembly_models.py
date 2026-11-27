"""Model role contracts without remote calls."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from backend.assembly.models import ATTEMPT_SECONDS, DECIDER, GEMMA, ModelCalls, decisions_valid
from backend.contracts.engine import ModelAttemptFailure
from backend.dialog.service import _AttemptRecorder
from backend.runtime import enqueue, run_once


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")
    client = httpx.AsyncClient

    def mock(handler):
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda **options: client(transport=httpx.MockTransport(handler), **options))

    return mock


def model_calls(attempts=None):
    return ModelCalls(SimpleNamespace(deadline=datetime.now(UTC) + timedelta(seconds=20), attempts=attempts))


def chat_response():
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=(
        b'data: {"model":"served","provider":"DeepInfra","choices":[{"delta":{"content":"OK"},'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":2}}\n\ndata: [DONE]\n'
    ))


def test_chat_and_decisions_use_separate_endpoints_and_count_attempts(upstream):

    seen = []
    def response(req):
        seen.append((req.url.path, json.loads(req.content)))
        if req.url.path.endswith("/decisions"):
            return httpx.Response(200, json={"answers": {"r": {"type": "choice", "choice": "yes"}},
                                             "usage": {"input_tokens": 3}})
        return chat_response()

    upstream(response)
    calls = []

    def record(invoke, **context):
        value = invoke()
        calls.append((context, value))
        return value.value

    models = model_calls(SimpleNamespace(call=record))
    message = {"model": GEMMA, "messages": [{"role":"user","content":"OK"}], "stream": True}
    assert models.chat("test", message, "answer")["text"] == "OK"
    assert models.decisions({"model": DECIDER, "questions": {}, "state": "test"})["response"]["answers"]
    assert [item[0] for item in seen] == ["/api/v1/chat/completions", "/api/alpha/decisions"]
    assert seen[0][1]["provider"]["only"] == ["deepinfra/fp8"]
    assert "messages" not in seen[1][1]
    assert calls[0][1].input_tokens == 2 and calls[0][1].route == "openrouter/chat/DeepInfra"
    assert calls[1][1].input_tokens == 3


@pytest.mark.parametrize("code", [429, 503, 401])
def test_failed_chat_is_technical_and_retries_bounded(upstream, code):
    seen = []

    def rejected(req):
        seen.append(req)
        return httpx.Response(code)

    upstream(rejected)
    with pytest.raises(ModelAttemptFailure, match=f"HTTP {code}"):
        model_calls().chat("test", {"model": GEMMA}, "answer")
    assert len(seen) == (1 if code == 401 else 2)


def test_chat_deadline_before_headers_uses_configured_reserve(upstream, monkeypatch):
    monkeypatch.setitem(ATTEMPT_SECONDS, GEMMA, (0.1, 0.5))
    monkeypatch.setenv("II_GEMMA_RESERVE_PROVIDER", "reserve")
    seen = []

    async def response(req):
        seen.append(json.loads(req.content)["provider"]["only"])
        if len(seen) == 1:
            await asyncio.sleep(1)
            pytest.fail("The primary attempt should have ended at its deadline")
        return chat_response()

    upstream(response)
    assert model_calls().chat("test", {"model": GEMMA}, "answer")["text"] == "OK"
    assert seen == [["deepinfra/fp8"], ["reserve"]]


def test_decisions_deadline_covers_trickled_body(upstream, monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                yield b" "
                await asyncio.sleep(0.01)
            pytest.fail("Reading should have stopped before the body finished")

    monkeypatch.setitem(ATTEMPT_SECONDS, DECIDER, (0.1, 0.1))
    seen = []

    def response(req):
        seen.append(req)
        return httpx.Response(200, stream=SlowBody())

    upstream(response)
    with pytest.raises(ModelAttemptFailure, match="request exceeded its deadline"):
        model_calls().decisions({"model": DECIDER, "questions": {}, "state": "test"})
    assert len(seen) == 2


def test_exhausted_model_retries_do_not_restart_answer_job(upstream, dsn, db):
    seen = []

    def unavailable(req):
        seen.append(req)
        return httpx.Response(503)

    upstream(unavailable)
    job_id = enqueue(db, queue="interactive", kind="answer", key="turn", payload={},
                     deadline_at=datetime.now(UTC) + timedelta(seconds=20))
    db.commit()
    turn_id, case_id = uuid4(), uuid4()

    def answer(job):
        context = SimpleNamespace(deadline=job.deadline_at, turn_id=turn_id,
                                  attempts=_AttemptRecorder(dsn, job, turn_id, case_id))
        ModelCalls(context).chat("test", {"model": GEMMA}, "answer")

    assert run_once(dsn, "interactive", {"answer": answer}).id == job_id
    assert run_once(dsn, "interactive", {"answer": answer}) is None
    assert len(seen) == 2
    assert db.execute("SELECT state FROM jobs WHERE id = %s", (job_id,)).fetchone()["state"] == "failed"
    calls = db.execute("SELECT state FROM model_calls WHERE job_id = %s", (job_id,)).fetchall()
    assert calls == [{"state": "failed"}, {"state": "failed"}]


def test_decisions_reject_missing_and_corrupt_fields():
    questions = {"check": {"type": "noul"}, "route": {"type": "choice", "criteria": {"answer": "", "hold": ""}}}
    valid = {"check": {"type":"noul", "noul": .5}, "route": {"type":"choice", "choice":"answer",
             "confidence": .75, "probabilities": {"answer": .875, "hold": .125}}}
    assert decisions_valid(valid, questions)
    assert not decisions_valid({"check": valid["check"]}, questions)
    assert not decisions_valid(dict(valid, check={"type":"noul", "noul": float("nan")}), questions)
    assert not decisions_valid(dict(valid, route=dict(valid["route"], choice="publish")), questions)
