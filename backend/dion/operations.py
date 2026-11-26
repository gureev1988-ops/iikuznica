"""Operator reconciliation of an exact DION delivery attempt."""

from collections.abc import Callable
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.db import connect, lock_source_catalog
from backend.runtime import DeliveryStateConflict, enqueue, reconcile_delivery

from .adapter import DELIVER, lock_delivery_receipts
from .audit import record_outcome


class DionDeliveryDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    state: Literal["sent", "failed"]
    evidence: str = Field(min_length=1, max_length=1000)
    message_id: UUID | None = None
    retry: bool = False

    @model_validator(mode="after")
    def validate_decision(self):
        if (self.state == "sent") != (self.message_id is not None):
            raise ValueError("A successful DION delivery requires the final provider message ID")
        if self.retry and self.state != "failed":
            raise ValueError("Retry requires definitive evidence of non-delivery")
        return self


class DionDeliveryDecisionResult(BaseModel):
    attempt_id: UUID
    state: Literal["sent", "failed"]
    retry_job_id: UUID | None


def dion_operations_router(dsn: str | None, authorize_ops: Callable) -> APIRouter:
    router = APIRouter(prefix="/v1/ops/dion", tags=["operations"])

    @router.post(
        "/delivery-attempts/{attempt_id}/reconcile",
        dependencies=[Depends(authorize_ops)],
        response_model=DionDeliveryDecisionResult,
    )
    def reconcile_dion_delivery(attempt_id: UUID, decision: DionDeliveryDecision):
        with connect(dsn) as conn:
            return _reconcile_attempt(conn, dsn, attempt_id, decision)

    return router


def _reconcile_attempt(conn, dsn, attempt_id, decision):
    row = conn.execute(
        """
        SELECT f.bot_id FROM delivery_attempts a
        JOIN dion_outbox o ON a.effect_key = o.effect_key
        JOIN dion_feeds f ON f.conversation_id = o.conversation_id
        WHERE a.id = %s AND a.channel = 'dion'
        """,
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail={"code": "dion_attempt_not_found"})

    lock_source_catalog(conn)
    lock_delivery_receipts(conn, row["bot_id"])
    attempt = conn.execute(
        """
        SELECT a.state, a.provider_id, a.effect_key,
               o.confirmed_message_id, o.conversation_id, o.case_id, o.turn_id
        FROM delivery_attempts a JOIN dion_outbox o ON a.effect_key = o.effect_key
        WHERE a.id = %s FOR UPDATE OF a
        """,
        (attempt_id,),
    ).fetchone()
    if decision.state == "sent" and attempt["confirmed_message_id"] not in (None, decision.message_id):
        raise HTTPException(409, detail={"code": "dion_message_id_conflict"})
    try:
        result = reconcile_delivery(
            dsn, attempt_id=attempt_id, state=decision.state, conn=conn,
            provider_id=str(decision.message_id) if decision.message_id and not attempt["provider_id"] else None,
            detail=f"operator_reconciliation: {decision.evidence}",
        )
    except DeliveryStateConflict:
        raise HTTPException(409, detail={"code": "dion_attempt_state_conflict"}) from None

    if decision.state == "sent":
        conn.execute(
            "UPDATE dion_outbox SET confirmed_message_id = %s WHERE effect_key = %s",
            (decision.message_id, attempt["effect_key"]),
        )
    record_outcome(
        conn, attempt, attempt_id, decision.state,
        provider_id=result.provider_id, message_id=decision.message_id,
    )

    retry_job_id = None
    if decision.retry:
        retry_job_id = _retry_failed_attempt(conn, attempt_id, attempt)
    return {"attempt_id": attempt_id, "state": decision.state, "retry_job_id": retry_job_id}


def _retry_failed_attempt(conn, attempt_id, attempt):
    retry_key = f"{attempt['effect_key']}:after:{attempt_id}"
    existing = conn.execute(
        "SELECT id FROM jobs WHERE queue = 'background' AND kind = %s AND business_key = %s",
        (DELIVER, retry_key),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    latest = conn.execute(
        """
        SELECT id FROM delivery_attempts WHERE channel = 'dion' AND effect_key = %s
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (attempt["effect_key"],),
    ).fetchone()
    if latest["id"] != attempt_id:
        raise HTTPException(409, detail={"code": "dion_attempt_superseded"})
    return enqueue(
        conn, queue="background", kind=DELIVER, key=retry_key,
        payload={"effect_key": attempt["effect_key"]},
    )
