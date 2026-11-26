"""Versioned, synthetic fixtures shared by both AnswerEngine implementations."""

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.db import lock_source_catalog

FIXTURE_SOURCE_KEY = "synthetic-v1"


class FixtureRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FixtureOption(FixtureRecord):
    id: str
    text: str


class FixtureScenario(FixtureRecord):
    questions: tuple[str, ...]
    action: Literal["answer", "clarify", "escalate", "technical_failure"]
    text: str = ""
    document_id: UUID | None = None
    condition_key: str | None = None
    options: tuple[FixtureOption, ...] = ()


class FixtureRevision(FixtureRecord):
    version: int
    revision_id: UUID
    fragment_id: UUID
    text: str


class FixtureSource(FixtureRecord):
    document_id: UUID
    external_id: str
    title: str
    anchor: str
    heading: str
    revisions: tuple[FixtureRevision, ...]


class FixtureSet(FixtureRecord):
    version: Literal[1]
    scenarios: tuple[FixtureScenario, ...]
    sources: tuple[FixtureSource, ...]


@lru_cache(maxsize=1)
def load_fixture() -> FixtureSet:
    path = Path(__file__).with_name("fixtures") / "v1.json"
    return FixtureSet.model_validate_json(path.read_text(encoding="utf-8"))


def seed_fixture_sources(conn, *, active_version: int = 1, hackathon: bool = False) -> None:
    """Install eligible synthetic revisions and activate the requested version."""
    fixture = load_fixture()
    if any(active_version not in {rev.version for rev in source.revisions} for source in fixture.sources):
        raise ValueError(f"Unknown synthetic source version: {active_version}")
    with conn.transaction():
        lock_source_catalog(conn, write=True)
        for source in fixture.sources:
            revisions = _local_public_revisions(conn, source) if hackathon else source.revisions
            active = next((revision for revision in revisions if revision.version == active_version), None)
            if active is None:
                continue
            conn.execute(
                """
                INSERT INTO sources (id, source_system, source_key, external_id, space_key)
                VALUES (%s, 'document', %s, %s, 'SYNTHETIC')
                ON CONFLICT (id) DO NOTHING
                """,
                (source.document_id, FIXTURE_SOURCE_KEY, source.external_id),
            )
            for revision in revisions:
                _install_revision(conn, source, revision)
            _activate_fixture_revision(conn, source, active, hackathon=hackathon)


def _local_public_revisions(conn, source: FixtureSource) -> tuple[FixtureRevision, ...]:
    from backend.documents import PageSnapshot
    from backend.documents.catalog import is_local_public_page

    expected = {
        "id": source.document_id, "source_system": "document",
        "source_key": FIXTURE_SOURCE_KEY, "external_id": source.external_id,
    }
    catalog_rows = conn.execute(
        """
        SELECT id, source_system, source_key, external_id FROM sources
        WHERE id = %(id)s OR (source_system = %(source_system)s
            AND source_key = %(source_key)s AND external_id = %(external_id)s)
        """,
        expected,
    )
    if any(row != expected for row in catalog_rows):
        return ()
    return tuple(
        revision for revision in source.revisions
        if is_local_public_page(
            conn, FIXTURE_SOURCE_KEY, source.external_id,
            PageSnapshot(source.external_id, revision.version, source.title, revision.text),
        )
    )


def _activate_fixture_revision(conn, source: FixtureSource, active: FixtureRevision, *, hackathon: bool) -> None:
    current = conn.execute(
        "SELECT status, current_revision_id, acl_actor_ids FROM sources WHERE id = %s",
        (source.document_id,),
    ).fetchone()
    if current["status"] == "active" and current["current_revision_id"] == active.revision_id:
        return
    conn.execute(
        """
        UPDATE sources SET status = 'active', current_revision_id = %s, updated_at = now()
        WHERE id = %s
        """,
        (active.revision_id, source.document_id),
    )
    if current["acl_actor_ids"] is None and conn.execute(
        "SELECT 1 FROM faq_entries WHERE status = 'active' LIMIT 1"
    ).fetchone():
        from backend.mailfaq import reconcile_document_revision

        previous = current["current_revision_id"] if current["status"] == "active" else None
        reconcile_document_revision(conn, active.revision_id, previous, hackathon=hackathon)


def _install_revision(conn, source: FixtureSource, revision: FixtureRevision) -> None:
    checksum = hashlib.sha256(revision.text.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO source_revisions
            (id, source_id, version, title, storage_body, checksum, space_key, indexed_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'SYNTHETIC', now())
        ON CONFLICT (id) DO NOTHING
        """,
        (revision.revision_id, source.document_id, revision.version,
         source.title, revision.text, checksum),
    )
    conn.execute(
        """
        INSERT INTO source_fragments (id, revision_id, ordinal, anchor, heading_path, text)
        VALUES (%s, %s, 0, %s, %s, %s) ON CONFLICT (id) DO NOTHING
        """,
        (revision.fragment_id, revision.revision_id, source.anchor, [source.heading], revision.text),
    )
