"""Durable journal for external deliveries and physical model calls."""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Generic, TypeVar
from uuid import UUID, uuid4

from psycopg import Connection

from backend.db import connect

from .jobs import Job, require_lease


class KnownDeliveryFailure(Exception):
    """The provider definitively rejected this attempt before acceptance."""

    def __init__(self, reason: str = "provider_rejected"):
        self.reason = reason
        super().__init__(reason)


class DeliveryStateConflict(Exception):
    """The observed outcome conflicts with the attempt's current state."""


@dataclass(frozen=True)
class PendingDelivery:
    """The provider accepted a request whose outcome arrives asynchronously."""

    provider_id: str


@dataclass(frozen=True)
class DeliveryResult:
    attempt_id: UUID
    state: str
    provider_id: str | None
    detail: str | None


def _delivery_result(row: dict) -> DeliveryResult:
    return DeliveryResult(row["id"], row["state"], row["provider_id"], row["detail"])


def _update_delivery(
    conn: Connection,
    attempt_id: UUID,
    state: str,
    provider_id: str | None,
    detail: str | None,
) -> DeliveryResult:
    row = conn.execute(
        """
        UPDATE delivery_attempts SET state = %s, provider_id = coalesce(%s, provider_id),
            detail = %s, updated_at = clock_timestamp()
        WHERE id = %s RETURNING id, state, provider_id, detail
        """,
        (state, provider_id, detail, attempt_id),
    ).fetchone()
    return _delivery_result(row)


def _begin_delivery(
    conn: Connection,
    job: Job | None,
    channel: str,
    effect_key: str,
    timeout: timedelta,
) -> tuple[DeliveryResult, bool]:
    if job is not None:
        job = require_lease(conn, job)
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || %s, 0))",
        (f":delivery:{channel}:{effect_key}",),
    )
    row = conn.execute(
        """
        SELECT a.id, a.state, a.provider_id, a.detail,
            CASE WHEN a.job_id IS NULL THEN a.expires_at <= clock_timestamp()
                ELSE NOT EXISTS (
                    SELECT 1 FROM jobs j WHERE j.id = a.job_id AND j.state = 'running'
                        AND j.lease_token = a.lease_token AND j.lease_expires_at > clock_timestamp()
                ) END AS expired
        FROM delivery_attempts a
        WHERE a.channel = %s AND a.effect_key = %s AND a.state <> 'failed' FOR UPDATE
        """,
        (channel, effect_key),
    ).fetchone()
    if row:
        if row["state"] == "sending" and row["expired"]:
            return _update_delivery(conn, row["id"], "delivery_unknown", None, "sender_interrupted"), False
        return _delivery_result(row), False

    row = conn.execute(
        """
        INSERT INTO delivery_attempts (id, channel, effect_key, job_id, lease_token, state, expires_at)
        VALUES (%s, %s, %s, %s, %s, 'sending', clock_timestamp() + %s)
        RETURNING id, state, provider_id, detail
        """,
        (uuid4(), channel, effect_key, job.id if job else None, job.lease_token if job else None, timeout),
    ).fetchone()
    return _delivery_result(row), True


def send_once(
    dsn: str | None,
    *,
    channel: str,
    effect_key: str,
    send: Callable[[], str | PendingDelivery | None],
    job: Job | None = None,
    sending_timeout_seconds: float = 30,
) -> DeliveryResult:
    """Send a business effect after committing a durable sending record.

    A settled or ambiguous attempt suppresses further sends for the same key.
    Supply a PendingDelivery when acceptance has an asynchronous outcome.
    """
    if sending_timeout_seconds <= 0:
        raise ValueError("sending_timeout_seconds must be positive")
    with connect(dsn) as conn:
        attempt, should_send = _begin_delivery(
            conn, job, channel, effect_key, timedelta(seconds=sending_timeout_seconds)
        )
    if not should_send:
        return attempt

    provider_id = None
    detail = None
    try:
        result = send()
    except KnownDeliveryFailure as error:
        state, detail = "failed", error.reason
    except Exception as error:
        state, detail = "delivery_unknown", type(error).__name__
    else:
        if isinstance(result, PendingDelivery):
            state, provider_id, detail = "delivery_unknown", result.provider_id, "provider_pending"
        else:
            state, provider_id = "sent", result

    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT id, state, provider_id, detail FROM delivery_attempts WHERE id = %s FOR UPDATE",
            (attempt.attempt_id,),
        ).fetchone()
        # The owner of this physical attempt may receive a late provider response.
        # Preserve a final outcome already confirmed by reconciliation.
        if row["state"] in ("sending", "delivery_unknown"):
            return _update_delivery(conn, attempt.attempt_id, state, provider_id, detail)
        return _delivery_result(row)


def reconcile_delivery(
    dsn: str | None,
    *,
    attempt_id: UUID,
    state: str,
    provider_id: str | None = None,
    detail: str | None = None,
    conn: Connection | None = None,
) -> DeliveryResult:
    """Record authoritative evidence for an ambiguous delivery.

    The caller may supply its own transaction to update related records
    atomically. State failed allows another send for the business effect.
    """
    if state not in ("sent", "failed"):
        raise ValueError("reconciliation requires a final delivery state")
    with nullcontext(conn) if conn is not None else connect(dsn) as transaction:
        row = transaction.execute(
            "SELECT id, state, provider_id, detail FROM delivery_attempts WHERE id = %s FOR UPDATE",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise LookupError(attempt_id)
        if provider_id is not None and row["provider_id"] not in (None, provider_id):
            raise DeliveryStateConflict(f"provider identifier differs for attempt {attempt_id}")
        if row["state"] == state:
            return _delivery_result(row)
        if row["state"] != "delivery_unknown":
            raise DeliveryStateConflict(f"attempt {attempt_id} is {row['state']}")
        return _update_delivery(transaction, attempt_id, state, provider_id, detail)


Value = TypeVar("Value")


@dataclass(frozen=True)
class ModelResult(Generic[Value]):
    value: Value
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | int | None = None
    route: str | None = None
    model: str | None = None


class ModelCallFailure(Exception):
    """A failed call for which the provider reported usage or route metadata."""

    def __init__(
        self,
        reason: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost: Decimal | int | None = None,
        route: str | None = None,
        model: str | None = None,
    ):
        self.result = ModelResult(None, input_tokens, output_tokens, cost, route, model)
        super().__init__(reason)


def _finish_model_call(
    dsn: str | None,
    call_id: UUID,
    state: str,
    result: ModelResult,
    detail: str | None = None,
) -> None:
    with connect(dsn) as conn:
        conn.execute(
            """
            UPDATE model_calls SET state = %s, finished_at = clock_timestamp(), input_tokens = %s,
                output_tokens = %s, cost = %s, route = coalesce(%s, route),
                model = coalesce(%s, model), detail = %s
            WHERE id = %s
            """,
            (
                state, result.input_tokens, result.output_tokens, result.cost,
                result.route, result.model, detail, call_id,
            ),
        )


def call_model(
    dsn: str | None,
    fn: Callable[[], Value | ModelResult[Value]],
    *,
    role: str,
    trace_id: str,
    model: str,
    route: str,
    turn_id: UUID | str | None = None,
    case_id: UUID | str | None = None,
    job: Job | None = None,
    timeout_seconds: float = 120,
) -> Value:
    """Record one physical call. Invoke this method inside any provider retry loop."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    call_id = uuid4()
    with connect(dsn) as conn:
        if job is not None:
            job = require_lease(conn, job)
        conn.execute(
            """
            INSERT INTO model_calls
                (id, job_id, lease_token, turn_id, case_id, role, trace_id, model, route, state, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'calling', clock_timestamp() + %s)
            """,
            (
                call_id, job.id if job else None, job.lease_token if job else None,
                turn_id, case_id, role, trace_id, model, route, timedelta(seconds=timeout_seconds),
            ),
        )

    try:
        response = fn()
    except Exception as error:
        result = error.result if isinstance(error, ModelCallFailure) else ModelResult(None)
        _finish_model_call(dsn, call_id, "failed", result, type(error).__name__)
        raise
    result = response if isinstance(response, ModelResult) else ModelResult(response)
    _finish_model_call(dsn, call_id, "succeeded", result)
    return result.value


def recover_journal(dsn: str | None, *, batch_size: int = 100) -> tuple[int, int]:
    """Move abandoned network attempts into explicit unknown states."""
    with connect(dsn) as conn:
        delivery_count = conn.execute(
            """
            WITH abandoned AS (
                SELECT a.id FROM delivery_attempts a
                WHERE a.state = 'sending' AND (
                    (a.job_id IS NULL AND a.expires_at <= clock_timestamp()) OR
                    (a.job_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM jobs j WHERE j.id = a.job_id AND j.state = 'running'
                            AND j.lease_token = a.lease_token AND j.lease_expires_at > clock_timestamp()
                    ))
                )
                ORDER BY a.expires_at, a.id LIMIT %s FOR UPDATE SKIP LOCKED
            )
            UPDATE delivery_attempts SET state = 'delivery_unknown',
                detail = 'sender_interrupted', updated_at = clock_timestamp()
            FROM abandoned WHERE delivery_attempts.id = abandoned.id
            """,
            (batch_size,),
        ).rowcount
        model_count = conn.execute(
            """
            WITH abandoned AS (
                SELECT a.id FROM model_calls a WHERE a.state = 'calling' AND (
                    a.expires_at <= clock_timestamp() OR
                    (a.job_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM jobs j WHERE j.id = a.job_id AND j.state = 'running'
                            AND j.lease_token = a.lease_token AND j.lease_expires_at > clock_timestamp()
                    ))
                )
                ORDER BY a.expires_at, a.id LIMIT %s FOR UPDATE SKIP LOCKED
            )
            UPDATE model_calls SET state = 'outcome_unknown', detail = 'caller_interrupted',
                finished_at = clock_timestamp()
            FROM abandoned WHERE model_calls.id = abandoned.id
            """,
            (batch_size,),
        ).rowcount
    return delivery_count, model_count
