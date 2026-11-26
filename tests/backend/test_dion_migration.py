"""Verify upgrade from SSE-only DION audit to per-attempt durable history."""

from shutil import copyfile
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from backend import db

MIGRATION = "014_dion_dispatch_audit.sql"


@pytest.fixture
def legacy_dsn(request, monkeypatch, tmp_path):
    legacy = tmp_path / "legacy_migrations"
    legacy.mkdir()
    for migration in db.MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"):
        if migration.name < MIGRATION:
            copyfile(migration, legacy / migration.name)
    with monkeypatch.context() as patch:
        patch.setattr(db, "MIGRATIONS", legacy)
        return request.getfixturevalue("dsn")


def _feed(conn):
    conversation_id = conn.execute(
        "INSERT INTO conversations (owner_id) VALUES ('employee') RETURNING id",
    ).fetchone()["id"]
    case_id = conn.execute(
        "INSERT INTO cases (conversation_id) VALUES (%s) RETURNING id", (conversation_id,),
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO dion_feeds (bot_id, chat_id, external_user_id, conversation_id)
        VALUES (%s, %s, %s, %s)
        """,
        (uuid4(), uuid4(), uuid4(), conversation_id),
    )
    return conversation_id, case_id


def _effect(conn, conversation_id, case_id):
    effect_key = f"turn:{uuid4()}"
    conn.execute(
        """
        INSERT INTO dion_outbox (effect_key, conversation_id, case_id, actor_id, text)
        VALUES (%s, %s, %s, 'employee', 'Original outbox text')
        """,
        (effect_key, conversation_id, case_id),
    )
    return effect_key


def _attempt(conn, effect_key, state, channel="dion"):
    attempt_id = uuid4()
    conn.execute(
        """
        INSERT INTO delivery_attempts (id, channel, effect_key, state, expires_at)
        VALUES (%s, %s, %s, %s, now())
        """,
        (attempt_id, channel, effect_key, state),
    )
    return attempt_id


def test_upgrade_keeps_attempt_audit_after_events_are_pruned(legacy_dsn):
    envelope = {
        "text": "Official answer delivered after FAQ changed",
        "source_refs": [{"revision_id": str(uuid4()), "quote": "Official answer"}],
        "substitute": True,
        "offer_id": None,
        "clarification_id": None,
        "mail_request_id": None,
        "reply_message_id": str(uuid4()),
    }
    provider_id = str(uuid4())
    with db.connect(legacy_dsn) as conn:
        assert conn.execute("SELECT to_regclass('dion_dispatches') AS name").fetchone()["name"] is None
        conversation_id, case_id = _feed(conn)
        unrelated_conversation, unrelated_case = _feed(conn)
        effect_key = _effect(conn, conversation_id, case_id)
        legacy_effect = _effect(conn, conversation_id, case_id)
        sent_attempt = _attempt(conn, effect_key, "sent")
        preflight_failure = _attempt(conn, effect_key, "failed")
        legacy_attempt = _attempt(conn, legacy_effect, "sent")
        other_channel = _attempt(conn, effect_key, "sent", channel="smtp")

        def publish(kind, payload):
            return db.append_event(conn, conversation_id, case_id, None, kind, payload)

        kind = "dion.dispatch_prepared"
        dispatch_payload = {"attempt_id": str(sent_attempt), **envelope}
        db.append_event(conn, unrelated_conversation, unrelated_case, None, kind, dispatch_payload)
        publish(kind, {"attempt_id": str(sent_attempt), "text": "Incomplete event"})
        cursor = publish(kind, dispatch_payload)
        rendered_at = conn.execute(
            "SELECT created_at FROM events WHERE conversation_id = %s AND cursor = %s",
            (conversation_id, cursor),
        ).fetchone()["created_at"]
        publish(kind, {**dispatch_payload, "text": "Later duplicate must not replace the rendering"})
        publish(kind, {"attempt_id": str(other_channel), **envelope})
        publish("dion.delivery_sent", {"state": "sent"})  # Legacy attempt ID is unknown.
        publish("dion.delivery_failed", {"attempt_id": str(preflight_failure), "state": "failed"})
        pending = {"attempt_id": str(sent_attempt), "state": "delivery_unknown"}
        publish("dion.delivery_unknown", pending)
        publish("dion.delivery_unknown", pending)
        publish("dion.delivery_sent", {
            "attempt_id": str(sent_attempt), "state": "sent", "provider_id": provider_id,
        })

    pending = tuple(sorted(
        path.name for path in db.MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql") if path.name >= MIGRATION
    ))
    assert db.migrate(legacy_dsn) == pending
    assert db.migrate(legacy_dsn) == ()
    with db.connect(legacy_dsn) as conn:
        assert conn.execute("SELECT * FROM dion_dispatches").fetchall() == [{
            "attempt_id": sent_attempt, "effect_key": effect_key,
            "envelope": envelope, "rendered_at": rendered_at,
        }]
        outcomes = conn.execute(
            "SELECT attempt_id, state, provider_id FROM dion_attempt_outcomes",
        ).fetchall()
        assert {(r["attempt_id"], r["state"], r["provider_id"]) for r in outcomes} == {
            (preflight_failure, "failed", None),
            (sent_attempt, "delivery_unknown", None),
            (sent_attempt, "sent", provider_id),
        }

        with pytest.raises(ForeignKeyViolation), conn.transaction():
            conn.execute(
                "INSERT INTO dion_dispatches (attempt_id, effect_key, envelope) VALUES (%s, %s, %s)",
                (legacy_attempt, effect_key, Jsonb(envelope)),
            )
        conn.execute("UPDATE conversations SET events_floor = event_cursor WHERE id = %s", (conversation_id,))
        conn.execute("DELETE FROM events WHERE conversation_id = %s", (conversation_id,))
        replay = conn.execute(
            """
            INSERT INTO dion_attempt_outcomes (attempt_id, state) VALUES (%s, 'sent')
            ON CONFLICT DO NOTHING RETURNING attempt_id
            """,
            (sent_attempt,),
        ).fetchone()
        assert replay is None
        assert conn.execute(
            "SELECT envelope FROM dion_dispatches WHERE attempt_id = %s", (sent_attempt,),
        ).fetchone()["envelope"] == envelope
