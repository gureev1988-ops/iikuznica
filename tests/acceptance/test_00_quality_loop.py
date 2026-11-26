"""The release's first end-to-end scenario, using ordinary Reply and restarted workers."""

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import json
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db import connect
from backend.local_mail import LocalMailbox
from .conftest import credentials

QUESTION = "Сколько дней рассматривают заявку?"
ANSWER = "Заявку рассматривают в течение 2 дней."


def worker(mode):
    subprocess.run([sys.executable, "-m", "backend", mode, "--once"], check=True, timeout=30)


def submit(client, chat, who, key):
    response = client.post(f"/v1/conversations/{chat}/messages", headers=credentials(who),
                           json={"text": QUESTION, "client_message_id": key})
    assert response.status_code == 202, response.text
    return response.json()


def read_turn(client, accepted, who):
    res = client.get("/v1/turns/" + accepted["turn_id"], headers=credentials(who))
    assert res.status_code == 200, res.text
    return res.json()


def test_consent_reply_restart_immediate_faq_and_wiki_conflict(deployment, dsn):
    settings, config_path, mailbox_path = deployment
    chat, second_chat = uuid4(), uuid4()
    mailbox = LocalMailbox(mailbox_path)
    with TestClient(create_app(settings)) as client:
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
    assert str(sent["To"]) == "official@example.test"
    assert str(sent["Cc"]) == "first@example.test"
    assert str(sent["Reply-To"]) == "bot@example.test"
    reply = EmailMessage(policy=policy.SMTP)
    for key, value in {
        "From": "official@example.test", "To": str(sent["Reply-To"]), "Subject": "Re: " + str(sent["Subject"]),
        "In-Reply-To": mid, "References": mid, "Message-ID": "<accepted-reply@example.test>",
        "Authentication-Results": "mail-gateway.example.test; dmarc=pass header.from=example.test",
    }.items():
        reply[key] = value
    reply.set_content(ANSWER + "\n\n> Исходная переписка сотрудника")
    for _ in range(2):
        mailbox.inject(reply.as_bytes())
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
    with TestClient(create_app(Settings.load())) as client:
        conflicting = submit(client, second_chat, "second", "q3")
        worker("interactive")
        latest = read_turn(client, conflicting, "second")["result"]
        assert ANSWER in latest["text"] and latest["sources"][0]["kind"] == "faq"
        assert "расходится" in latest["notice"].casefold()
        assert read_turn(client, following, "second")["result"]["sources"][0]["revision_id"] == source["revision_id"]
    assert len(mailbox.sent()) == 1
