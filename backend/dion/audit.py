"""Case history for the rendered envelopes and outcomes of DION physical attempts."""

from uuid import UUID

from psycopg.types.json import Jsonb

from backend.db import append_event
from backend.dialog import DialogueService


def record_dispatch(conn, outbox: dict, attempt_id: UUID, envelope: dict) -> None:
    conn.execute(
        """
        INSERT INTO dion_dispatches (attempt_id, effect_key, envelope)
        VALUES (%s, %s, %s)
        """,
        (attempt_id, outbox["effect_key"], Jsonb(envelope)),
    )
    if outbox["case_id"] is not None:
        append_event(
            conn, outbox["conversation_id"], outbox["case_id"], outbox["turn_id"],
            "dion.dispatch_prepared", {"attempt_id": str(attempt_id), **envelope},
        )


def record_outcome(
    conn, outbox: dict, attempt_id: UUID, state: str, *,
    provider_id: str | None = None, message_id: UUID | None = None,
) -> None:
    recorded = conn.execute(
        """
        INSERT INTO dion_attempt_outcomes (attempt_id, state, provider_id) VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING RETURNING attempt_id
        """,
        (attempt_id, state, provider_id),
    ).fetchone()
    if recorded is None:
        if provider_id is not None:
            conn.execute(
                """
                UPDATE dion_attempt_outcomes SET provider_id = %s
                WHERE attempt_id = %s AND state = %s AND provider_id IS NULL
                """,
                (provider_id, attempt_id, state),
            )
        return
    if outbox["case_id"] is None:
        return

    conversation_id = outbox["conversation_id"]
    kind = "dion.delivery_unknown" if state == "delivery_unknown" else f"dion.delivery_{state}"
    append_event(
        conn, conversation_id, outbox["case_id"], outbox["turn_id"], kind,
        {
            "attempt_id": str(attempt_id), "state": state, "provider_id": provider_id,
            "message_id": str(message_id) if message_id else None,
        },
    )
    if state != "sent":
        return

    prepared = conn.execute(
        """
        SELECT envelope FROM dion_dispatches WHERE attempt_id = %s
        """,
        (attempt_id,),
    ).fetchone()
    if prepared is not None:
        envelope = prepared["envelope"]
        if envelope["substitute"]:
            DialogueService.record_delivery_replacement(
                conn, attempt_id=attempt_id, conversation_id=conversation_id,
                case_id=outbox["case_id"], turn_id=outbox["turn_id"],
                text=envelope["text"], source_refs=envelope["source_refs"],
                condition_sources=envelope.get("condition_sources"),
            )
