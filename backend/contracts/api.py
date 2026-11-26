"""Public v1 dialogue models shared by FastAPI and the client contract."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from .engine import Action, SourceKind

TurnStatus = Literal["queued", "processing", "completed", "technical_failure"]
ClarificationStatus = Literal["open", "resolved", "superseded"]
OfferStatus = Literal["open", "confirmed", "declined", "superseded"]


class MessageRequest(BaseModel):
    client_message_id: str = Field(min_length=1, max_length=256)
    text: str | None = Field(default=None, max_length=16_000)
    case_id: UUID | None = None
    clarification_id: UUID | None = None
    clarification_version: int | None = Field(default=None, ge=1)
    option_id: str | None = Field(default=None, min_length=1, max_length=256)


class AcceptedMessage(BaseModel):
    conversation_id: UUID
    case_id: UUID
    turn_id: UUID


class OfferDecision(BaseModel):
    version: int = Field(ge=1)


class MessageView(BaseModel):
    id: UUID
    case_id: UUID
    turn_id: UUID | None = None
    client_message_id: str | None = None
    role: Literal["user", "assistant", "system"]
    text: str
    created_at: datetime


class ClarificationOptionView(BaseModel):
    id: str
    text: str
    conditions: dict[str, str] = Field(default_factory=dict)


class ClarificationView(BaseModel):
    id: UUID
    turn_id: UUID
    case_id: UUID
    version: int
    status: ClarificationStatus
    prompt: str
    options: list[ClarificationOptionView]


class OfferView(BaseModel):
    id: UUID
    turn_id: UUID
    case_id: UUID
    version: int
    status: OfferStatus
    text: str


class SourceReference(BaseModel):
    document_id: UUID
    revision_id: UUID
    fragment_id: UUID
    kind: SourceKind
    quote: str
    url: str
    claim: str | None = None


class ResultView(BaseModel):
    action: Action
    text: str
    sources: list[SourceReference] = Field(default_factory=list)
    notice: str | None = None


class TurnView(BaseModel):
    turn_id: UUID
    conversation_id: UUID
    case_id: UUID
    status: TurnStatus
    question: str
    result: ResultView | None
    error: str | None
    attempt: int
    clarifications: list[ClarificationView]
    offers: list[OfferView]
    cursor: int


class HistoryView(BaseModel):
    conversation_id: UUID
    cursor: int
    messages: list[MessageView]
    turns: list[TurnView]


class SourceView(BaseModel):
    document_id: UUID
    revision_id: UUID
    fragment_id: UUID
    text: str
    title: str
    anchor: str
    kind: SourceKind


class EventView(BaseModel):
    conversation_id: UUID
    case_id: UUID
    turn_id: UUID | None
    cursor: int
    kind: str
    payload: dict[str, Any]
    created_at: datetime

    def encode_sse(self) -> str:
        return f"id: {self.cursor}\nevent: {self.kind}\ndata: {self.model_dump_json()}\n\n"


class EventCursorExpired(BaseModel):
    code: Literal["event_cursor_expired"] = "event_cursor_expired"
    cursor: int


class EventCursorExpiredResponse(BaseModel):
    detail: EventCursorExpired


EVENT_CURSOR_EXPIRED = {
    "model": EventCursorExpiredResponse,
    "description": "Cursor expired. Read a new conversation snapshot.",
}


EVENT_STREAM = {
    "description": (
        "Server-sent events. Resume with Last-Event-ID from the same conversation. "
        "After a 410 response, read the conversation snapshot and resume at its cursor."
    ),
    "content": {
        "text/event-stream": {
            "schema": {"type": "string"},
            "x-event-schema": EventView.model_json_schema(),
            "x-reset-schema": EventCursorExpired.model_json_schema(),
        }
    },
}
