"""HTTP and SSE representations of the shared dialogue service."""

import asyncio
import json
import re
from dataclasses import asdict
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from backend.contracts.api import (
    EVENT_CURSOR_EXPIRED,
    EVENT_STREAM,
    AcceptedMessage,
    EventView,
    HistoryView,
    MessageRequest,
    OfferDecision,
    OfferView,
    SourceView,
    TurnView,
    dialogue_error_responses,
)
from backend.db import connect

from .service import Actor, DialogueError, DialogueService

_MAX_CURSOR = 2**63 - 1


def _http_error(exc: DialogueError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, **exc.details})


def _event_cursor(last_event_id: str | None, after: int | None) -> int:
    if last_event_id is None:
        return after if after is not None else 0
    if re.fullmatch(r"[0-9]{1,19}", last_event_id):
        cursor = int(last_event_id)
        if cursor <= _MAX_CURSOR:
            return cursor
    raise DialogueError(422, "invalid_event_cursor")


def create_router(service: DialogueService, get_actor) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["dialogue"])

    def execute(method, *args):
        with connect(service.dsn) as conn:
            return method(conn, *args)

    def http(method, *args):
        try:
            return execute(method, *args)
        except DialogueError as exc:
            raise _http_error(exc) from exc

    @router.post(
        "/conversations/{conversation_id}/messages", status_code=202, response_model=AcceptedMessage,
        responses=dialogue_error_responses(401, 403, 404, 409, 422, 503),
    )
    def admit(conversation_id: UUID, body: MessageRequest, actor: Actor = Depends(get_actor)):
        return http(service.admit, actor, conversation_id, body)

    @router.get(
        "/conversations/{conversation_id}/messages", response_model=HistoryView,
        responses=dialogue_error_responses(401, 403, 404, 422),
    )
    def history(conversation_id: UUID, actor: Actor = Depends(get_actor)):
        return http(service.history, actor, conversation_id)

    @router.get("/turns/{turn_id}", response_model=TurnView, responses=dialogue_error_responses(401, 403, 404, 422))
    def get_turn(turn_id: UUID, actor: Actor = Depends(get_actor)):
        return http(service.get_turn, actor, turn_id)

    @router.post(
        "/turns/{turn_id}/retry", status_code=202, response_model=AcceptedMessage,
        responses=dialogue_error_responses(401, 403, 404, 409, 422, 503),
    )
    def retry(
        turn_id: UUID,
        attempt: int | None = Query(default=None, ge=1, description="Attempt shown on the failed turn snapshot."),
        actor: Actor = Depends(get_actor),
    ):
        return http(service.retry, actor, turn_id, attempt)

    @router.post(
        "/offers/{offer_id}/confirm", response_model=OfferView,
        responses=dialogue_error_responses(401, 403, 404, 409, 422, 503),
    )
    def confirm(offer_id: UUID, body: OfferDecision, actor: Actor = Depends(get_actor)):
        return http(service.confirm, actor, offer_id, body.version)

    @router.post(
        "/offers/{offer_id}/decline", response_model=OfferView,
        responses=dialogue_error_responses(401, 403, 404, 409, 422),
    )
    def decline(offer_id: UUID, body: OfferDecision, actor: Actor = Depends(get_actor)):
        return http(service.decline, actor, offer_id, body.version)

    @router.get(
        "/sources/{revision_id}/fragments/{fragment_id}", response_model=SourceView,
        responses=dialogue_error_responses(401, 403, 404, 422),
    )
    def get_source(revision_id: UUID, fragment_id: UUID, actor: Actor = Depends(get_actor)):
        return asdict(http(service.get_source, actor, revision_id, fragment_id))

    @router.get(
        "/conversations/{conversation_id}/events",
        response_class=StreamingResponse,
        responses={**dialogue_error_responses(401, 403, 404, 409, 422), 200: EVENT_STREAM, 410: EVENT_CURSOR_EXPIRED},
    )
    async def events(
        conversation_id: UUID,
        request: Request,
        actor: Actor = Depends(get_actor),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        after: int | None = Query(default=None, ge=0, le=_MAX_CURSOR),
    ):
        try:
            cursor = _event_cursor(last_event_id, after)
            first = await run_in_threadpool(execute, service.events, actor, conversation_id, cursor)
        except DialogueError as exc:
            raise _http_error(exc) from exc

        async def stream():
            nonlocal cursor
            rows = first
            heartbeat = monotonic()
            if not rows:
                yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    return
                for row in rows:
                    event = EventView.model_validate(row)
                    cursor = event.cursor
                    yield event.encode_sse()
                if rows:
                    heartbeat = monotonic()
                else:
                    if monotonic() - heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        heartbeat = monotonic()
                    await asyncio.sleep(0.25)
                try:
                    rows = await run_in_threadpool(execute, service.events, actor, conversation_id, cursor)
                except DialogueError as exc:
                    if exc.status == 410:
                        payload = json.dumps({"code": exc.code, **exc.details})
                        yield f"event: reset\ndata: {payload}\n\n"
                    return

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
