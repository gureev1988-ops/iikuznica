"""Recover a versioned action and resume events over an actual loopback HTTP connection."""

from uuid import uuid4

import httpx
from backend.app import Backend
from backend.contracts.engine import build_engine
from backend.db import connect
from .conftest import credentials


def first_event(http, path, cursor):
    with http.stream("GET", path, headers={**credentials("first"), "Last-Event-ID": str(cursor)}) as result:
        assert result.status_code == 200
        assert result.headers["content-type"].startswith("text/event-stream")
        for line in result.iter_lines():
            if line.startswith("id: "):
                return int(line[4:])
    raise AssertionError("No event received")


def test_sse_disconnect_switch_engine_and_recover(live_http, deployment, dsn):
    settings, _, _ = deployment
    chat = uuid4()
    base = f"/v1/conversations/{chat}"
    with httpx.Client(base_url=live_http, trust_env=False, timeout=5) as http:
        response = http.post(base + "/messages", headers=credentials("first"),
                             json={"text": "Как подать заявку?", "client_message_id": "need-clarification"})
        response.raise_for_status()
        first = first_event(http, base + "/events", 0)
        backend = Backend(settings)
        try:
            backend.run_job("interactive")
            next_cursor = first_event(http, base + "/events", first)
            snapshot = http.get(base + "/messages", headers=credentials("first")).json()
            clarification = snapshot["turns"][0]["clarifications"][0]
            assert next_cursor <= snapshot["cursor"]
            assert clarification["status"] == "open"
            backend.dialog.engine = build_engine("fixture-beta")
            followup = http.post(base + "/messages", headers=credentials("first"), json={
                "client_message_id": "chosen", "case_id": response.json()["case_id"],
                "clarification_id": clarification["id"], "clarification_version": clarification["version"],
                "option_id": clarification["options"][0]["id"],
            })
            assert followup.status_code == 202, followup.text
            backend.run_job("interactive")
            assert first_event(http, base + "/events", snapshot["cursor"]) > snapshot["cursor"]
            with connect(dsn) as conn:
                conn.execute("UPDATE conversations SET events_floor=event_cursor WHERE id=%s", (chat,))
            stale = http.get(base + "/events", headers={**credentials("first"), "Last-Event-ID": str(first)})
            assert stale.status_code == 410
            recovered = http.get(base + "/messages", headers=credentials("first")).json()
            assert recovered["cursor"] > snapshot["cursor"]
            assert http.get(base + "/events", headers=credentials("second")).status_code == 404
        finally:
            backend.close()


def test_web_fixture_resumes_at_persisted_offer():
    import json
    from pathlib import Path
    from backend.contracts.api import AcceptedMessage, EventView, HistoryView, MessageRequest, OfferDecision

    sample = json.loads((Path(__file__).parent / "fixtures/web-v1.json").read_text())
    MessageRequest.model_validate(sample["request"]["body"])
    accepted = AcceptedMessage.model_validate(sample["accepted"])
    snapshot = HistoryView.model_validate(sample["snapshot"])
    decision = OfferDecision.model_validate(sample["confirm"]["body"])
    offer = snapshot.turns[0].offers[0]
    assert accepted.conversation_id == snapshot.conversation_id and accepted.turn_id == offer.turn_id
    assert offer.status == "open" and decision.version == offer.version
    cursor = int(sample["resume"]["last_event_id"])
    assert cursor == snapshot.cursor
    for frame in sample["resume"]["frames"]:
        data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
        event = EventView.model_validate_json(data)
        assert event.conversation_id == accepted.conversation_id and event.cursor == cursor + 1
        assert event.kind == "offer.confirmed" and event.payload["offer"]["id"] == str(offer.id)
        cursor = event.cursor
    assert cursor > snapshot.cursor
