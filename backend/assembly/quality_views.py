"""Public browser response types for the training quality desk."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from backend.contracts.api import SourceReference


class QualityEvidence(BaseModel):
    outcome: Literal["conflict", "consistent", "unconfirmed", "no_source"]
    reason: str
    ref: SourceReference | None
    title: str | None
    anchor: str | None
    quote: str | None
    url: str | None


class QualityHistory(BaseModel):
    version: int
    question: str
    answer: str
    at: datetime
    action: str
    actor: str


class QualityCard(BaseModel):
    id: UUID
    request_id: UUID
    version: int
    question: str
    answer: str
    created_at: datetime
    updated_at: datetime
    uses: int
    status: Literal["active", "review", "withdrawn"]
    evidence: QualityEvidence | None
    history: list[QualityHistory]


class QualityReport(BaseModel):
    id: UUID
    subject: str
    text: str
    created_at: datetime
    delivery: str


class ThreadHeader(BaseModel):
    id: UUID
    subject: str
    updated_at: datetime
    sender: str
    preview: str
    open: bool
    service: bool


class ThreadMessage(BaseModel):
    message_id: str | None
    processing: Literal["interpreting", "review"] | None = None
    sender: str
    to: str
    cc: str
    subject: str
    text: str
    at: datetime


class MailThread(ThreadHeader):
    messages: list[ThreadMessage]
    reply_message_id: str | None
