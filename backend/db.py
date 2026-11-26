"""PostgreSQL connections, transactional migrations and shared event persistence."""

import hashlib
import os
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATIONS = Path(__file__).with_name("migrations")


def connect(dsn: str | None = None):
    if dsn is None:
        dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ValueError("DATABASE_URL is required")
    return psycopg.connect(dsn, row_factory=dict_row)


def migrate(dsn: str | None = None) -> tuple[str, ...]:
    """Apply new migrations in one transaction, checking already installed SQL."""
    applied = []
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), 317110)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name text PRIMARY KEY,
                sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        known = {
            row["name"]: row["sha256"]
            for row in conn.execute("SELECT name, sha256 FROM schema_migrations")
        }
        files = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
        missing = known.keys() - {path.name for path in files}
        if missing:
            raise ValueError(f"Applied migrations are missing locally: {', '.join(sorted(missing))}")
        for path in files:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            previous = known.get(path.name)
            if previous is not None:
                if previous != digest:
                    raise ValueError(f"Applied migration changed: {path.name}")
                continue
            conn.execute(content.decode("utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)",
                (path.name, digest),
            )
            applied.append(path.name)
    return tuple(applied)


def append_event(
    conn,
    conversation_id: UUID | str,
    case_id: UUID | str,
    turn_id: UUID | str | None,
    kind: str,
    payload: dict,
) -> int:
    """Allocate a feed cursor under the conversation row lock and append the event.

    Use the operation's transaction to commit the event and associated state together.
    """
    row = conn.execute(
        """
        WITH next_cursor AS (
            UPDATE conversations SET event_cursor = event_cursor + 1
            WHERE id = %s RETURNING id, event_cursor
        )
        INSERT INTO events (conversation_id, cursor, case_id, turn_id, kind, payload)
        SELECT id, event_cursor, %s, %s, %s, %s FROM next_cursor
        RETURNING cursor
        """,
        (conversation_id, case_id, turn_id, kind, Jsonb(payload)),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown conversation: {conversation_id}")
    return row["cursor"]


def lock_source_catalog(conn, *, write: bool = False) -> None:
    """Serialize source visibility changes with evidence reads in the transaction."""
    if write:
        conn.execute("SELECT pg_advisory_xact_lock(31007)")
    else:
        conn.execute("SELECT pg_advisory_xact_lock_shared(31007)")
