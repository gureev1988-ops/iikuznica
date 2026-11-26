"""Health and protected diagnostic endpoints for the runtime."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from backend.db import connect


class Liveness(BaseModel):
    status: Literal["ok"]


class Readiness(BaseModel):
    status: Literal["ready", "unavailable"]
    database: Literal["available", "unavailable"]


class IntegrationStates(BaseModel):
    integrations: dict[str, Any]


class JobStatus(BaseModel):
    id: UUID
    queue: Literal["interactive", "background"]
    kind: str
    state: Literal["queued", "running", "succeeded", "failed"]
    attempt: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    available_at: datetime
    deadline_at: datetime | None
    first_started_at: datetime | None
    finished_at: datetime | None
    queue_wait_seconds: float | None


class JobStatuses(BaseModel):
    jobs: list[JobStatus]


class ModelCost(BaseModel):
    scope: Literal["turn", "case", "background"]
    scope_id: UUID | None
    calls: int
    failed_calls: int
    unresolved_calls: int
    input_tokens: int | None
    output_tokens: int | None
    known_cost: str | None
    unknown_cost_calls: int


class ModelCosts(BaseModel):
    costs: list[ModelCost]


class ModelCall(BaseModel):
    id: UUID
    job_id: UUID | None
    turn_id: UUID | None
    case_id: UUID | None
    role: str
    trace_id: str
    model: str
    route: str
    state: Literal["calling", "succeeded", "failed", "outcome_unknown"]
    started_at: datetime
    finished_at: datetime | None
    input_tokens: int | None
    output_tokens: int | None
    cost: str | None
    detail: str | None


class ModelCalls(BaseModel):
    calls: list[ModelCall]


class TurnLatency(BaseModel):
    turn_id: UUID
    case_id: UUID
    first_useful_seconds: float | None
    verified_answer_seconds: float | None
    human_wait_seconds: float | None


class TurnLatencies(BaseModel):
    latencies: list[TurnLatency]


class DeliveryAttempt(BaseModel):
    id: UUID
    channel: Literal["smtp", "dion"]
    effect_key: str
    job_id: UUID | None
    state: Literal["sending", "sent", "failed", "delivery_unknown"]
    provider_id: str | None
    detail: str | None
    started_at: datetime
    updated_at: datetime


class DeliveryAttempts(BaseModel):
    deliveries: list[DeliveryAttempt]


def model_costs(
    dsn: str | None,
    *,
    turn_id: UUID | None = None,
    case_id: UUID | None = None,
) -> list[dict]:
    """Aggregate physical attempts by turn, case or background work."""
    with connect(dsn) as conn:
        return conn.execute(
            """
            WITH selected AS (
                SELECT m.*, coalesce(j.queue = 'background', m.turn_id IS NULL) AS background
                FROM model_calls m LEFT JOIN jobs j ON j.id = m.job_id
                WHERE (%(turn_id)s::uuid IS NULL OR m.turn_id = %(turn_id)s)
                    AND (%(case_id)s::uuid IS NULL OR m.case_id = %(case_id)s)
            ), scopes AS (
                SELECT 'turn' AS scope, turn_id AS scope_id, selected.* FROM selected WHERE turn_id IS NOT NULL
                UNION ALL
                SELECT 'case', case_id, selected.* FROM selected WHERE case_id IS NOT NULL
                UNION ALL
                SELECT 'background', NULL, selected.* FROM selected WHERE background
            )
            SELECT scope, scope_id, count(*) AS calls,
                count(*) FILTER (WHERE state = 'failed') AS failed_calls,
                count(*) FILTER (WHERE state IN ('calling', 'outcome_unknown')) AS unresolved_calls,
                sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                sum(cost)::text AS known_cost, count(*) FILTER (WHERE cost IS NULL) AS unknown_cost_calls
            FROM scopes GROUP BY scope, scope_id ORDER BY scope, scope_id
            """,
            {"turn_id": turn_id, "case_id": case_id},
        ).fetchall()


def operations_router(
    dsn: str | None,
    authorize_ops: Callable,
    integrations: Mapping[str, Callable[[], object]] | None = None,
) -> APIRouter:
    deps = [Depends(authorize_ops)]
    router = APIRouter()

    @router.get("/health/live", response_model=Liveness)
    def live() -> dict:
        return {"status": "ok"}

    @router.get("/health/ready", response_model=Readiness, responses={503: {"model": Readiness}})
    def ready(response: Response) -> dict:
        try:
            with connect(dsn) as conn:
                conn.execute("SELECT 1 FROM jobs LIMIT 0")
        except (psycopg.Error, ValueError):
            response.status_code = 503
            return {"status": "unavailable", "database": "unavailable"}
        return {"status": "ready", "database": "available"}

    @router.get("/v1/ops/integrations", dependencies=deps, response_model=IntegrationStates)
    def integration_states() -> dict:
        statuses = {}
        for name, probe in (integrations or {}).items():
            try:
                statuses[name] = probe()
            except Exception as error:
                statuses[name] = {"status": "unavailable", "reason": type(error).__name__}
        return {"integrations": statuses}

    @router.get("/v1/ops/jobs", dependencies=deps, response_model=JobStatuses)
    def jobs(
        queue: str | None = None,
        state: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        with connect(dsn) as conn:
            rows = conn.execute(
                """
                SELECT id, queue, kind, state, attempt, max_attempts, last_error,
                    created_at, available_at, deadline_at, first_started_at, finished_at,
                    extract(epoch FROM first_started_at - created_at) AS queue_wait_seconds
                FROM jobs WHERE (%(queue)s::text IS NULL OR queue = %(queue)s)
                    AND (%(state)s::text IS NULL OR state = %(state)s)
                ORDER BY created_at DESC, id LIMIT %(limit)s
                """,
                {"queue": queue, "state": state, "limit": limit},
            ).fetchall()
        return {"jobs": rows}

    @router.get("/v1/ops/model-costs", dependencies=deps, response_model=ModelCosts)
    def cost_report(turn_id: UUID | None = None, case_id: UUID | None = None) -> dict:
        return {"costs": model_costs(dsn, turn_id=turn_id, case_id=case_id)}

    @router.get("/v1/ops/model-calls", dependencies=deps, response_model=ModelCalls)
    def model_calls(
        turn_id: UUID | None = None,
        case_id: UUID | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        with connect(dsn) as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, turn_id, case_id, role, trace_id, model, route,
                    state, started_at, finished_at, input_tokens, output_tokens,
                    cost::text AS cost, detail
                FROM model_calls WHERE (%(turn_id)s::uuid IS NULL OR turn_id = %(turn_id)s)
                    AND (%(case_id)s::uuid IS NULL OR case_id = %(case_id)s)
                ORDER BY started_at DESC, id LIMIT %(limit)s
                """,
                {"turn_id": turn_id, "case_id": case_id, "limit": limit},
            ).fetchall()
        return {"calls": rows}

    @router.get("/v1/ops/latencies", dependencies=deps, response_model=TurnLatencies)
    def latencies(
        case_id: UUID | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        with connect(dsn) as conn:
            rows = conn.execute(
                """
                SELECT t.id AS turn_id, t.case_id,
                    extract(epoch FROM metrics.first_useful - t.created_at) AS first_useful_seconds,
                    extract(epoch FROM metrics.verified_answer - t.created_at) AS verified_answer_seconds,
                    extract(epoch FROM metrics.staff_answer - metrics.offer_confirmed) AS human_wait_seconds
                FROM turns t
                CROSS JOIN LATERAL (
                    SELECT min(e.created_at) FILTER (WHERE e.kind = 'turn.completed'
                            OR e.kind IN ('mail.resolution', 'mail.partial', 'mail.counter_question')) AS first_useful,
                        min(e.created_at) FILTER (WHERE e.kind = 'mail.resolution' OR (
                            e.kind = 'turn.completed' AND e.payload->'result'->>'action' = 'answer'
                        )) AS verified_answer,
                        min(e.created_at) FILTER (WHERE e.kind = 'offer.confirmed') AS offer_confirmed,
                        min(e.created_at) FILTER (
                            WHERE e.kind IN ('mail.resolution', 'mail.partial', 'mail.counter_question')
                        ) AS staff_answer
                    FROM events e WHERE e.conversation_id = t.conversation_id AND e.turn_id = t.id
                ) AS metrics
                WHERE (%(case_id)s::uuid IS NULL OR t.case_id = %(case_id)s)
                ORDER BY t.created_at DESC, t.id LIMIT %(limit)s
                """,
                {"case_id": case_id, "limit": limit},
            ).fetchall()
        return {"latencies": rows}

    @router.get("/v1/ops/deliveries", dependencies=deps, response_model=DeliveryAttempts)
    def deliveries(
        state: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        with connect(dsn) as conn:
            rows = conn.execute(
                """
                SELECT id, channel, effect_key, job_id, state, provider_id,
                    detail, started_at, updated_at
                FROM delivery_attempts WHERE (%(state)s::text IS NULL OR state = %(state)s)
                ORDER BY started_at DESC, id LIMIT %(limit)s
                """,
                {"state": state, "limit": limit},
            ).fetchall()
        return {"deliveries": rows}

    return router
