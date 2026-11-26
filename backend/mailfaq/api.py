"""Quality, employee correspondence and mail operations HTTP routes."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from backend.config import HACKATHON_DOCUMENT_SOURCES, HACKATHON_QUALITY_EMAIL, INTEGRATED_QUALITY_EMAIL
from backend.db import connect

from .faq import MailError, replace_faq, set_coverage, withdraw_faq
from .service import MailService, WAITING_STATES, bind_inbound, send_followup
from .transport import MailConfig


class RequestSummary(BaseModel):
    id: UUID
    conversation_id: UUID
    case_id: UUID
    turn_id: UUID
    state: str
    pending: list[str]
    updated_at: datetime


class InboundSummary(BaseModel):
    id: UUID
    request_id: UUID | None
    message_id: str | None
    trusted: bool
    state: str
    reason: str | None
    created_at: datetime


class QualityQueue(BaseModel):
    requests: list[RequestSummary]
    inbound: list[InboundSummary]


class InboundBinding(BaseModel):
    request_id: UUID


class FollowupBody(BaseModel):
    client_id: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=8000)


class OutboundAccepted(BaseModel):
    outbound_id: UUID


class FaqVersion(BaseModel):
    version: int = Field(gt=0)


class CoverFaqBody(FaqVersion):
    revision_id: UUID


class ReplaceFaqBody(FaqVersion):
    text: str = Field(min_length=1, max_length=8000)


class FaqSummary(BaseModel):
    id: UUID
    version: int
    status: str
    current_revision_id: UUID
    covered_revision_id: UUID | None
    conflict_revision_id: UUID | None


class FaqConflict(FaqSummary):
    priority: Literal["highest"] = "highest"


class Replacement(BaseModel):
    id: UUID
    version: int
    revision_id: UUID
    fragment_id: UUID
    conflict_revision_id: UUID | None


class CursorSummary(BaseModel):
    uidvalidity: str
    last_uid: int
    updated_at: datetime


class OutboundSummary(BaseModel):
    id: UUID
    request_id: UUID
    state: str
    attempt_id: UUID | None
    reason: str | None


class MailStatus(BaseModel):
    mode: Literal["disabled", "simulator", "network"]
    cursor: CursorSummary | None
    outbound: list[OutboundSummary]


class ReconciliationBody(BaseModel):
    attempt_id: UUID
    state: Literal["sent", "failed"]
    detail: str = Field(min_length=1, max_length=400)
    retry: bool = False


class ReconciliationResult(BaseModel):
    id: UUID
    attempt_id: UUID
    state: str


def _role(actor, role: str) -> None:
    if role not in actor.roles:
        raise HTTPException(403, detail=f"{role}_role_required")


def _raise(error: MailError) -> None:
    raise HTTPException(error.status, detail=error.code) from None


def build_router(dsn: str | None, get_actor, *, service: MailService | None = None, is_quality=None) -> APIRouter:
    router = APIRouter()
    service = service or MailService(dsn, MailConfig.for_hackathon())
    limit_type = Annotated[int, Query(ge=1, le=500)]
    hackathon = service.config.hackathon
    recipient = service.config.scoped_recipient
    mailbox = service.config.cursor_key if hackathon else None

    def require_quality(actor=Depends(get_actor)):
        _role(actor, "quality")
        expected = INTEGRATED_QUALITY_EMAIL if service.config.mode == "network" else HACKATHON_QUALITY_EMAIL
        allowed = is_quality(actor) if is_quality is not None else (actor.email or "").casefold() == expected
        if not allowed:
            raise HTTPException(403, detail="quality_identity_required")

    @router.get("/v1/quality/requests", response_model=QualityQueue)
    def quality_queue(limit: limit_type = 100, _=Depends(require_quality)):
        with connect(dsn) as conn:
            requests = conn.execute(
                """
                SELECT id, conversation_id, case_id, turn_id, state, pending, updated_at
                FROM mail_requests WHERE state = ANY(%s) AND (%s::text IS NULL OR recipient = %s)
                ORDER BY updated_at, id LIMIT %s
                """,
                (list(WAITING_STATES), recipient, recipient, limit),
            ).fetchall()
            inbox = conn.execute(
                """
                SELECT id, request_id, message_id, trusted, state, reason, created_at
                FROM mail_inbound WHERE state IN ('review', 'rejected')
                    AND (%s::text IS NULL OR mailbox = %s)
                ORDER BY created_at, id LIMIT %s
                """,
                (mailbox, mailbox, limit),
            ).fetchall()
        return {"requests": requests, "inbound": inbox}

    @router.get(
        "/v1/quality/inbound/{inbound_id}/raw", response_class=Response,
        responses={200: {"content": {"message/rfc822": {"schema": {"type": "string", "format": "binary"}}}}},
    )
    def read_raw(inbound_id: UUID, _=Depends(require_quality)):
        with connect(dsn) as conn:
            row = conn.execute(
                "SELECT raw FROM mail_inbound WHERE id = %s AND (%s::text IS NULL OR mailbox = %s)",
                (inbound_id, mailbox, mailbox),
            ).fetchone()
        if row is None:
            raise HTTPException(404, detail="inbound_mail_not_found")
        return Response(
            row["raw"], media_type="message/rfc822",
            headers={
                "Content-Disposition": "attachment; filename=message.eml",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/v1/quality/inbound/{inbound_id}/bind", response_model=InboundSummary)
    def bind(inbound_id: UUID, body: InboundBinding, _=Depends(require_quality)):
        try:
            with connect(dsn) as conn:
                return bind_inbound(conn, inbound_id, body.request_id, config=service.config)
        except MailError as exc:
            _raise(exc)

    @router.post("/v1/mail/requests/{request_id}/followup", status_code=202, response_model=OutboundAccepted)
    def followup(request_id: UUID, body: FollowupBody, actor=Depends(get_actor)):
        _role(actor, "employee")
        try:
            with connect(dsn) as conn:
                outbound_id = send_followup(
                    conn, request_id, actor_id=actor.id, client_id=body.client_id, text=body.text,
                    recipient=recipient,
                )
        except MailError as exc:
            _raise(exc)
        return {"outbound_id": outbound_id}

    @router.get("/v1/quality/conflicts", response_model=list[FaqConflict])
    def conflicts(limit: limit_type = 100, _=Depends(require_quality)):
        with connect(dsn) as conn:
            return conn.execute(
                """
                SELECT f.id, f.version, f.status, f.current_revision_id, f.covered_revision_id, f.conflict_revision_id
                FROM faq_entries f JOIN mail_requests m ON m.id = f.request_id
                JOIN source_revisions r ON r.id = f.conflict_revision_id
                JOIN sources s ON s.id = r.source_id
                WHERE f.status = 'active' AND (%s::text IS NULL OR m.recipient = %s)
                  AND (NOT %s OR (s.source_key = ANY(%s) AND s.acl_actor_ids IS NULL
                      AND r.acl_actor_ids IS NULL AND r.upstream_url IS NULL))
                ORDER BY f.updated_at, f.id LIMIT %s
                """,
                (recipient, recipient, hackathon, list(HACKATHON_DOCUMENT_SOURCES), limit),
            ).fetchall()

    @router.post("/v1/quality/faq/{faq_id}/coverage", response_model=FaqSummary)
    def cover(faq_id: UUID, body: CoverFaqBody, _=Depends(require_quality)):
        try:
            with connect(dsn) as conn:
                return set_coverage(conn, faq_id, body.version, body.revision_id, hackathon=hackathon)
        except MailError as exc:
            _raise(exc)

    @router.post("/v1/quality/faq/{faq_id}/withdraw", response_model=FaqSummary)
    def withdraw(faq_id: UUID, body: FaqVersion, _=Depends(require_quality)):
        try:
            with connect(dsn) as conn:
                return withdraw_faq(conn, faq_id, body.version, hackathon=hackathon)
        except MailError as exc:
            _raise(exc)

    @router.post("/v1/quality/faq/{faq_id}/replace", response_model=Replacement)
    def replace(faq_id: UUID, body: ReplaceFaqBody, _=Depends(require_quality)):
        try:
            with connect(dsn) as conn:
                match = replace_faq(conn, faq_id, body.version, body.text, hackathon=hackathon)
        except MailError as exc:
            _raise(exc)
        return {
            "id": match.faq_id, "version": match.version, "revision_id": match.revision_id,
            "fragment_id": match.fragment_id, "conflict_revision_id": match.conflict_revision_id,
        }

    @router.get("/v1/admin/mail/status", response_model=MailStatus)
    def status(limit: limit_type = 100, actor=Depends(get_actor)):
        _role(actor, "ops")
        with connect(dsn) as conn:
            cursor = conn.execute(
                "SELECT uidvalidity, last_uid, updated_at FROM mail_cursors WHERE mailbox = %s",
                (service.config.cursor_key,),
            ).fetchone()
            outbound = conn.execute(
                """
                SELECT o.id, o.request_id,
                    CASE WHEN o.state = 'cancelled' AND a.state = 'failed' THEN o.state
                         ELSE coalesce(a.state, o.state) END AS state,
                    a.id AS attempt_id,
                    a.detail AS reason
                FROM mail_outbound o JOIN mail_requests r ON r.id = o.request_id LEFT JOIN LATERAL (
                    SELECT a.id, a.state, a.detail FROM delivery_attempts a
                    WHERE a.channel = 'smtp' AND a.effect_key = o.id::text
                    ORDER BY a.started_at DESC, a.id DESC LIMIT 1
                ) a ON true
                WHERE (%s::text IS NULL OR r.recipient = %s)
                ORDER BY o.updated_at DESC, o.id LIMIT %s
                """,
                (recipient, recipient, limit),
            ).fetchall()
        return {"mode": service.config.mode, "cursor": cursor, "outbound": outbound}

    @router.post("/v1/admin/mail/outbound/{outbound_id}/reconcile", response_model=ReconciliationResult)
    def reconcile(outbound_id: UUID, body: ReconciliationBody, actor=Depends(get_actor)):
        _role(actor, "ops")
        try:
            return service.reconcile_outbound(
                outbound_id, body.attempt_id, body.state, detail=body.detail, retry=body.retry,
            )
        except MailError as exc:
            _raise(exc)

    return router
