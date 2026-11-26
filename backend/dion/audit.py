"""Case history for the rendered envelopes and outcomes of DION physical attempts."""

from uuid import UUID

from backend.db import append_event
from backend.dialog import DialogueService


def record_dispatch(conn, outbox: dict, attempt_id: UUID, envelope: dict) -> None:
    if outbox["case_id"] is not None:
        append_event(
            conn, outbox["conversation_id"], outbox["case_id"], outbox["turn_id"],
            "dion.dispatch_prepared", {"attempt_id": str(attempt_id), **envelope},
        )


def record_outcome(
    conn, outbox: dict, attempt_id: UUID, state: str, *,
    provider_id: str | None = None, message_id: UUID | None = None,
) -> None:
    if outbox["case_id"] is None:
        return

    conversation_id = outbox["conversation_id"]
    kind = "dion.delivery_unknown" if state == "delivery_unknown" else f"dion.delivery_{state}"
    exists = conn.execute(
        """
        SELECT 1 FROM events
        WHERE conversation_id = %s AND kind = %s AND payload->>'attempt_id' = %s
        """,
        (conversation_id, kind, str(attempt_id)),
    ).fetchone()
    if exists is not None:
        return
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
        SELECT payload FROM events
        WHERE conversation_id = %s AND kind = 'dion.dispatch_prepared'
            AND payload->>'attempt_id' = %s
        """,
        (conversation_id, str(attempt_id)),
    ).fetchone()
    if prepared is not None:
        envelope = prepared["payload"]
        if envelope["substitute"]:
            DialogueService.record_delivery_replacement(
                conn, attempt_id=attempt_id, conversation_id=conversation_id,
                case_id=outbox["case_id"], turn_id=outbox["turn_id"],
                text=envelope["text"], source_refs=envelope["source_refs"],
            )
