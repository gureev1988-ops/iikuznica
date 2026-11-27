"""The release's first end-to-end scenario, using ordinary Reply and restarted workers."""

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import json
import os
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import Backend, create_app
from backend.config import HACKATHON_QUALITY_EMAIL, Settings
from backend.db import connect
from backend.documents import DocumentSync, PageSnapshot, list_fragments
from backend.local_mail import LocalMailbox
from .conftest import credentials

QUESTION = "Сколько дней рассматривают заявку?"
ANSWER = "Заявку рассматривают в течение 2 дней."


def worker(mode):
    subprocess.run([sys.executable, "-m", "backend", mode, "--once"], check=True, timeout=30)


def local_mail(*args, token=None):
    env = os.environ.copy()
    env.pop("II_QUALITY_TOKEN", None)
    if token:
        env["II_QUALITY_TOKEN"] = token
    return subprocess.run(
        [sys.executable, "-m", "backend", *args], env=env, text=True, capture_output=True, timeout=30,
    )


def submit(client, chat, who, key):
    response = client.post(f"/v1/conversations/{chat}/messages", headers=credentials(who),
                           json={"text": QUESTION, "client_message_id": key})
    assert response.status_code == 202, response.text
    return response.json()


def read_turn(client, accepted, who):
    res = client.get("/v1/turns/" + accepted["turn_id"], headers=credentials(who))
    assert res.status_code == 200, res.text
    return res.json()


def test_consent_reply_restart_immediate_faq_and_wiki_conflict(deployment, dsn, monkeypatch):
    settings, config_path, mailbox_path = deployment
    for name, value in {
        "MAIL_MODE": "network", "MAIL_RECIPIENT": "quality@softline.ru", "MAIL_SMTP_PORT": "invalid-stale-port",
        "MAIL_SMTP_HOST": "smtp.company.invalid", "MAIL_IMAP_HOST": "imap.company.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    settings.integrations.update({"confluence": {"ignored_old_option": True}, "dion": {"ignored_old_option": True}})
    values = json.loads(config_path.read_text())
    values["integrations"] = settings.integrations
    config_path.write_text(json.dumps(values))
    chat, second_chat = uuid4(), uuid4()
    mailbox = LocalMailbox(mailbox_path)
    with TestClient(create_app(settings)) as client:
        integrations = client.get("/v1/ops/integrations", headers=credentials("operator")).json()["integrations"]
        assert {name: item["mode"] for name, item in integrations.items()} == {
            "mail": "simulator", "dion": "disabled", "documents": "batch",
        }
        assert client.post("/v1/ops/documents/confluence/sync", headers=credentials("operator")).status_code == 404
        accepted = submit(client, chat, "first", "q1")
        assert submit(client, chat, "first", "q1") == accepted
        worker("interactive")
        turn = read_turn(client, accepted, "first")
        assert turn["result"]["action"] == "escalate"
        assert mailbox.sent() == []
        offer = turn["offers"][0]
        confirm = f"/v1/offers/{offer['id']}/confirm"
        assert client.post(confirm, headers=credentials("second"), json={"version": offer["version"]}).status_code == 404
        for _ in range(2):
            response = client.post(confirm, headers=credentials("first"), json={"version": offer["version"]})
            assert response.status_code == 200, response.text
    worker("background")
    (mid, raw), = mailbox.sent()
    sent = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(sent["From"]) == "bot@example.test"
    assert str(sent["To"]) == HACKATHON_QUALITY_EMAIL
    assert str(sent["Cc"]) == "first@example.test"
    assert str(sent["Reply-To"]) == "bot@example.test"
    assert local_mail("mail-list").returncode != 0
    denied = local_mail("mail-reply", mid, "Посторонняя публикация", token="fixture-impostor")
    assert denied.returncode != 0 and mailbox.fetch(None, 0, 100).receipts == ()
    listed = local_mail("mail-list", token="fixture-quality")
    assert listed.returncode == 0 and mid in listed.stdout
    replied = local_mail("mail-reply", mid, ANSWER + "\n\n> Исходная переписка сотрудника", token="fixture-quality")
    assert replied.returncode == 0, replied.stderr
    (received,) = mailbox.fetch(None, 0, 100).receipts
    reply = BytesParser(policy=policy.default).parsebytes(received.raw)
    assert str(reply["From"]) == HACKATHON_QUALITY_EMAIL
    assert str(reply["In-Reply-To"]) == mid
    mailbox.inject(received.raw)
    worker("background")
    with TestClient(create_app(Settings.load())) as restarted:
        path = f"/v1/conversations/{chat}/messages"
        history = restarted.get(path, headers=credentials("first")).json()
        assert sum(ANSWER in item["text"] for item in history["messages"]) == 1
        assert restarted.get(path, headers=credentials("second")).status_code == 404
        following = submit(restarted, second_chat, "second", "q2")
        worker("interactive")  # No background indexing or moderation runs after receipt.
        result = read_turn(restarted, following, "second")["result"]
        assert result["action"] == "answer" and ANSWER in result["text"]
        source = result["sources"][0]
        assert source["kind"] == "faq" and source["quote"] == ANSWER
        fragment = restarted.get(source["url"], headers=credentials("second"))
        assert fragment.status_code == 200 and fragment.json()["text"] == ANSWER
        assert "Исходная переписка" not in str(result) + fragment.text
        with connect(dsn) as conn:
            assert conn.execute("SELECT count(*) AS n FROM model_calls WHERE turn_id=%s", (following["turn_id"],)).fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM faq_entries").fetchone()["n"] == 1
    for version, text in [(1, ANSWER), (2, ANSWER.replace("2", "7"))]:
        values = json.loads(config_path.read_text())
        values["integrations"]["batch"] = [dict(external_id="deadline", version=version, title=QUESTION,
                                               storage_html=f"<p>{text}</p>", space_key="SYNTH")]
        config_path.write_text(json.dumps(values))
        with TestClient(create_app(Settings.load())) as client:
            started = client.post("/v1/ops/documents/batch/sync", headers=credentials("operator"))
            assert started.status_code in (200, 202), started.text
            worker("background")
            with connect(dsn) as conn:
                faq = conn.execute("SELECT covered_revision_id,conflict_revision_id FROM faq_entries").fetchone()
            assert faq["covered_revision_id" if version == 1 else "conflict_revision_id"] is not None
            if version == 1:
                covered = submit(client, second_chat, "second", "covered")
                worker("interactive")
                result = read_turn(client, covered, "second")["result"]
                assert result["action"] == "answer" and result["text"] == ANSWER
                citation, = result["sources"]
                assert citation["kind"] == "document" and citation["revision_id"] == str(faq["covered_revision_id"])
                fragment = client.get(citation["url"], headers=credentials("second"))
                assert fragment.status_code == 200 and ANSWER in fragment.json()["text"]
    with TestClient(create_app(Settings.load())) as client:
        conflicting = submit(client, second_chat, "second", "q3")
        worker("interactive")
        latest = read_turn(client, conflicting, "second")["result"]
        assert ANSWER in latest["text"] and latest["sources"][0]["kind"] == "faq"
        assert "расходится" in latest["notice"].casefold()
        assert read_turn(client, following, "second")["result"]["sources"][0]["revision_id"] == source["revision_id"]
        values = json.loads(config_path.read_text())
        values["integrations"]["batch"][0].update(version=3, acl_actor_ids=["first"])
        config_path.write_text(json.dumps(values))
        client.post("/v1/ops/documents/batch/sync", headers=credentials("operator")).raise_for_status()
        worker("background")
        with connect(dsn) as conn:
            faq = conn.execute("SELECT covered_revision_id, conflict_revision_id FROM faq_entries").fetchone()
            assert faq == {"covered_revision_id": None, "conflict_revision_id": None}
    assert len(mailbox.sent()) == 1


def test_quality_is_the_only_account_that_can_close_publish_or_moderate(deployment, dsn):
    settings, _, mailbox_path = deployment
    backend = Backend(settings)
    mailbox = LocalMailbox(mailbox_path)
    quality = credentials("quality")
    impostor = credentials("impostor")

    def start_request(client, key):
        chat = uuid4()
        accepted = submit(client, chat, "first", key)
        assert backend.run_job("interactive")
        offer, = read_turn(client, accepted, "first")["offers"]
        client.post(f"/v1/offers/{offer['id']}/confirm", headers=credentials("first"),
                    json={"version": offer["version"]}).raise_for_status()
        assert backend.run_job("background")
        return chat, mailbox.sent()[-1][0]

    with TestClient(create_app(services=backend)) as client:
        chat, mid = start_request(client, "close")
        prompt = local_mail("mail-reply", mid, "Уточните категорию заявки?", token="fixture-quality")
        assert prompt.returncode == 0, prompt.stderr
        assert backend.mail.poll_inbox() == 1
        request, = client.get("/v1/quality/requests", headers=quality).json()["requests"]
        followup = client.post(
            f"/v1/mail/requests/{request['id']}/followup", headers=credentials("first"),
            json={"client_id": "category", "text": "Внутренняя заявка"},
        )
        assert followup.status_code == 202
        closed = local_mail("mail-reply", mid, "Отзыв: обращение больше не требуется", token="fixture-quality")
        assert closed.returncode == 0, closed.stderr
        assert backend.mail.poll_inbox() == 1
        assert backend.run_job("background")
        assert len(mailbox.sent()) == 1
        stale = local_mail("mail-reply", mid, ANSWER, token="fixture-quality")
        assert stale.returncode == 0, stale.stderr
        assert backend.mail.poll_inbox() == 1
        queue = client.get("/v1/quality/requests", headers=quality).json()
        assert not queue["requests"]
        assert queue["inbound"][0]["reason"] == "request_withdrawn"
        assert client.get("/v1/quality/requests", headers=impostor).status_code == 403
        history = client.get(f"/v1/conversations/{chat}/messages", headers=credentials("first")).json()
        assert sum(item["text"] == "Обращение снято с рассмотрения." for item in history["messages"]) == 1
        with connect(dsn) as conn:
            assert conn.execute("SELECT state FROM mail_requests").fetchone()["state"] == "withdrawn"
            assert conn.execute("SELECT count(*) AS n FROM faq_entries").fetchone()["n"] == 0
            cancelled = conn.execute(
                "SELECT state, attempt_id FROM mail_outbound WHERE id = %s", (followup.json()["outbound_id"],),
            ).fetchone()
            assert cancelled == {"state": "cancelled", "attempt_id": None}
            historical_raw = conn.execute(
                """
                INSERT INTO mail_inbound(id, mailbox, uidvalidity, uid, fingerprint, trusted, raw, parsed, state)
                VALUES (%s, 'imaps://company.invalid', '1', 1, 'legacy-raw', true, %s, '{}', 'review')
                RETURNING id
                """,
                (uuid4(), b"private historical mail"),
            ).fetchone()["id"]
        assert client.get(f"/v1/quality/inbound/{historical_raw}/raw", headers=quality).status_code == 404

        chat, mid = start_request(client, "manual-bind")
        unlinked = EmailMessage(policy=policy.SMTP)
        unlinked["From"] = HACKATHON_QUALITY_EMAIL
        unlinked["Message-ID"] = "<unlinked@softline.test>"
        unlinked["Authentication-Results"] = f"{backend.mail_config.authserv_id}; auth=pass smtp.auth={HACKATHON_QUALITY_EMAIL}"
        unlinked.set_content(ANSWER)
        mailbox.inject(unlinked.as_bytes())
        assert backend.mail.poll_inbox() == 1
        queue = client.get("/v1/quality/requests", headers=quality).json()
        request = next(item for item in queue["requests"] if item["conversation_id"] == str(chat))
        inbound = next(item for item in queue["inbound"] if item["reason"] == "no_matching_thread")
        assert str(historical_raw) not in {item["id"] for item in queue["inbound"]}
        raw = f"/v1/quality/inbound/{inbound['id']}/raw"
        assert client.get(raw, headers=impostor).status_code == 403
        assert client.get(raw, headers=quality).content == unlinked.as_bytes()
        bind = f"/v1/quality/inbound/{inbound['id']}/bind"
        payload = {"request_id": request["id"]}
        assert client.post(bind, json=payload, headers=impostor).status_code == 403
        assert client.post(bind, json=payload, headers=quality).json()["state"] == "processed"
        with connect(dsn) as conn:
            faq = conn.execute("SELECT id, version FROM faq_entries").fetchone()
        path = f"/v1/quality/faq/{faq['id']}"
        first_withdrawal = local_mail("mail-reply", mid, "Отзыв: срок требует проверки", token="fixture-quality")
        assert first_withdrawal.returncode == 0, first_withdrawal.stderr
        original_withdrawal = mailbox.fetch(None, 0, 100).receipts[-1].raw
        assert backend.mail.poll_inbox() == 1
        replacement = {"version": faq["version"], "text": "Заявку рассматривают в течение 3 дней."}
        assert client.post(f"{path}/replace", headers=impostor, json=replacement).status_code == 403
        assert client.post(f"{path}/replace", headers=quality, json=replacement).status_code == 409
        replacement["version"] += 1
        replaced = client.post(f"{path}/replace", headers=quality, json=replacement).json()
        mailbox.inject(original_withdrawal)
        assert backend.mail.poll_inbox() == 0
        with connect(dsn) as conn:
            row = conn.execute(
                "SELECT f.status, r.state FROM faq_entries f JOIN mail_requests r ON r.id = f.request_id",
            ).fetchone()
            assert row == {"status": "active", "state": "resolved"}
        version = {"version": replaced["version"]}
        assert client.post(f"{path}/withdraw", headers=impostor, json=version).status_code == 403
        new_withdrawal = local_mail("mail-reply", mid, "Отзыв: разъяснение больше не действует", token="fixture-quality")
        assert new_withdrawal.returncode == 0, new_withdrawal.stderr
        assert backend.mail.poll_inbox() == 1
        version["version"] += 1
        assert client.post(f"{path}/withdraw", headers=quality, json=version).json()["status"] == "withdrawn"


def test_legacy_public_batch_links_survive_without_exposing_private_documents(deployment, dsn):
    settings, _, _ = deployment
    shared = PageSnapshot("old-shared", 1, "Общий старый регламент", "<p>Общий текст.</p>")
    private = PageSnapshot("old-private", 1, "Личный документ", "<p>Закрытый текст.</p>",
                           acl_actor_ids=frozenset({"first"}))
    upstream = PageSnapshot("old-upstream", 1, "Прежний источник", "<p>Содержимое Confluence.</p>",
                            upstream_url="https://company.invalid/wiki/old-upstream")
    imports = DocumentSync(dsn, {})
    imports.import_batch("batch", (shared, private))
    imports.import_batch("confluence", (upstream,))
    with connect(dsn) as conn:
        original = {fragment.title: fragment for fragment in list_fragments(conn, "first")}

    settings.integrations["batch"] = [
        dict(external_id=shared.external_id, version=shared.version, title=shared.title, storage_html=shared.storage_html),
        dict(external_id=private.external_id, version=2, title="Приватный кандидат", storage_html="<p>Новый текст.</p>"),
        dict(external_id="demo-public", version=1, title="Новый общий регламент", storage_html="<p>Новый общий текст.</p>"),
        dict(external_id="not-demo", version=1, title="Закрытый кандидат", storage_html="<p>Не общий текст.</p>",
             acl_actor_ids=["first"]),
    ]
    backend = Backend(settings)
    with TestClient(create_app(services=backend)) as client:
        old = original[shared.title]
        old_url = f"/v1/sources/{old.revision_id}/fragments/{old.fragment_id}"
        for user in ("first", "second", "quality"):
            assert client.get(old_url, headers=credentials(user)).json()["text"] == "Общий текст."
        assert client.get(old_url, headers=credentials("operator")).status_code == 403
        for title in (private.title, upstream.title):
            hidden = original[title]
            url = f"/v1/sources/{hidden.revision_id}/fragments/{hidden.fragment_id}"
            assert client.get(url, headers=credentials("first")).status_code == 404
        synced = client.post("/v1/ops/documents/batch/sync", headers=credentials("operator")).json()
        assert backend.run_job("background")
        run = client.get(f"/v1/ops/documents/sync/{synced['run_id']}", headers=credentials("operator")).json()
        assert run["source_key"] == "batch" and run["status"] == "completed"
        assert client.get(old_url, headers=credentials("second")).status_code == 200
        with connect(dsn) as conn:
            fragments = list_fragments(conn, "second", kind="document", hackathon=True)
            assert {fragment.title for fragment in fragments} == {shared.title, "Новый общий регламент"}
            assert len(fragments) == 2
            legacy = conn.execute("SELECT acl_actor_ids FROM sources WHERE external_id = 'old-private'").fetchone()
            assert legacy["acl_actor_ids"] == ["first"]
            assert conn.execute("SELECT count(*) AS n FROM sources WHERE external_id = 'old-shared'").fetchone()["n"] == 1

        settings.integrations["batch"][0].update(version=2, acl_actor_ids=["first"])
        changed = Backend(settings)
        new_run = changed.documents.request_sync("batch")
        assert changed.run_job("background")
        assert changed.documents.result(new_run).status == "completed"
        assert client.get(old_url, headers=credentials("first")).status_code == 404
        assert client.get(old_url, headers=credentials("second")).status_code == 404

        direct = PageSnapshot("demo-public", 2, "Переезд в upstream", "<p>Закрытая копия.</p>",
                              upstream_url="https://company.invalid/wiki/demo-public")
        assert changed.documents.import_batch("batch", (direct,)) == ()
        with connect(dsn) as conn:
            assert list_fragments(conn, "second", kind="document", hackathon=True) == ()
