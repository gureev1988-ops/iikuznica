from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier, Event, Thread
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from backend.db import connect
from backend.runtime import (
    DeliveryStateConflict,
    KnownDeliveryFailure,
    LeaseLost,
    ModelCallFailure,
    ModelResult,
    PendingDelivery,
    QueueFull,
    call_model,
    claim,
    enqueue,
    finish,
    heartbeat,
    model_costs,
    operations_router,
    reconcile_delivery,
    recover_jobs,
    recover_journal,
    require_lease,
    run_once,
    send_once,
)


class WorkerStopped(BaseException):
    """Simulate a restart before the worker can record a provider outcome."""


@pytest.mark.parametrize("slots", [3, 10])
def test_interactive_worker_polls_once_when_idle_and_fills_all_slots(dsn, monkeypatch, slots):
    from backend import __main__ as cli
    from backend.runtime import jobs, journal

    idle, resume, second_idle = Event(), Event(), Event()

    class StopEvent(Event):
        def wait(self, timeout=None):
            if idle.is_set():
                second_idle.set()
            idle.set()
            resume.wait(timeout=5)
            return self.is_set()

    stop = StopEvent()
    monkeypatch.setattr(cli, "Event", lambda: stop)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    connections = []

    def counted_connect(url):
        connections.append(url)
        return connect(url)

    monkeypatch.setattr(jobs, "connect", counted_connect)
    monkeypatch.setattr(journal, "connect", counted_connect)
    active_handlers = Barrier(slots)

    def handle(job):
        if job.business_key == str(slots):
            stop.set()
        else:
            active_handlers.wait(timeout=5)

    backend = SimpleNamespace(
        settings=SimpleNamespace(profile="test", interactive_concurrency=slots, poll_interval=15),
        run_job=lambda queue, **opts: run_once(dsn, queue, {"answer": handle}, max_running=slots, **opts),
    )
    runner = Thread(target=cli.worker, args=(backend, "interactive", False))
    runner.start()
    try:
        assert idle.wait(timeout=5)
        assert not second_idle.wait(timeout=0.1)
        assert len(connections) == 3  # Recovery of jobs, recovery of calls, and one claim.
        with connect(dsn) as conn:
            for i in range(slots + 1):
                enqueue(conn, queue="interactive", kind="answer", key=str(i), payload={})
        resume.set()
        runner.join(timeout=8)
        assert not runner.is_alive()
    finally:
        stop.set()
        resume.set()
        active_handlers.abort()
        runner.join(timeout=8)
    with connect(dsn) as conn:
        rows = conn.execute("SELECT business_key, state FROM jobs ORDER BY first_started_at").fetchall()
    assert [(row["business_key"], row["state"]) for row in rows] == [
        (str(i), "succeeded") for i in range(slots + 1)
    ]


def test_atomic_admission_and_business_key_deduplication(dsn, db):
    with pytest.raises(RuntimeError):
        with connect(dsn) as conn:
            enqueue(conn, queue="interactive", kind="answer", key="rollback", payload={})
            raise RuntimeError("reject the caller transaction")
    assert db.execute("SELECT count(*) AS count FROM jobs").fetchone()["count"] == 0

    def submit():
        with connect(dsn) as conn:
            return enqueue(conn, queue="interactive", kind="answer", key="same", payload={}, max_pending=1)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: submit(), range(10)))
    assert len(set(results)) == 1
    assert claim(dsn, "interactive").id == results[0]
    with pytest.raises(QueueFull):
        enqueue(db, queue="interactive", kind="answer", key="other", payload={}, max_pending=1)
    enqueue(db, queue="background", kind="answer", key="other", payload={}, max_pending=1)


def test_concurrent_claims_respect_each_queue_limit(dsn, db):
    for queue in ("interactive", "background"):
        for index in range(3):
            enqueue(db, queue=queue, kind="example", key=str(index), payload={})
    db.commit()

    barrier = Barrier(6)

    def race(queue):
        barrier.wait(timeout=5)
        return claim(dsn, queue, max_running=1)

    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(race, ["interactive", "background"] * 3))
    assert sorted(job.queue for job in claims if job is not None) == ["background", "interactive"]
    for job in claims:
        if job:
            finish(db, job)


def test_retries_delay_and_terminal_outcomes_are_durable(dsn, db):
    job_id = enqueue(db, queue="interactive", kind="answer", key="1", payload={"turn": 1}, max_attempts=2)
    expired_id = enqueue(
        db, queue="interactive", kind="answer", key="2", payload={"turn": 2},
        deadline_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db.commit()

    observed = []

    def fail(job):
        observed.append(job.attempt)
        raise TimeoutError("raw provider message outside diagnostics")

    db.execute("CREATE TABLE outcomes (id uuid PRIMARY KEY, reason text NOT NULL)")
    db.commit()

    def terminal(conn, job, reason):
        conn.execute("INSERT INTO outcomes VALUES (%s, %s)", (job.id, reason))

    options = {"base_retry_seconds": 5, "terminal_handlers": {"answer": terminal}}
    assert run_once(dsn, "interactive", {"answer": fail}, **options).id == job_id
    assert run_once(dsn, "interactive", {"answer": fail}, **options) is None
    db.execute("UPDATE jobs SET available_at = clock_timestamp() WHERE id = %s", (job_id,))
    db.commit()
    assert run_once(dsn, "interactive", {"answer": fail}, **options).id == job_id

    assert observed == [1, 2]
    assert {
        row["id"]: row["reason"] for row in db.execute("SELECT * FROM outcomes")
    } == {job_id: "TimeoutError", expired_id: "deadline_exceeded"}
    assert {row["state"] for row in db.execute("SELECT state FROM jobs")} == {"failed"}


def test_worker_renews_lease_during_slow_handler(dsn, db):
    job_id = enqueue(db, queue="background", kind="sync", key="one", payload={})
    db.commit()

    def slow_handler(job):
        sleep(1.4)
        with connect(dsn) as conn:
            renewed = require_lease(conn, job)
            assert renewed.lease_expires_at > job.lease_expires_at

    assert run_once(dsn, "background", {"sync": slow_handler}, lease_seconds=1.2).id == job_id
    assert db.execute("SELECT state FROM jobs").fetchone()["state"] == "succeeded"


def test_lease_token_fences_old_owner_between_reclaims(dsn, db):
    current = datetime.now(UTC)
    job_id = enqueue(
        db, queue="interactive", kind="answer", key="1", payload={},
        deadline_at=current + timedelta(seconds=30),
    )
    db.commit()
    first = claim(dsn, "interactive", lease_seconds=1)
    extended = heartbeat(dsn, first, lease_seconds=60)
    assert extended.id == job_id
    assert extended.lease_expires_at == extended.deadline_at
    assert extended.lease_expires_at > first.lease_expires_at

    db.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() WHERE id = %s", (job_id,))
    db.commit()
    assert recover_jobs(dsn, "interactive", base_retry_seconds=0) == 1
    second = claim(dsn, "interactive", max_running=1)
    assert second.id == first.id and second.attempt == 2
    assert second.lease_token != first.lease_token
    with pytest.raises(LeaseLost):
        require_lease(db, first)
    with pytest.raises(LeaseLost):
        heartbeat(dsn, first)
    with pytest.raises(LeaseLost):
        finish(db, first)
    finish(db, second)
    assert db.execute("SELECT state FROM jobs").fetchone()["state"] == "succeeded"


@pytest.mark.parametrize("channel", ["smtp", "dion"])
@pytest.mark.parametrize("accepted", [False, True])
def test_interrupted_delivery_is_unknown_until_reconciled(dsn, db, channel, accepted):
    key = str(uuid4())
    accepted_effects = []

    def interrupt():
        with connect(dsn) as conn:
            assert conn.execute("SELECT state FROM delivery_attempts").fetchone()["state"] == "sending"
        if accepted:
            accepted_effects.append(key)
        raise WorkerStopped

    job_id = enqueue(db, queue="background", kind="send", key=key, payload={})
    db.commit()
    with pytest.raises(WorkerStopped):
        run_once(dsn, "background", {"send": lambda job: send_once(
            dsn, channel=channel, effect_key=key, send=interrupt, job=job,
        )})
    db.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() WHERE id = %s", (job_id,))
    db.commit()
    outcomes = []

    def send():
        accepted_effects.append(key)
        return "provider-1"

    def retry(job):
        outcomes.append(send_once(dsn, channel=channel, effect_key=key, send=send, job=job))

    run_once(dsn, "background", {"send": retry}, base_retry_seconds=0)
    unknown = outcomes[0]
    assert unknown.state == "delivery_unknown"
    assert len(accepted_effects) == int(accepted)
    assert db.execute("SELECT state FROM jobs").fetchone()["state"] == "succeeded"

    resolution = "sent" if accepted else "failed"
    reconcile_delivery(dsn, attempt_id=unknown.attempt_id, state=resolution, detail="provider evidence")
    again = send_once(dsn, channel=channel, effect_key=key, send=send)
    assert again.state == "sent"
    assert len(accepted_effects) == 1
    assert (again.attempt_id == unknown.attempt_id) is accepted


def test_concurrent_deliveries_and_definitive_rejection(dsn):
    active = Event()
    release = Event()

    def send():
        active.set()
        assert release.wait(timeout=5)
        raise KnownDeliveryFailure("smtp_451")

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(send_once, dsn, channel="smtp", effect_key="same", send=send)
        try:
            assert active.wait(timeout=5)
            duplicate = send_once(dsn, channel="smtp", effect_key="same", send=lambda: "unexpected")
            assert duplicate.state == "sending"
        finally:
            release.set()
        failure = result.result()

    assert failure.attempt_id == duplicate.attempt_id
    assert (failure.state, failure.detail) == ("failed", "smtp_451")
    successful = send_once(dsn, channel="smtp", effect_key="same", send=lambda: "expected")
    assert successful.attempt_id != duplicate.attempt_id
    assert successful.provider_id == "expected"


def test_async_success_and_ambiguous_transport_errors(dsn, db):
    def timed_out():
        raise TimeoutError("private error details")

    unknown = send_once(dsn, channel="smtp", effect_key="timed-out", send=timed_out)
    assert (unknown.state, unknown.provider_id, unknown.detail) == ("delivery_unknown", None, "TimeoutError")
    pending = send_once(dsn, channel="dion", effect_key="send", send=lambda: PendingDelivery("ack"))
    assert (pending.state, pending.provider_id) == ("delivery_unknown", "ack")
    with pytest.raises(DeliveryStateConflict):
        reconcile_delivery(dsn, attempt_id=pending.attempt_id, state="sent", provider_id="different")
    reconcile_delivery(dsn, attempt_id=pending.attempt_id, state="sent", provider_id="ack", conn=db)
    db.rollback()
    again = send_once(dsn, channel="dion", effect_key="send", send=lambda: "unexpected")
    assert again == pending
    assert reconcile_delivery(dsn, attempt_id=pending.attempt_id, state="sent").provider_id == "ack"


def test_journal_retains_each_model_attempt_and_separates_unknown_cost(dsn, db):
    turn_id = uuid4()
    case_id = uuid4()
    job_id = enqueue(db, queue="interactive", kind="answer", key=str(turn_id), payload={})
    db.commit()

    def first_provider():
        raise ModelCallFailure("provider unavailable", route="route-actual", input_tokens=10, cost=0)

    def work(job):
        shared = {"trace_id": "trace", "model": "configured", "route": "gateway"}
        location = {"job": job, "turn_id": turn_id, "case_id": case_id}
        if job.attempt == 1:
            call_model(dsn, first_provider, role="answer", **shared, **location)
        else:
            response = call_model(
                dsn, lambda: ModelResult("response", 5, 4, Decimal("0.05"), "served", "configured-v2"),
                role="answer", **shared, **location,
            )
            assert response == "response"
            call_model(dsn, lambda: True, role="judge", **shared, **location)

    assert run_once(dsn, "interactive", {"answer": work}, base_retry_seconds=0).id == job_id
    assert run_once(dsn, "interactive", {"answer": work}, base_retry_seconds=0).id == job_id
    call_model(dsn, lambda: ModelResult("background", cost=0), role="import", trace_id="bg", model="m", route="bg")
    calls = list(db.execute("SELECT * FROM model_calls ORDER BY started_at"))
    assert [(entry["role"], entry["state"], entry["cost"]) for entry in calls] == [
        ("answer", "failed", Decimal("0")),
        ("answer", "succeeded", Decimal("0.05")),
        ("judge", "succeeded", None),
        ("import", "succeeded", Decimal("0")),
    ]
    assert [entry["route"] for entry in calls] == ["route-actual", "served", "gateway", "bg"]
    assert calls[1]["model"] == "configured-v2"
    assert {entry["trace_id"] for entry in calls[:-1]} == {"trace"}
    scopes = {row["scope"]: row for row in model_costs(dsn)}
    assert scopes["case"]["scope_id"] == case_id
    assert scopes["turn"]["scope_id"] == turn_id
    assert scopes["turn"]["unknown_cost_calls"] == 1
    assert scopes["turn"]["known_cost"] == "0.05"
    assert scopes["turn"]["input_tokens"] == 15
    assert scopes["background"]["known_cost"] == "0"
    assert scopes["background"]["unknown_cost_calls"] == 0


def test_model_attempt_survives_worker_stop(dsn, db):
    def stop():
        raise WorkerStopped

    enqueue(db, queue="interactive", kind="answer", key="one", payload={})
    db.commit()
    job = claim(dsn, "interactive")
    with pytest.raises(WorkerStopped):
        call_model(dsn, stop, role="answer", trace_id="one", model="m", route="route", job=job)
    db.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() WHERE id = %s", (job.id,))
    db.commit()
    assert recover_journal(dsn) == (0, 1)
    attempt = db.execute("SELECT state, cost, finished_at FROM model_calls").fetchone()
    assert attempt["state"] == "outcome_unknown" and attempt["cost"] is None
    assert attempt["finished_at"] is not None


def test_health_probes_separate_optional_integrations_from_readiness(dsn, db):
    def authorize(x_ops: str = Header(default="")):
        if x_ops != "yes":
            raise HTTPException(403)

    def unavailable():
        raise TimeoutError("secret URL")

    enqueue(db, queue="interactive", kind="answer", key="one", payload={"private": "text"})
    db.commit()
    app = FastAPI()
    app.include_router(operations_router(dsn, authorize, {"smtp": unavailable}))
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").json() == {"status": "ready", "database": "available"}
        assert client.get("/v1/ops/jobs").status_code == 403
        headers = {"X-Ops": "yes"}
        assert client.get("/v1/ops/integrations", headers=headers).json() == {
            "integrations": {"smtp": {"status": "unavailable", "reason": "TimeoutError"}}
        }
        report = client.get("/v1/ops/jobs", headers=headers).json()
        assert len(report["jobs"]) == 1
        assert "payload" not in report["jobs"][0]
        assert client.get("/v1/ops/model-costs", headers=headers).json() == {"costs": []}
        assert client.get("/v1/ops/model-calls", headers=headers).json() == {"calls": []}
        assert client.get("/v1/ops/latencies", headers=headers).json() == {"latencies": []}
        assert client.get("/v1/ops/deliveries", headers=headers).json() == {"deliveries": []}

    app = FastAPI()
    app.include_router(operations_router("", authorize))
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_latency_metrics_use_persisted_events(dsn, db):
    created = datetime.now(UTC)
    conversation_id = db.execute("INSERT INTO conversations (owner_id) VALUES ('employee') RETURNING id").fetchone()["id"]
    case_id = db.execute("INSERT INTO cases (conversation_id) VALUES (%s) RETURNING id", (conversation_id,)).fetchone()["id"]
    turn_id = db.execute(
        "INSERT INTO turns (conversation_id, case_id, request_id, question, created_at)"
        "VALUES (%s, %s, 'client-request', 'test', %s) RETURNING id",
        (conversation_id, case_id, created),
    ).fetchone()["id"]
    timeline = [
        ("turn.processing", 1),
        ("turn.completed", 2),
        ("offer.confirmed", 3),
        ("mail.automatic", 7),
        ("mail.counter_question", 13),
        ("mail.resolution", 45),
    ]
    for cursor, (kind, seconds) in enumerate(timeline, start=1):
        db.execute(
            """
            INSERT INTO events (conversation_id, case_id, turn_id, cursor, kind, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (conversation_id, case_id, turn_id, cursor, kind, created + timedelta(seconds=seconds)),
        )
    db.commit()
    app = FastAPI()
    app.include_router(operations_router(dsn, lambda: None))
    with TestClient(app) as client:
        response = client.get("/v1/ops/latencies", params={"case_id": str(case_id)})
    assert response.json() == {"latencies": [{
        "turn_id": str(turn_id), "case_id": str(case_id), "first_useful_seconds": 2,
        "verified_answer_seconds": 45, "human_wait_seconds": 10,
    }]}


def test_disabled_job_kinds_park_without_attempts_then_drain(dsn, db):
    parked_id = enqueue(db, queue="background", kind="dion.deliver", key="parked", payload={})
    expired_id = enqueue(
        db, queue="background", kind="dion.deliver", key="expired", payload={},
        deadline_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    mail_id = enqueue(db, queue="background", kind="mail.send", key="active", payload={})
    db.commit()
    handled = []
    terminal = []

    def terminal_outcome(conn, job, reason):
        terminal.append((job.id, reason))

    handlers = {"mail.send": lambda job: handled.append(job.id)}
    assert claim(dsn, "background", active_kinds=[]) is None
    result = run_once(
        dsn, "background", handlers, active_kinds=handlers.keys(),
        terminal_handlers={"dion.deliver": terminal_outcome},
    )
    assert result.id == mail_id
    assert terminal == [(expired_id, "deadline_exceeded")]
    assert run_once(dsn, "background", handlers, active_kinds=handlers.keys()) is None
    assert db.execute(
        "SELECT state, attempt, last_error FROM jobs WHERE id = %s", (parked_id,),
    ).fetchone() == {"state": "queued", "attempt": 0, "last_error": None}

    handlers["dion.deliver"] = lambda job: handled.append(job.id)
    assert run_once(dsn, "background", handlers, active_kinds=handlers.keys()).id == parked_id
    assert handled == [mail_id, parked_id]
    assert db.execute("SELECT state FROM jobs WHERE id = %s", (parked_id,)).fetchone()["state"] == "succeeded"


def test_unfiltered_worker_still_reports_unknown_kinds(dsn, db):
    job_id = enqueue(db, queue="background", kind="unknown", key="unfiltered", payload={})
    db.commit()
    assert run_once(dsn, "background", {}).id == job_id
    assert db.execute(
        "SELECT state, last_error FROM jobs WHERE id = %s", (job_id,),
    ).fetchone() == {"state": "failed", "last_error": "unknown_job_kind"}
