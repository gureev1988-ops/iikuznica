from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
import json
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from backend.contracts.api import EventCursorExpired, EventView
from backend.contracts.engine import (
    AnswerRequest,
    Citation,
    EngineUnavailable,
    HistoryMessage,
    SourceFragment,
    SourceSnapshot,
    build_engine,
)
from backend.contracts.export import DEFAULT_OUTPUT, openapi_document
from backend.contracts.fixtures import load_fixture, seed_fixture_sources
from backend.db import append_event, connect, migrate


def fixture_request(scenario_index=0, *, version=1):
    fixture = load_fixture()
    scenario = fixture.scenarios[scenario_index]
    fragments = []
    for source in fixture.sources:
        revision = next(revision for revision in source.revisions if revision.version == version)
        fragments.append(SourceFragment(
            source.document_id, revision.revision_id, revision.fragment_id,
            revision.text, source.title, source.anchor,
        ))
    return AnswerRequest(
        question=scenario.questions[0],
        turn_id=uuid4(),
        case_id=uuid4(),
        conditions={},
        history=(),
        deadline=datetime.now(UTC) + timedelta(seconds=20),
        sources=SourceSnapshot(fragments),
    )


@pytest.mark.parametrize("name", ["fixture-alpha", "fixture-beta"])
def test_engines_follow_the_same_port_and_use_permitted_revisions(name):
    engine = build_engine(name)
    assert engine.version == 1
    for version in (1, 2):
        request = fixture_request(version=version)
        fragment = request.sources.fragments[0]
        result = engine.answer(request)
        assert result.action == "answer"
        assert result.text == fragment.text
        assert result.citations == (
            Citation(fragment.document_id, fragment.revision_id, fragment.fragment_id,
                     fragment.text, fragment.text),
        )

    restricted = replace(request, sources=SourceSnapshot())
    assert engine.answer(restricted).action == "escalate"
    unrelated = replace(request, question="Unrelated subject", history=(
        HistoryMessage("user", request.question),
    ))
    assert engine.answer(unrelated).action == "out_of_scope"
    assert engine.answer(fixture_request(2)).action == "escalate"
    with pytest.raises(EngineUnavailable, match="Simulated connection failure"):
        engine.answer(fixture_request(3))
    with pytest.raises(EngineUnavailable, match="deadline"):
        engine.answer(replace(request, deadline=datetime.now(UTC)))


@pytest.mark.parametrize("name", ["fixture-alpha", "fixture-beta"])
def test_fixture_engines_do_not_report_physical_attempts(name):
    class UnexpectedCall:
        def call(self, *args, **kwargs):
            pytest.fail("A fixture engine must not call a physical model provider")

    request = replace(fixture_request(), attempts=UnexpectedCall())
    assert build_engine(name).answer(request).action == "answer"


@pytest.mark.parametrize(
    ("first", "second"), [("fixture-alpha", "fixture-beta"), ("fixture-beta", "fixture-alpha")]
)
def test_engines_can_switch_after_clarification(first, second):
    request = fixture_request(1)
    clarification = build_engine(first).answer(request)
    assert clarification.action == "clarify"
    assert clarification.clarification_key == "requester"
    option = clarification.options[0]
    assert build_engine(second).answer(replace(request, conditions=option.conditions)).action == "answer"
    assert build_engine(second).answer(replace(request, conditions={"requester": "free text"})).action == "answer"
    assert build_engine(first).route != build_engine(second).route


def test_engine_receives_immutable_input():
    request = fixture_request()
    source_list = list(request.sources.fragments)
    conditions = {"requester": "employee"}
    history = [HistoryMessage("user", "Question")]
    request = replace(request, sources=SourceSnapshot(source_list), conditions=conditions, history=history)
    source_list.clear()
    conditions.clear()
    history.clear()
    assert request.sources.fragments
    assert request.conditions == {"requester": "employee"}
    assert request.history == (HistoryMessage("user", "Question"),)
    with pytest.raises(TypeError):
        request.conditions["requester"] = "changed"
    with pytest.raises(FrozenInstanceError):
        request.sources.fragments[0].text = "changed"


def test_sse_resumption_uses_cursor_and_round_trips_event():
    event = EventView(
        conversation_id=uuid4(), case_id=uuid4(), turn_id=None, cursor=29,
        kind="mail.answered", payload={"text": "Ответ\nс новой строкой"}, created_at=datetime.now(UTC),
    )
    event_id, event_name, body = event.encode_sse().strip().splitlines()
    assert event_id == "id: 29"
    assert event_name == "event: mail.answered"
    assert EventView.model_validate_json(body.removeprefix("data: ")) == event
    assert event.encode_sse().endswith("\n\n")


def _feed(conn):
    conversation_id = conn.execute(
        "INSERT INTO conversations(owner_id) VALUES ('employee') RETURNING id"
    ).fetchone()["id"]
    case_id = conn.execute(
        "INSERT INTO cases(conversation_id) VALUES (%s) RETURNING id", (conversation_id,)
    ).fetchone()["id"]
    return conversation_id, case_id


def test_clean_migrations_and_immutable_source_revisions(dsn, db):
    assert migrate(dsn) == ()
    expected_tables = {
        "conversations", "cases", "turns", "messages", "offers", "clarifications", "events",
        "sources", "source_revisions", "source_fragments", "document_sync_runs", "document_sync_seen",
        "jobs", "model_calls", "delivery_attempts", "mail_requests", "mail_outbound", "mail_inbound",
        "mail_cursors", "faq_entries", "dion_cursors", "dion_feeds", "dion_updates", "dion_outbox",
        "integration_status",
    }
    rows = db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    )
    assert expected_tables <= {row["tablename"] for row in rows}
    seed_fixture_sources(db)
    seed_fixture_sources(db, active_version=2)
    expected = load_fixture().sources[0]
    rows = db.execute(
        """
        SELECT r.id, r.version, f.text, s.current_revision_id
        FROM sources s JOIN source_revisions r ON r.source_id = s.id
        JOIN source_fragments f ON f.revision_id = r.id
        WHERE s.id = %s ORDER BY r.version
        """,
        (expected.document_id,),
    ).fetchall()
    assert [(row["version"], row["text"]) for row in rows] == [
        (revision.version, revision.text) for revision in expected.revisions
    ]
    assert all(row["current_revision_id"] == expected.revisions[-1].revision_id for row in rows)


def test_event_cursors_are_monotonic_and_transactional(dsn, db):
    feed_id, case_id = _feed(db)
    db.commit()

    def publish(value):
        with connect(dsn) as conn:
            return append_event(conn, feed_id, case_id, None, "test", {"value": value})

    with ThreadPoolExecutor(max_workers=6) as pool:
        cursors = list(pool.map(publish, range(12)))
    assert sorted(cursors) == list(range(1, 13))
    assert append_event(db, feed_id, case_id, None, "reverted", {}) == 13
    db.rollback()
    assert publish(13) == 13
    rows = db.execute(
        "SELECT cursor, kind FROM events WHERE conversation_id = %s ORDER BY cursor", (feed_id,)
    ).fetchall()
    assert rows == [{"cursor": cursor, "kind": "test"} for cursor in range(1, 14)]


def test_message_idempotency_and_event_conversation_constraints(db):
    feed_a, case_a = _feed(db)
    feed_b, case_b = _feed(db)
    insert = """
        INSERT INTO turns(conversation_id, case_id, request_id, question)
        VALUES (%s, %s, 'client-turn', 'Question') RETURNING id
    """
    turn_a = db.execute(insert, (feed_a, case_a)).fetchone()["id"]
    db.execute(insert, (feed_b, case_b))
    with pytest.raises(UniqueViolation), db.transaction():
        db.execute(insert, (feed_a, case_a))
    with pytest.raises(ForeignKeyViolation), db.transaction():
        append_event(db, feed_b, case_b, turn_a, "invalid", {})
    assert append_event(db, feed_b, case_b, None, "allowed", {}) == 1



def test_published_openapi_describes_admission_and_sse_recovery():
    contract = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    paths = contract["paths"]
    messages = paths["/v1/conversations/{conversation_id}/messages"]
    request_body = messages["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert request_body["$ref"] == "#/components/schemas/MessageRequest"
    accepted = messages["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    assert accepted["$ref"] == "#/components/schemas/AcceptedMessage"

    events = paths["/v1/conversations/{conversation_id}/events"]["get"]
    resume = {(item["in"], item["name"]) for item in events["parameters"]}
    assert {("header", "Last-Event-ID"), ("query", "after")} <= resume
    sse = events["responses"]["200"]["content"]["text/event-stream"]
    assert sse["x-event-schema"] == EventView.model_json_schema()
    assert sse["x-reset-schema"] == EventCursorExpired.model_json_schema()
    assert "410" in events["responses"]


def test_published_openapi_matches_composed_api():
    if find_spec("backend.app") is None:
        pytest.skip("Run in the integrated backend checkout to compare the registered routers")
    assert json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8")) == openapi_document()


def test_fixture_revision_change_reactivates_covered_faq(db):
    if find_spec("backend.mailfaq") is None:
        pytest.skip("Run in the integrated checkout to verify FAQ reconciliation")
    from backend.mailfaq import MailConfig, create_request, find_faq
    from backend.mailfaq.faq import publish_faq

    fixture = load_fixture()
    source = fixture.sources[0]
    first, second = source.revisions
    question = fixture.scenarios[0].questions[0]
    seed_fixture_sources(db)
    conversation_id, case_id = _feed(db)
    turn_id = db.execute(
        """
        INSERT INTO turns (conversation_id, case_id, request_id, question)
        VALUES (%s, %s, 'fixture-coverage', %s) RETURNING id
        """,
        (conversation_id, case_id, question),
    ).fetchone()["id"]
    offer_id = db.execute(
        """
        INSERT INTO offers (case_id, turn_id, status, text)
        VALUES (%s, %s, 'confirmed', 'Official clarification') RETURNING id
        """,
        (case_id, turn_id),
    ).fetchone()["id"]
    request_id = create_request(
        db, MailConfig(), offer_id=offer_id, conversation_id=conversation_id, case_id=case_id,
        turn_id=turn_id, actor_id="employee", actor_email="employee@example.test", channel="web",
        question=question, conditions={},
    )
    request = db.execute("SELECT * FROM mail_requests WHERE id = %s", (request_id,)).fetchone()
    official = publish_faq(db, request, first.text)
    assert find_faq(db, question, {}) is None  # The equivalent first revision covers this FAQ.

    seed_fixture_sources(db, active_version=2)
    visible = find_faq(db, question, {})
    assert visible.revision_id == official.revision_id
    assert visible.conflict_revision_id == second.revision_id
    assert visible.text == first.text
