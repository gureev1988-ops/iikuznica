"""Browser endpoints for the statistics workspace, personal ratings and documents."""
from datetime import date
from functools import lru_cache
import os
from pathlib import Path
from typing import Literal
from uuid import UUID
from xml.etree import ElementTree
from zipfile import ZipFile

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.db import connect, lock_source_catalog
from .demo_project import display_example
from .statistics import VISIBLE_DOCUMENT, group_questions, summary


class Rating(BaseModel):
    rating: Literal["up", "down"]


class MessageRating(BaseModel):
    message_id: UUID
    rating: Literal["up", "down"] | None


class DocumentEntry(BaseModel):
    document_id: UUID
    revision_id: UUID
    title: str
    version: int
    status: str
    source_key: str
    historical: bool
    upstream_url: str | None
    original_url: str | None


class DocumentSection(BaseModel):
    fragment_id: UUID | None
    anchor: str
    heading_path: list[str]
    text: str


class FullRevision(DocumentEntry):
    fragments: list[DocumentSection]


class StatsPeriod(BaseModel):
    start: date
    end: date
    timezone: str


class IssuedRegulation(BaseModel):
    document_id: UUID
    revision_id: UUID
    title: str
    version: int
    issues: int


class ProblemClause(BaseModel):
    document_id: UUID | None
    revision_id: UUID | None
    fragment_id: UUID | None
    title: str | None
    version: int | None
    anchor: str | None
    gaps: int
    conflicts: int


class RepeatedTopic(BaseModel):
    id: UUID
    topic: str
    conditions: dict[str, str]
    count: int


class RatingCounts(BaseModel):
    up: int
    down: int


class QualityStatistics(BaseModel):
    period: StatsPeriod
    documents: list[IssuedRegulation]
    problems: list[ProblemClause]
    gaps_total: int
    groups: list[RepeatedTopic]
    ratings: RatingCounts


class CaseQuestion(BaseModel):
    case_id: UUID
    question: str


DOCUMENTS = """
    SELECT s.id AS document_id,r.id AS revision_id,r.title,r.version,s.status,s.source_key,
           s.current_revision_id<>r.id AS historical,r.upstream_url,o.name,o.path,
           r.storage_body<>'' AS has_storage
    FROM sources s JOIN source_revisions r ON r.source_id=s.id
    LEFT JOIN assembly_originals o ON o.revision_id=r.id WHERE """ + VISIBLE_DOCUMENT

MESSAGES = """
    SELECT m.id,m.source_refs,m.role,t.result,rating.rating FROM messages m
    JOIN conversations c ON c.id=m.conversation_id
    JOIN turns t ON t.id=m.turn_id
    LEFT JOIN assembly_message_ratings rating ON rating.message_id=m.id
    WHERE c.owner_id=%s AND m.role='assistant' AND t.status='completed'
      AND ((m.source_refs IS NULL AND ((t.result->>'action'='answer'
               AND EXISTS (SELECT 1 FROM jsonb_array_elements(t.result->'sources') ref
                           WHERE ref->>'claim' IS DISTINCT FROM 'Открыть регламент'))
               OR EXISTS (SELECT 1 FROM jsonb_array_elements(t.result->'parts') part
                          WHERE part->>'action'='answer' AND jsonb_array_length(part->'sources')>0)))
        OR (m.source_refs IS NOT NULL AND jsonb_array_length(m.source_refs)>0))
"""


def read_documents(conn, actor_id, where, **params):
    rows = conn.execute(DOCUMENTS + where, dict(actor=actor_id, **params)).fetchall()
    for row in rows:
        base = f"/v1/browser/documents/{row['document_id']}/revisions/{row['revision_id']}"
        row["original_url"] = base + "/original" if row["path"] or row["has_storage"] else None
    return rows


def original_path(relative):
    directory = Path(os.environ.get("II_ORIGINALS", ".ii-local/originals")).resolve()
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise HTTPException(404, "Оригинал этой редакции недоступен")
    return path


@lru_cache(maxsize=32)
def docx_preface(path):
    from eval.retrieval.extract import NUMBERED, W, paragraphs
    with ZipFile(path) as archive:
        body = ElementTree.fromstring(archive.read("word/document.xml")).find(W + "body")
    lines = []
    for line in paragraphs(body):
        if NUMBERED.match(line):
            break
        if line:
            lines.append(line)
    return "\n".join(lines)


def hide_storage(row):
    return {k: v for k, v in row.items() if k not in {"name", "path", "has_storage"}}


def mount_analytics(app, backend):
    browser = backend.assembly
    def session_actor(actor=Depends(browser.dependency())):
        if actor.channel != "guest":
            raise HTTPException(403, "Откройте браузерный демостенд")
        return actor

    @app.get("/v1/browser/documents", response_model=list[DocumentEntry])
    def documents(actor=Depends(session_actor)):
        with connect(backend.dsn) as conn:
            rows = read_documents(conn, actor.id, "AND r.id=s.current_revision_id ORDER BY r.title,s.id")
        return [hide_storage(row) for row in rows]

    def get_revision(conn, actor, document_id, revision_id):
        lock_source_catalog(conn)
        rows = read_documents(conn, actor.id, "AND s.id=%(document)s AND r.id=%(revision)s",
                              document=document_id, revision=revision_id)
        if not rows:
            raise HTTPException(404, "Редакция недоступна")
        return rows[0]

    @app.get("/v1/browser/documents/{document_id}/revisions/{revision_id}", response_model=FullRevision)
    def revision(document_id: UUID, revision_id: UUID, response: Response, actor=Depends(session_actor)):
        response.headers["Cache-Control"] = "private, no-store"
        with connect(backend.dsn) as conn:
            row = get_revision(conn, actor, document_id, revision_id)
            row["fragments"] = conn.execute("""
                SELECT id AS fragment_id,anchor,heading_path,text FROM source_fragments
                WHERE revision_id=%s ORDER BY ordinal
            """, (revision_id,)).fetchall()
            if row["path"]:
                preface = docx_preface(original_path(row["path"]))
                if preface:
                    row["fragments"].insert(0, dict(fragment_id=None, anchor="", heading_path=[], text=preface))
            return hide_storage(row)

    @app.get("/v1/browser/documents/{document_id}/revisions/{revision_id}/original")
    def original(document_id: UUID, revision_id: UUID, actor=Depends(session_actor)):
        with connect(backend.dsn) as conn:
            row = get_revision(conn, actor, document_id, revision_id)
            headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
            if row["path"]:
                return FileResponse(original_path(row["path"]), filename=row["name"], headers=headers,
                                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            if not row["has_storage"]:
                raise HTTPException(404, "Оригинал этой редакции недоступен")
            body = conn.execute("SELECT storage_body FROM source_revisions WHERE id=%s", (revision_id,)).fetchone()
            # The upstream URL can change after import. Deliver immutable storage as an attachment.
            headers["Content-Disposition"] = f'attachment; filename="revision-{revision_id}.txt"'
            return Response(body["storage_body"], media_type="text/plain", headers=headers)

    @app.get("/v1/browser/conversations/{conversation_id}/ratings", response_model=list[MessageRating])
    def ratings(conversation_id: UUID, response: Response, actor=Depends(session_actor)):
        response.headers["Cache-Control"] = "private, no-store"
        with connect(backend.dsn) as conn:
            lock_source_catalog(conn)
            if not conn.execute("SELECT 1 FROM conversations WHERE id=%s AND owner_id=%s",
                                (conversation_id, actor.id)).fetchone():
                raise HTTPException(404, "Диалог недоступен")
            rows = conn.execute(MESSAGES + " AND m.conversation_id=%s", (actor.id, conversation_id))
            return [{"message_id": row["id"], "rating": row["rating"]} for row in rows.fetchall()
                    if backend.dialog._message_visible(conn, actor, row, row["result"])]

    @app.put("/v1/browser/messages/{message_id}/rating", response_model=MessageRating)
    def rate(message_id: UUID, body: Rating, actor=Depends(session_actor)):
        with connect(backend.dsn) as conn:
            lock_source_catalog(conn)
            row = conn.execute(MESSAGES + " AND m.id=%s", (actor.id, message_id)).fetchone()
            if not row or not backend.dialog._message_visible(conn, actor, row, row["result"]):
                raise HTTPException(404, "Ответ недоступен для оценки")
            conn.execute("""INSERT INTO assembly_message_ratings VALUES (%s,%s)
                ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating""", (message_id, body.rating))
        return {"message_id": message_id, "rating": body.rating}

    @app.get("/v1/browser/statistics", response_model=QualityStatistics)
    def statistics(response: Response, actor=Depends(session_actor)):
        response.headers["Cache-Control"] = "private, no-store"
        with connect(backend.dsn) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return summary(conn, actor.id)

    @app.get("/v1/browser/statistics/groups/{group_id}/questions", response_model=list[CaseQuestion])
    def questions(group_id: UUID, request: Request, response: Response, actor=Depends(session_actor)):
        browser.authorized_inbox(request)
        response.headers["Cache-Control"] = "private, no-store"
        with connect(backend.dsn) as conn:
            return display_example(group_questions(conn, group_id))
