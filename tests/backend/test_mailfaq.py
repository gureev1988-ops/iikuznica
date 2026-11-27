"""Synthetic mail/FAQ lifecycle and the provider/corpus trust boundaries."""

from dataclasses import replace
from email import policy
from email.message import EmailMessage
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
import pytest

from backend.db import connect
from backend.mailfaq import (
    MailConfig, MailError, MailService, SmtpTransport, bind_inbound, build_router, create_request,
    current_faq_notification, find_faq, reconcile_document_revision, send_followup,
)
from backend.mailfaq.message import parse_receipt
from backend.mailfaq.transport import Outbound, Receipt
from backend.runtime import run_once


QUESTION = "Какой срок согласования?"
ANSWER = "Срок согласования составляет 2 дня."
USER = "staff-123"


def request(dsn, config=None, *, conditions=None):
    config = config or MailConfig()
    conversation_id, case_id, turn_id, offer_id = (uuid4() for _ in range(4))
    with connect(dsn) as conn:
        conn.execute("INSERT INTO conversations(id, owner_id) VALUES (%s, %s)", (conversation_id, USER))
        conn.execute("INSERT INTO cases(id, conversation_id) VALUES (%s, %s)", (case_id, conversation_id))
        conn.execute(
            """
            INSERT INTO turns (id, conversation_id, case_id, request_id, question)
            VALUES (%s, %s, %s, 'client', %s)
            """,
            (turn_id, conversation_id, case_id, QUESTION),
        )
        conn.execute(
            "INSERT INTO offers(id, turn_id, case_id, status, text) VALUES (%s, %s, %s, 'open', 'Send?')",
            (offer_id, turn_id, case_id),
        )
        kwargs = dict(
            offer_id=offer_id, conversation_id=conversation_id, case_id=case_id, turn_id=turn_id,
            actor_id=USER, actor_email="employee@example.test", channel="web", question=QUESTION,
            conditions=conditions or {},
        )
        with pytest.raises(MailError, match="mail_requires_confirmed_offer"):
            create_request(conn, config, **kwargs)
        conn.execute("UPDATE offers SET status = 'confirmed' WHERE id = %s", (offer_id,))
        request_id = create_request(conn, config, **kwargs)
        assert create_request(conn, config, **kwargs) == request_id
    return SimpleNamespace(
        id=request_id, conversation_id=conversation_id, case_id=case_id, turn_id=turn_id,
    )


def send(dsn, mail):
    assert run_once(dsn, "background", mail.handlers)


def client(dsn, mail):
    def actor(x_role: str = Header("employee"), x_actor: str = Header(USER)):
        email = "quality@softline.test" if x_role == "quality" else "employee@example.test"
        return SimpleNamespace(id=x_actor, roles={x_role}, email=email)

    app = FastAPI()
    app.include_router(build_router(dsn, actor, service=mail))
    return TestClient(app)


def test_ordinary_reply_publishes_only_safe_excerpt_and_replays_after_uid_reset(dsn):
    mail = MailService(dsn, MailConfig())
    origin = request(dsn)
    send(dsn, mail)
    initial = mail.simulator.sent[0]
    assert initial.sender == "bot@example.test"
    assert initial.cc == "employee@example.test"
    assert initial.reply_to == "bot@example.test"
    message_id = "<official-1@example.test>"
    mail.simulator.reply(initial, "Поддельное письмо.", message_id=message_id, trusted=False)
    mail.simulator.reply(
        initial,
        f"{ANSWER}\n\nС уважением\nperson@example.test\n> Персональные сведения",
        message_id=message_id,
    )
    restarted = MailService(dsn, MailConfig(), inbox=mail.simulator, transport=mail.simulator)
    assert restarted.poll_inbox() == 2
    assert restarted.poll_inbox() == 0
    restarted.inbox.reset_uidvalidity()
    assert restarted.poll_inbox() == 0
    with connect(dsn) as conn:
        active = find_faq(conn, "  КАКОЙ срок согласования? ", {})
        assert active.text == ANSWER
        assert conn.execute(
            "SELECT title FROM source_revisions WHERE id = %s", (active.revision_id,),
        ).fetchone()["title"] == "Официальное разъяснение"
        assert find_faq(conn, QUESTION, {"region": "west"}) is None
        assert conn.execute("SELECT count(*) AS n FROM mail_outbound").fetchone()["n"] == 1
        event = conn.execute(
            "SELECT * FROM events WHERE conversation_id = %s AND kind = 'mail.resolution'",
            (origin.conversation_id,),
        ).fetchone()
        assert event["turn_id"] == origin.turn_id
        assert event["payload"]["source_refs"] == [active.source_ref()]
        assert event["payload"]["text"] == ANSWER
        raw = conn.execute(
            "SELECT id, raw FROM mail_inbound WHERE trusted AND state = 'processed'",
        ).fetchone()
        assert b"person@example.test" in raw["raw"]
        cursor = conn.execute("SELECT uidvalidity, last_uid FROM mail_cursors").fetchone()
        assert cursor == {"uidvalidity": "2", "last_uid": 2}
    api = client(dsn, mail)
    path = f"/v1/quality/inbound/{raw['id']}/raw"
    assert api.get(path).status_code == 403
    assert api.get(path, headers={"X-Role": "admin"}).status_code == 403
    response = api.get(path, headers={"X-Role": "quality"})
    assert response.status_code == 200
    assert response.content == raw["raw"]


def test_counter_partial_followup_manual_bind_and_correction(dsn):
    mail = MailService(dsn, MailConfig())
    origin = request(dsn, conditions={"category": "internal"})
    send(dsn, mail)
    initial = mail.simulator.sent[0]
    mail.simulator.reply(
        initial, "Пожалуйста, уточните категорию документа?", message_id="<counter@example.test>",
    )
    mail.simulator.reply(
        initial, "Письмо вне цепочки.", message_id="<unmatched@example.test>",
        references=False, subject="Новый вопрос",
    )
    assert mail.poll_inbox() == 2
    api = client(dsn, mail)
    queue = api.get("/v1/quality/requests", headers={"X-Role": "quality"}).json()
    assert queue["requests"][0]["state"] == "counter_question"
    assert queue["inbound"][0]["reason"] == "no_matching_thread"
    path = f"/v1/mail/requests/{origin.id}/followup"
    data = {"client_id": "clarification", "text": "Категория — внутренняя."}
    assert api.post(path, json=data, headers={"X-Actor": "somebody"}).status_code == 404
    posted = api.post(path, json=data)
    assert posted.status_code == 202
    assert api.post(path, json=data).json() == posted.json()
    send(dsn, mail)
    followup = mail.simulator.sent[1]
    assert followup.subject == initial.subject
    assert followup.in_reply_to == "<counter@example.test>"
    assert followup.references == (initial.message_id, "<counter@example.test>")
    mail.simulator.reply(
        followup, "Ответ: Срок 2 дня.\nУточнение: Укажите отдел.\nОжидается: техническая проверка.",
        message_id="<partial@example.test>",
    )
    assert mail.poll_inbox() == 1
    with connect(dsn) as conn:
        assert find_faq(conn, QUESTION, {}) is None
        partial = find_faq(conn, QUESTION, {"category": "internal"})
        assert partial.text == "Срок 2 дня."
        assert partial.partial
        with pytest.raises(MailError, match="mail_request_not_found"):
            send_followup(conn, origin.id, actor_id="elsewhere", client_id="key", text="Отдел")
        out = send_followup(conn, origin.id, actor_id=USER, client_id="department", text="Отдел: A")
        assert out
    send(dsn, mail)
    mail.simulator.reply(
        mail.simulator.sent[2], "Исправление: Срок 3 дня.", message_id="<correction@example.test>",
        references=False,
    )
    assert mail.poll_inbox() == 1
    with connect(dsn) as conn:
        current = find_faq(conn, QUESTION, {"category": "internal"})
        assert current.text == "Срок 3 дня."
        assert not current.partial
        assert current.revision_id != partial.revision_id
        row = conn.execute(
            "SELECT text FROM source_fragments WHERE id = %s", (partial.fragment_id,),
        ).fetchone()
        assert row["text"] == partial.text

    # Binding is an independent idempotent command using the same publication process.
    orphan = queue["inbound"][0]["id"]
    with connect(dsn) as conn:
        bound = bind_inbound(conn, orphan, origin.id)
        assert bound["state"] == "processed"
        assert bind_inbound(conn, orphan, origin.id)["id"] == bound["id"]


def _document_revision(conn, source_id, version, answer, previous=None, unrelated=None):
    revision_id = uuid4()
    conn.execute(
        """
        INSERT INTO source_revisions(id, source_id, version, title, checksum, indexed_at)
        VALUES (%s, %s, %s, %s, 'synthetic', now())
        """,
        (revision_id, source_id, version, QUESTION),
    )
    conn.execute(
        """
        INSERT INTO source_fragments(id, revision_id, ordinal, anchor, text)
        VALUES (%s, %s, 0, 'fragment', %s)
        """,
        (uuid4(), revision_id, answer),
    )
    if unrelated:
        conn.execute(
            """
            INSERT INTO source_fragments(id, revision_id, ordinal, anchor, heading_path, text)
            VALUES (%s, %s, 1, 'another-section', ARRAY['Другая процедура'], %s)
            """,
            (uuid4(), revision_id, unrelated),
        )
    conn.execute(
        "UPDATE sources SET status = 'active', current_revision_id = %s WHERE id = %s",
        (revision_id, source_id),
    )
    reconcile_document_revision(conn, revision_id, previous)
    return revision_id


def test_coverage_conflict_withdrawal_and_replacement_keep_history(dsn):
    mail = MailService(dsn, MailConfig())
    request(dsn)
    send(dsn, mail)
    mail.simulator.reply(mail.simulator.sent[0], ANSWER, message_id="<a@example.test>")
    mail.poll_inbox()
    source_id = uuid4()
    with connect(dsn) as conn:
        before = find_faq(conn, QUESTION, {})
        conn.execute(
            "INSERT INTO sources(id, source_system, source_key) VALUES (%s, 'document', 'docs')",
            (source_id,),
        )
        revision_a = _document_revision(conn, source_id, 1, ANSWER)
        assert find_faq(conn, QUESTION, {}) is None
        assert current_faq_notification(conn, before.revision_id, before.fragment_id)
        covered = conn.execute("SELECT covered_revision_id FROM faq_entries").fetchone()
        assert covered["covered_revision_id"] == revision_a
        revision_b = _document_revision(
            conn, source_id, 2, "Срок согласования составляет 7 дней.", revision_a, unrelated=ANSWER,
        )
        active = find_faq(conn, QUESTION, {})
        assert active.text == ANSWER
        assert active.conflict_revision_id == revision_b
        assert active.revision_id == before.revision_id

    api = client(dsn, mail)
    headers = {"X-Role": "quality"}
    conflicts = api.get("/v1/quality/conflicts", headers=headers).json()
    assert conflicts[0]["priority"] == "regulation"
    path = f"/v1/quality/faq/{active.faq_id}"
    invalid_cover = api.post(
        f"{path}/coverage", json={"version": active.version, "revision_id": str(revision_b)}, headers=headers,
    )
    assert invalid_cover.status_code == 409
    assert api.post(f"{path}/withdraw", json={"version": before.version}, headers=headers).status_code == 409
    withdrawn = api.post(f"{path}/withdraw", json={"version": active.version}, headers=headers).json()
    assert withdrawn["status"] == "withdrawn"
    with connect(dsn) as conn:
        assert find_faq(conn, QUESTION, {}) is None
        assert not current_faq_notification(conn, before.revision_id, before.fragment_id)
    # A matching public reply after withdrawal requires an explicit correction.
    mail.simulator.reply(mail.simulator.sent[0], ANSWER, message_id="<old@example.test>")
    mail.poll_inbox()
    with connect(dsn) as conn:
        assert find_faq(conn, QUESTION, {}) is None

    replaced = api.post(
        f"{path}/replace", json={"version": withdrawn["version"], "text": "Новый срок 5 дней."},
        headers=headers,
    )
    assert replaced.status_code == 200
    with connect(dsn) as conn:
        active = find_faq(conn, QUESTION, {})
        assert active.text == "Новый срок 5 дней."
        assert active.conflict_revision_id == revision_b
        assert conn.execute(
            "SELECT text FROM source_fragments WHERE id = %s", (before.fragment_id,),
        ).fetchone()["text"] == ANSWER
        conn.execute("UPDATE sources SET status = 'access_lost' WHERE id = %s", (source_id,))
        reconcile_document_revision(conn, None, revision_b)
        assert find_faq(conn, QUESTION, {}).conflict_revision_id is None


def test_unknown_smtp_result_is_reconciled_without_blind_retry(dsn):
    mail = MailService(dsn, MailConfig())
    request(dsn)
    mail.simulator.failure = "after_acceptance"
    send(dsn, mail)
    api = client(dsn, mail)
    headers = {"X-Role": "ops"}
    outbound = api.get("/v1/admin/mail/status", headers=headers).json()["outbound"][0]
    assert outbound["state"] == "delivery_unknown"
    assert not run_once(dsn, "background", mail.handlers)
    assert len(mail.simulator.sent) == 1
    result = api.post(
        f"/v1/admin/mail/outbound/{outbound['id']}/reconcile",
        json={"attempt_id": outbound["attempt_id"], "state": "sent", "detail": "Synthetic delivery accepted"},
        headers=headers,
    )
    assert result.status_code == 200
    assert result.json()["state"] == "sent"
    assert not run_once(dsn, "background", mail.handlers)
    assert len(mail.simulator.sent) == 1


def test_trusted_reply_reconciles_smtp_and_automatic_is_distinct(dsn):
    mail = MailService(dsn, MailConfig())
    request(dsn)
    mail.simulator.failure = "after_acceptance"
    send(dsn, mail)
    initial = mail.simulator.sent[0]
    mail.simulator.reply(
        initial, "Я в отпуске.", message_id="<auto@example.test>", auto_submitted="auto-replied",
    )
    mail.poll_inbox()
    with connect(dsn) as conn:
        assert find_faq(conn, QUESTION, {}) is None
        assert conn.execute("SELECT state FROM delivery_attempts").fetchone()["state"] == "sent"
        assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "mail.automatic"
    mail.simulator.reply(initial, ANSWER, message_id="<answer@example.test>")
    mail.poll_inbox()
    with connect(dsn) as conn:
        assert find_faq(conn, QUESTION, {}).text == ANSWER


def test_authentication_results_must_prove_expected_mailbox_origin():
    cfg = MailConfig()
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = cfg.recipient
    message["Message-ID"] = "<forgery@example.test>"
    message.set_content("Безопасный текст.")
    cases = (
        ("attacker.example; dmarc=pass header.from=example.test", False),
        (f"{cfg.authserv_id}; dmarc=fail header.from=example.test", False),
        (f"{cfg.authserv_id}; dmarc=pass header.from=attacker.example", False),
        (f"{cfg.authserv_id}; dmarc=pass header.from=example.test", False),
        (f"{cfg.authserv_id}; auth=pass smtp.auth=somebody@example.test", False),
        (f"{cfg.authserv_id}; auth=pass smtp.auth=official@example.test", True),
    )
    for header, trusted in cases:
        message["Authentication-Results"] = header
        parsed, _ = parse_receipt(Receipt(1, message.as_bytes()), cfg)
        assert parsed.trusted is trusted
        del message["Authentication-Results"]

    allowed_dmarc = replace(cfg, trust_gateway_mailbox_policy=True)
    message["Authentication-Results"] = f"{cfg.authserv_id}; dmarc=pass header.from=example.test"
    parsed, _ = parse_receipt(Receipt(1, message.as_bytes()), allowed_dmarc)
    assert parsed.trusted
    del message["Authentication-Results"]
    message["Authentication-Results"] = (
        f"{cfg.authserv_id}; dmarc=pass header.from=example.test; auth=pass smtp.auth=wrong@example.test"
    )
    parsed, _ = parse_receipt(Receipt(1, message.as_bytes()), allowed_dmarc)
    assert not parsed.trusted


def test_stale_reconciliation_evidence_cannot_resolve_a_later_attempt(dsn):
    class TimeoutTransport:
        def send(self, outbound):
            raise TimeoutError("Synthetic transport broke before provider acknowledgement")

    mail = MailService(dsn, MailConfig(), transport=TimeoutTransport())
    request(dsn)
    send(dsn, mail)
    api = client(dsn, mail)
    headers = {"X-Role": "ops"}
    status = "/v1/admin/mail/status"
    assert api.get(status).status_code == 403
    first = api.get(status, headers=headers).json()["outbound"][0]
    destination = f"/v1/admin/mail/outbound/{first['id']}/reconcile"
    failure = {
        "attempt_id": first["attempt_id"], "state": "failed",
        "detail": "Synthetic provider confirmed rejection", "retry": True,
    }
    assert api.post(destination, json=failure, headers=headers).status_code == 200
    send(dsn, mail)
    next_attempt = api.get(status, headers=headers).json()["outbound"][0]
    assert next_attempt["state"] == "delivery_unknown"
    assert next_attempt["attempt_id"] != first["attempt_id"]
    assert api.post(destination, json=failure, headers=headers).status_code == 409
    assert api.get(status, headers=headers).json()["outbound"][0] == next_attempt
    assert not run_once(dsn, "background", mail.handlers)


@pytest.mark.parametrize("actor", (
    None,
    SimpleNamespace(id=USER, roles={"employee"}, email="other@example.test"),
    SimpleNamespace(id=USER, roles={"quality"}, email="employee@example.test"),
))
def test_smtp_revalidates_original_employee_and_address_before_io(dsn, actor):
    mail = MailService(dsn, MailConfig(), resolve_actor=lambda user_id: actor)
    request(dsn)
    send(dsn, mail)
    assert not mail.simulator.sent
    with connect(dsn) as conn:
        assert conn.execute("SELECT state FROM mail_outbound").fetchone()["state"] == "access_lost"
        assert conn.execute("SELECT count(*) AS n FROM delivery_attempts").fetchone()["n"] == 0


def test_delivery_status_notification_is_not_an_official_answer(dsn):
    mail = MailService(dsn, MailConfig())
    request(dsn)
    send(dsn, mail)
    initial = mail.simulator.sent[0]
    raw = f"""From: official@example.test
To: bot@example.test
Message-ID: <dsn@example.test>
Authentication-Results: {mail.config.authserv_id}; auth=pass smtp.auth=official@example.test
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary=dsn

--dsn
Content-Type: text/plain

Delivery has failed.
--dsn
Content-Type: message/delivery-status

Reporting-MTA: dns; mail.example.test
Original-Message-ID: {initial.message_id}

Final-Recipient: rfc822; undeliverable@example.test
Action: failed
Status: 5.1.1

--dsn--
""".encode("ascii")
    mail.simulator.inject(raw)
    assert mail.poll_inbox() == 1
    with connect(dsn) as conn:
        assert find_faq(conn, QUESTION, {}) is None
        assert conn.execute("SELECT state FROM mail_requests").fetchone()["state"] == "delivery_failed"
        assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "mail.delivery_failed"


def test_unstructured_mixed_reply_keeps_resolved_excerpt_out_of_private_mime():
    config = MailConfig()
    raw = EmailMessage()
    raw["From"] = config.recipient
    raw["Authentication-Results"] = f"{config.authserv_id}; auth=pass smtp.auth={config.recipient}"
    raw.set_content(
        "Срок согласования составляет 2 дня. Уточните категорию заявки?\n\n"
        "С уважением, Имя Сотрудника\nsecret@example.test"
    )
    result, _ = parse_receipt(Receipt(1, raw.as_bytes()), config)
    assert result.kind == "partial"
    assert result.public_text == ANSWER
    assert result.prompt == "Уточните категорию заявки?"


def test_smtp_transport_greets_without_login_and_uses_ascii_mime(monkeypatch):
    observed = []

    class SmtpProvider:
        def __init__(self, *args, **kwargs):
            observed.append("connected")

        def ehlo_or_helo_if_needed(self):
            observed.append("ehlo")

        def mail(self, sender):
            assert observed[-1] == "ehlo"
            observed.append(sender)
            return 250, b"sender ok"

        def rcpt(self, recipient):
            observed.append(recipient)
            return 250, b"recipient ok"

        def data(self, raw):
            assert raw.isascii()
            assert b"Content-Transfer-Encoding: quoted-printable" in raw
            observed.append("accepted")
            return 250, b"accepted"

        def close(self):
            observed.append("closed")

    monkeypatch.setattr("backend.mailfaq.transport.smtplib.SMTP_SSL", SmtpProvider)
    transport = SmtpTransport(MailConfig(smtp_host="smtp.example.test", smtp_ssl=True))
    outbound = Outbound(
        "<smtp@example.test>", "bot@example.test", "official@example.test", "staff@example.test",
        "bot@example.test", "Официальное разъяснение", ANSWER,
    )
    assert transport.send(outbound) == outbound.message_id
    assert observed == [
        "connected", "ehlo", "bot@example.test", "official@example.test", "staff@example.test", "accepted", "closed",
    ]
