"""Exchange ordinary mail between three loopback SMTPS/IMAPS test accounts."""

from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid
import imaplib
import os
import smtplib
import ssl
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from backend.app import Backend, create_app
from backend.mailfaq import MailConfig
from .conftest import credentials
from .test_00_quality_loop import ANSWER, read_turn, submit


def find_message(user, message_id):
    with imaplib.IMAP4_SSL("localhost", 3993, ssl_context=ssl.create_default_context(), timeout=5) as box:
        box.login(user, "demo")
        assert box.select("INBOX", readonly=True)[0] == "OK"
        status, found = box.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
        assert status == "OK" and len(found[0].split()) == 1
        status, mail = box.uid("FETCH", found[0], "(BODY.PEEK[])")
        assert status == "OK"
        return BytesParser(policy=policy.default).parsebytes(mail[0][1])


@pytest.mark.skipif(os.getenv("II_LOOPBACK_MAIL") != "1", reason="Local TLS mail server is opt-in")
def test_two_mailboxes_and_employee_copy(deployment, dsn):
    from backend.db import connect

    settings, _, _ = deployment
    settings.identities[0].email = "first@sample.test"
    config = replace(MailConfig(), mode="network", sender="bot@sample.test", recipient="quality@sample.test",
                     reply_to="bot@sample.test", trusted_senders=("quality@sample.test",),
                     smtp_host="localhost", smtp_port=3465, smtp_ssl=True, smtp_user="bot", smtp_password="demo",
                     imap_host="localhost", imap_port=3993, imap_user="bot", imap_password="demo")
    services = Backend(settings, mail_config=config)
    first = uuid4()
    try:
        with TestClient(create_app(services=services)) as http:
            accepted = submit(http, first, "first", str(uuid4()))
            services.run_job("interactive")
            offer = read_turn(http, accepted, "first")["offers"][0]
            http.post(f"/v1/offers/{offer['id']}/confirm", headers=credentials("first"),
                      json={"version": offer["version"]}).raise_for_status()
            services.run_job("background")
            with connect(dsn) as conn:
                mid = conn.execute("SELECT message_id FROM mail_outbound").fetchone()["message_id"]
            received = find_message("quality", mid)
            assert find_message("first", mid)["Message-ID"] == mid
            reply = EmailMessage(policy=policy.SMTP)
            reply["From"] = config.recipient
            reply["To"] = received["Reply-To"]
            reply["Subject"] = "Re: " + str(received["Subject"])
            reply["Message-ID"] = make_msgid(domain="sample.test")
            reply["In-Reply-To"] = mid
            # GreenMail is an isolated transport stand. Corporate gateway assertions
            # are represented by this explicit fixture and verified separately from TLS.
            reply["Authentication-Results"] = (f"{config.authserv_id}; dmarc=pass header.from=sample.test; "
                                                "auth=pass smtp.auth=quality@sample.test")
            reply.set_content(ANSWER)
            with smtplib.SMTP_SSL("localhost", 3465, context=ssl.create_default_context(), timeout=5) as quality:
                quality.login("quality", "demo")
                quality.send_message(reply)
            assert services.mail.poll_inbox() >= 1
            second = submit(http, uuid4(), "second", str(uuid4()))
            services.run_job("interactive")
            answer = read_turn(http, second, "second")["result"]
            assert answer["text"] == ANSWER and answer["sources"][0]["kind"] == "faq"
            history = http.get(f"/v1/conversations/{first}/messages", headers=credentials("first")).json()
            assert sum(ANSWER in row["text"] for row in history["messages"]) == 1
    finally:
        services.close()
