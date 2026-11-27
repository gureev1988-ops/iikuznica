from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from backend.contracts.api import MessageRequest
from backend.contracts.engine import (
    AnswerResult, Citation, ClarificationOption, ModelAttemptFailure, ModelAttemptResult, build_engine,
)
from backend.contracts.fixtures import load_fixture, seed_fixture_sources
from backend.db import append_event, connect, lock_source_catalog
from backend.dialog import Actor, DialogueService, create_router
from backend.dion import queue_reply
from backend.mailfaq import MailConfig, send_followup
from backend.mailfaq.faq import publish_faq
from backend.runtime import run_once


EMPLOYEE = Actor("employee", frozenset({"employee"}), "employee@example.test")
FIXTURE = load_fixture()


def client_for(service):
    def identity(x_actor: str = Header(default="employee")):
        if x_actor == "operator":
            return Actor(x_actor, frozenset({"ops"}))
        if x_actor != EMPLOYEE.id:
            return Actor(x_actor, EMPLOYEE.roles, f"{x_actor}@example.test")
        return EMPLOYEE

    app = FastAPI()
    app.include_router(create_router(service, identity))
    return TestClient(app)


def enqueue(client, conversation_id, scenario, *, message_id="client-1", **body):
    result = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_message_id": message_id, "text": FIXTURE.scenarios[scenario].questions[0], **body},
    )
    assert result.status_code == 202, result.text
    return result.json()


def process(service):
    return run_once(
        service.dsn, "interactive", {"answer_turn": service.answer_job}, base_retry_seconds=0,
        terminal_handlers={"answer_turn": service.terminal_outcome},
    )


def snapshot(client, turn_id):
    response = client.get(f"/v1/turns/{turn_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_admission_atomic_idempotent_and_owner_scoped(dsn):
    service = DialogueService(dsn=dsn, engine=build_engine("fixture-alpha"), max_pending=1)
    client = client_for(service)
    first, rejected = uuid4(), uuid4()
    admitted = enqueue(client, first, 0)
    assert enqueue(client, first, 0) == admitted
    same_id = {"client_message_id": "client-1", "text": "changed"}
    path = f"/v1/conversations/{first}/messages"
    assert client.post(path, json=same_id).status_code == 409
    assert client.post(path, json=same_id, headers={"X-Actor": "other"}).status_code == 404
    assert client.get(path, headers={"X-Actor": "other"}).status_code == 404
    assert client.get(path, headers={"X-Actor": "operator"}).status_code == 403
    rejected_path = f"/v1/conversations/{rejected}/messages"
    assert client.post(rejected_path, json=same_id).status_code == 503
    assert client.get(rejected_path).status_code == 404
    with connect(dsn) as conn:
        for table in ("conversations", "cases", "turns", "messages", "jobs", "events"):
            assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 1


@pytest.mark.parametrize("free_text", [False, True])
def test_resumed_clarification_after_engine_switch(dsn, free_text):
    with connect(dsn) as conn:
        seed_fixture_sources(conn)
    alpha = DialogueService(dsn=dsn, engine=build_engine("fixture-alpha"))
    client = client_for(alpha)
    conversation_id = uuid4()
    first = enqueue(client, conversation_id, 1)
    process(alpha)
    pending = snapshot(client, first["turn_id"])
    clarification = pending["clarifications"][0]
    assert pending["status"] == "completed" and clarification["status"] == "open"

    beta = DialogueService(dsn=dsn, engine=build_engine("fixture-beta"))
    resumed = client_for(beta)
    path = f"/v1/conversations/{conversation_id}/messages"
    assert resumed.get(path).json()["cursor"] == pending["cursor"]
    followup = {"text": "Контрагенту"} if free_text else {"text": None, "option_id": "employee"}
    second = enqueue(
        resumed, conversation_id, 1, message_id="client-2", case_id=first["case_id"],
        clarification_id=clarification["id"], clarification_version=clarification["version"], **followup,
    )
    process(beta)
    view = snapshot(resumed, second["turn_id"])
    assert view["case_id"] == first["case_id"]
    assert view["result"]["action"] == "answer"
    assert snapshot(resumed, first["turn_id"])["clarifications"][0]["status"] == "resolved"
    with connect(dsn) as conn:
        received = beta.events(conn, EMPLOYEE, conversation_id, pending["cursor"])
        assert [row["kind"] for row in received] == [
            "clarification.resolved", "turn.accepted", "turn.processing", "turn.completed",
        ]
        assert received[-1]["cursor"] == view["cursor"]
        assert conn.execute("SELECT count(*) AS n FROM model_calls").fetchone()["n"] == 0
        conn.execute("UPDATE conversations SET events_floor = %s WHERE id = %s", (view["cursor"], conversation_id))
    response = resumed.get(
        f"/v1/conversations/{conversation_id}/events?after={view['cursor']}",
        headers={"Last-Event-ID": str(pending["cursor"])},
    )
    assert response.status_code == 410
    assert response.json()["detail"] == {"code": "event_cursor_expired", "cursor": view["cursor"]}


def test_decline_then_consent_is_closed_and_concurrent_confirm_queues_once(dsn):
    service = DialogueService(dsn=dsn, engine=build_engine("fixture-alpha"), mail_config=MailConfig())
    client = client_for(service)
    first = enqueue(client, uuid4(), 2)
    process(service)
    declined = snapshot(client, first["turn_id"])["offers"][0]
    decline_path = f"/v1/offers/{declined['id']}"
    assert client.post(f"{decline_path}/decline", json={"version": 1}).status_code == 200
    assert client.post(f"{decline_path}/confirm", json={"version": 1}).status_code == 409

    second = enqueue(client, uuid4(), 2)
    process(service)
    restarted = DialogueService(dsn=dsn, engine=build_engine("fixture-beta"), mail_config=MailConfig())
    resumed = client_for(restarted)
    offer = snapshot(resumed, second["turn_id"])["offers"][0]
    path = f"/v1/offers/{offer['id']}/confirm"
    assert resumed.post(path, json={"version": 2}).status_code == 409
    assert resumed.post(path, json={"version": 1}, headers={"X-Actor": "other"}).status_code == 404
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: resumed.post(path, json={"version": 1}), range(2)))
    assert [response.status_code for response in results] == [200, 200]
    assert all(response.json()["status"] == "confirmed" for response in results)
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM mail_requests").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM mail_outbound").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM events WHERE kind = 'offer.confirmed'").fetchone()["n"] == 1


def test_technical_failure_retry_reuses_the_turn(dsn):
    engine = build_engine("fixture-alpha")
    service = DialogueService(dsn=dsn, engine=engine)
    client = client_for(service)
    admitted = enqueue(client, uuid4(), 3)
    for _ in range(3):
        process(service)
    turn_id = admitted["turn_id"]
    failed = snapshot(client, turn_id)
    assert (failed["status"], failed["error"]) == ("technical_failure", "EngineUnavailable")
    path = f"/v1/turns/{turn_id}/retry?attempt=1"
    assert client.post(path).json() == admitted
    assert client.post(path).json() == admitted

    class RecoveredEngine:
        model = "recovered"
        route = "synthetic/recovered"

        def answer(self, request):
            assert str(request.case_id) == admitted["case_id"]
            return AnswerResult("out_of_scope", "Synthetic recovered result")

    service.engine = RecoveredEngine()
    process(service)
    assert client.post(path).json() == admitted
    complete = snapshot(client, turn_id)
    assert (complete["status"], complete["attempt"], complete["error"]) == ("completed", 2, None)
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM turns").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM model_calls").fetchone()["n"] == 0


def test_model_recorder_records_only_physical_retry_and_judge_attempts(dsn):
    class NetworkLikeEngine:
        def answer(self, request):
            def fail():
                raise ModelAttemptFailure("busy", input_tokens=11, route="response-route-1")

            with pytest.raises(ModelAttemptFailure):
                request.attempts.call(fail, role="answer", model="candidate", route="requested-1")
            draft = request.attempts.call(
                lambda: ModelAttemptResult("Synthetic response", 21, 4, Decimal("0.005"), "response-route-2"),
                role="answer", model="candidate", route="requested-2", trace_id="provider-trace-2",
            )
            verdict = request.attempts.call(
                lambda: ModelAttemptResult(True, 9, 1, 0, model="actual-judge"),
                role="judge", model="judge", route="judge-route",
            )
            assert verdict
            return AnswerResult("out_of_scope", draft)

    service = DialogueService(dsn=dsn, engine=NetworkLikeEngine())
    client = client_for(service)
    accepted = enqueue(client, uuid4(), 0)
    process(service)
    assert snapshot(client, accepted["turn_id"])["result"]["text"] == "Synthetic response"
    with connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT role, state, trace_id, model, route, input_tokens, output_tokens, cost
            FROM model_calls WHERE turn_id = %s AND case_id = %s ORDER BY started_at
            """,
            (accepted["turn_id"], accepted["case_id"]),
        ).fetchall()
    assert [row["role"] for row in rows] == ["answer", "answer", "judge"]
    assert [row["state"] for row in rows] == ["failed", "succeeded", "succeeded"]
    assert [row["route"] for row in rows] == ["response-route-1", "response-route-2", "judge-route"]
    assert [row["model"] for row in rows] == ["candidate", "candidate", "actual-judge"]
    assert [row["input_tokens"] for row in rows] == [11, 21, 9]
    assert [row["output_tokens"] for row in rows] == [None, 4, 1]
    assert [row["cost"] for row in rows] == [None, Decimal("0.005"), 0]
    assert len({row["trace_id"] for row in rows}) == 3 and rows[1]["trace_id"] == "provider-trace-2"


def test_history_preserves_old_revisions_then_hides_revoked_sources_and_events(dsn):
    with connect(dsn) as conn:
        seed_fixture_sources(conn)
    service = DialogueService(dsn=dsn, engine=build_engine("fixture-alpha"))
    client = client_for(service)
    conversation_id = uuid4()
    admitted = enqueue(client, conversation_id, 0)
    process(service)
    original = snapshot(client, admitted["turn_id"])
    source = original["result"]["sources"][0]
    with connect(dsn) as conn:
        seed_fixture_sources(conn, active_version=2)
    assert client.get(source["url"]).json()["text"] == source["quote"]
    assert snapshot(client, admitted["turn_id"])["result"] == original["result"]
    with connect(dsn) as conn:
        conn.execute(
            """
            INSERT INTO messages (conversation_id, case_id, turn_id, role, text, source_refs)
            VALUES (%s, %s, %s, 'assistant', 'Late source-backed message', %s)
            """,
            (conversation_id, admitted["case_id"], admitted["turn_id"], Jsonb([source])),
        )
        tail = append_event(
            conn, conversation_id, admitted["case_id"], admitted["turn_id"],
            "mail.resolution", {"text": "Late source-backed message", "source_refs": [source]},
        )
        conn.execute("UPDATE conversations SET events_floor = %s WHERE id = %s", (tail - 1, conversation_id))
        conn.execute("DELETE FROM events WHERE conversation_id = %s AND cursor < %s", (conversation_id, tail))
    history_path = f"/v1/conversations/{conversation_id}/messages"
    history = client.get(history_path).json()
    assert history["messages"][-1]["source_refs"] == [source]
    assert history["cursor"] == tail
    with connect(dsn) as conn:
        lock_source_catalog(conn, write=True)
        conn.execute("UPDATE sources SET acl_actor_ids = ARRAY['other'] WHERE id = %s", (source["document_id"],))
    assert client.get(source["url"]).status_code == 404
    history = client.get(history_path).json()
    assert [message["role"] for message in history["messages"]] == ["user"]
    assert history["turns"][0]["result"] is None
    with connect(dsn) as conn:
        assert service.events(conn, EMPLOYEE, conversation_id, tail - 1)[-1]["payload"] == {"redacted": True}


def test_withdrawal_during_provider_call_prevents_stale_result(dsn):
    with connect(dsn) as conn:
        seed_fixture_sources(conn)
    prepared, revoked = Event(), Event()
    engine = build_engine("fixture-alpha")

    class PausingEngine:
        model, route = engine.model, engine.route

        def answer(self, request):
            result = engine.answer(request)
            if result.action == "answer":
                prepared.set()
                assert revoked.wait(5)
            return result

    service = DialogueService(dsn=dsn, engine=PausingEngine())
    client = client_for(service)
    conversation_id = uuid4()
    admitted = enqueue(client, conversation_id, 0)
    with ThreadPoolExecutor(max_workers=1) as threads:
        work = threads.submit(process, service)
        assert prepared.wait(5)
        with connect(dsn) as conn:
            lock_source_catalog(conn, write=True)
            conn.execute("UPDATE sources SET status = 'access_lost' WHERE id = %s", (FIXTURE.sources[0].document_id,))
        revoked.set()
        work.result(timeout=5)
    assert snapshot(client, admitted["turn_id"])["status"] == "processing"
    process(service)
    result = snapshot(client, admitted["turn_id"])["result"]
    assert result["action"] == "escalate" and result["sources"] == []
    with connect(dsn) as conn:
        feed = service.events(conn, EMPLOYEE, conversation_id, 0)
        completed = [event for event in feed if event["kind"] == "turn.completed"]
        assert len(completed) == 1 and completed[0]["payload"]["result"]["action"] == "escalate"


@pytest.mark.parametrize("revoke_before_selection", [True, False])
def test_revoked_clarification_cannot_expose_its_selected_option(dsn, revoke_before_selection):
    with connect(dsn) as conn:
        seed_fixture_sources(conn)
    label = "Source-derived option"

    class SourcedAction:
        def answer(self, request):
            fragment = next((part for part in request.sources.fragments if part.document_id == FIXTURE.sources[0].document_id), None)
            if fragment is None:
                assert not request.conditions
                assert all(label not in message.text for message in request.history)
                return AnswerResult("out_of_scope", "Source changed")
            citation = Citation(fragment.document_id, fragment.revision_id, fragment.fragment_id, fragment.text)
            return AnswerResult(
                "clarify", "Select the source-derived label", citations=(citation,),
                options=(ClarificationOption("sourced", label, {"sourced": label}),),
            )

    service = DialogueService(dsn=dsn, engine=SourcedAction())
    client = client_for(service)
    conversation_id = uuid4()
    admitted = enqueue(client, conversation_id, 0)
    process(service)
    first = snapshot(client, admitted["turn_id"])["clarifications"][0]

    def select():
        return enqueue(
            client, conversation_id, 0, message_id="select", text=None, case_id=admitted["case_id"],
            clarification_id=first["id"], clarification_version=first["version"], option_id="sourced",
        )

    following = None if revoke_before_selection else select()
    with connect(dsn) as conn:
        lock_source_catalog(conn, write=True)
        conn.execute("UPDATE sources SET acl_actor_ids = ARRAY['other'] WHERE id = %s", (FIXTURE.sources[0].document_id,))
    assert snapshot(client, admitted["turn_id"])["clarifications"] == []
    following = following or select()
    process(service)
    latest = snapshot(client, following["turn_id"])
    assert latest["status"] == "completed" and latest["result"]["text"] == "Source changed"
    assert label not in client.get(f"/v1/conversations/{conversation_id}/messages").text
    with connect(dsn) as conn:
        action = conn.execute("SELECT status FROM clarifications WHERE id = %s", (first["id"],)).fetchone()
        assert action["status"] == ("superseded" if revoke_before_selection else "resolved")


def test_mail_followups_reconstructed_after_cursor_expiry_and_close_per_case(dsn):
    service = DialogueService(dsn=dsn, engine=build_engine("fixture-alpha"), mail_config=MailConfig())
    client = client_for(service)
    conversation_id = uuid4()
    for index in range(2):
        admitted = enqueue(client, conversation_id, 2, message_id=f"case-{index}")
        process(service)
        offer = snapshot(client, admitted["turn_id"])["offers"][0]
        assert client.post(f"/v1/offers/{offer['id']}/confirm", json={"version": 1}).status_code == 200
    with connect(dsn) as conn:
        cases = conn.execute("SELECT id, case_id FROM mail_requests ORDER BY created_at").fetchall()
        for index, case in enumerate(cases, 1):
            inbound_id = uuid4()
            conn.execute(
                """
                INSERT INTO mail_inbound (id, mailbox, uidvalidity, uid, fingerprint, message_id, trusted, raw,
                    parsed, request_id, state)
                VALUES (%s, 'test', '1', %s, %s, %s, true, %s, %s, %s, 'processed')
                """,
                (inbound_id, index, str(index), f"<counter-{index}@example.test>", b"fixture",
                 Jsonb({"thread_ids": []}), case["id"]),
            )
            conn.execute(
                "UPDATE mail_requests SET state = 'counter_question', pending = %s, last_inbound_id = %s WHERE id = %s",
                (Jsonb([f"Pending {index}"]), inbound_id, case["id"]),
            )
        conn.execute("UPDATE conversations SET events_floor = event_cursor WHERE id = %s", (conversation_id,))
        conn.execute("DELETE FROM events WHERE conversation_id = %s", (conversation_id,))
    path = f"/v1/conversations/{conversation_id}/messages"
    actions = client.get(path).json()["mail_followups"]
    assert [action["request_id"] for action in actions] == [str(case["id"]) for case in cases]
    assert [action["case_id"] for action in actions] == [str(case["case_id"]) for case in cases]
    assert [action["pending"] for action in actions] == [["Pending 1"], ["Pending 2"]]
    assert snapshot(client, actions[0]["turn_id"])["mail_followups"] == actions[:1]
    with connect(dsn) as conn:
        send_followup(conn, cases[0]["id"], actor_id=EMPLOYEE.id, client_id="counter-1", text="Synthetic clarification")
    assert client.get(path).json()["mail_followups"] == actions[1:]


def publish_official_reply(dsn, scenario, text):
    class Escalates:
        def answer(self, request):
            return AnswerResult("escalate", "Request consent")

    service = DialogueService(dsn=dsn, engine=Escalates(), mail_config=MailConfig())
    client = client_for(service)
    official = enqueue(client, uuid4(), scenario)
    process(service)
    offer = snapshot(client, official["turn_id"])["offers"][0]
    assert client.post(f"/v1/offers/{offer['id']}/confirm", json={"version": 1}).status_code == 200
    with connect(dsn) as conn:
        lock_source_catalog(conn, write=True)
        request = conn.execute("SELECT * FROM mail_requests WHERE offer_id = %s", (offer["id"],)).fetchone()
        return publish_faq(conn, request, text)


def test_delayed_turn_prefers_faq_published_after_answer(dsn):
    with connect(dsn) as conn:
        seed_fixture_sources(conn, active_version=2)
    service = DialogueService(dsn=dsn, engine=build_engine("fixture-alpha"))
    client = client_for(service)
    conversation_id = uuid4()
    answered = enqueue(client, conversation_id, 0)
    process(service)
    before = snapshot(client, answered["turn_id"])["result"]
    assert "7" in before["text"]
    faq = publish_official_reply(dsn, 0, "Заявку рассматривают в течение 2 дней.")
    with connect(dsn) as conn:
        text, refs, keep_actions = service.delivery_payload(
            conn, EMPLOYEE, conversation_id, before["text"], before["sources"],
            turn_id=answered["turn_id"], reevaluate_faq=True,
        )
    assert "2" in text and refs == [faq.source_ref()] and not keep_actions
    assert snapshot(client, answered["turn_id"])["result"] == before


@pytest.mark.parametrize(("scenario", "actions", "superseded"), [
    (1, "clarifications", "clarification.superseded"),
    (2, "offers", "offer.superseded"),
])
def test_delayed_action_sends_official_replacement_and_preserves_confirmed_history(dsn, scenario, actions, superseded):
    service = DialogueService(dsn=dsn, engine=build_engine("fixture-alpha"), queue_reply=queue_reply)
    client = client_for(service)
    actor = Actor(EMPLOYEE.id, EMPLOYEE.roles, EMPLOYEE.email, channel="dion")
    conversation_id = uuid4()
    with connect(dsn) as conn:
        service.ensure_conversation(conn, actor, conversation_id)
        conn.execute(
            "INSERT INTO dion_feeds (bot_id, chat_id, external_user_id, conversation_id) VALUES (%s, %s, %s, %s)",
            (uuid4(), uuid4(), uuid4(), conversation_id),
        )
        admitted = service.admit(conn, actor, conversation_id, MessageRequest(
            client_message_id="dion-action", text=FIXTURE.scenarios[scenario].questions[0],
        ))
    process(service)
    before = snapshot(client, admitted.turn_id)
    assert before[actions][0]["status"] == "open"
    faq = publish_official_reply(dsn, scenario, "Теперь доступно официальное разъяснение.")
    attempt_id = uuid4()
    with connect(dsn) as conn:
        outbox = conn.execute("SELECT * FROM dion_outbox WHERE turn_id = %s", (admitted.turn_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO delivery_attempts (id, channel, effect_key, state, expires_at)
            VALUES (%s, 'dion', %s, 'sending', now() + interval '1 minute')
            """,
            (attempt_id, outbox["effect_key"]),
        )
        text, refs, keep_actions = service.delivery_payload(
            conn, actor, conversation_id, outbox["text"], outbox["source_refs"],
            turn_id=admitted.turn_id, reevaluate_faq=True,
        )
        assert text == faq.text and refs == [faq.source_ref()] and not keep_actions
        assert service.open_actions(conn, actor, conversation_id)[actions] == []
        assert not service.delivery_payload(
            conn, actor, conversation_id, outbox["text"], outbox["source_refs"],
            turn_id=admitted.turn_id, reevaluate_faq=True,
        )[2]
        actual_text = f"{text}\n\n{refs[0]['url']}"
        audit = dict(attempt_id=attempt_id, conversation_id=conversation_id, case_id=admitted.case_id,
                     turn_id=admitted.turn_id, text=actual_text, source_refs=refs, condition_sources=[])
        conn.execute(
            "INSERT INTO dion_dispatches (attempt_id, effect_key, envelope) VALUES (%s, %s, %s)",
            (
                attempt_id, outbox["effect_key"], Jsonb({
                    "text": actual_text, "source_refs": refs, "condition_sources": [],
                    "substitute": True, "offer_id": None, "clarification_id": None,
                    "mail_request_id": None, "reply_message_id": None,
                }),
            ),
        )
        service.record_delivery_replacement(conn, **audit)
    history_path = f"/v1/conversations/{conversation_id}/messages"
    assert all(item["id"] != str(attempt_id) for item in client.get(history_path).json()["messages"])
    with connect(dsn) as conn:
        conn.execute("UPDATE delivery_attempts SET state = 'sent' WHERE id = %s", (attempt_id,))
        service.record_delivery_replacement(conn, **audit)
        service.record_delivery_replacement(conn, **audit)
        events = service.events(conn, actor, conversation_id, before["cursor"])
        assert [event["kind"] for event in events] == [superseded, "dion.reply_substituted"]
        assert events[-1]["payload"]["source_refs"] == refs
        assert events[-1]["payload"]["text"] == actual_text
        conn.execute("UPDATE conversations SET events_floor = event_cursor WHERE id = %s", (conversation_id,))
        conn.execute("DELETE FROM events WHERE conversation_id = %s", (conversation_id,))
    reconnected = client.get(history_path).json()
    (replacement,) = [item for item in reconnected["messages"] if item["id"] == str(attempt_id)]
    assert replacement["text"] == actual_text and replacement["source_refs"][0]["revision_id"] == str(faq.revision_id)
    after = snapshot(client, admitted.turn_id)
    assert after["result"] == before["result"] and after[actions][0]["status"] == "superseded"
