"""Verify both copies of a test exchange through IMAP and retry the physical Reply."""
from dataclasses import replace
from email import policy
from email.parser import BytesParser
import os
import smtplib
from uuid import UUID

from backend.assembly.web import Assembly
from backend.config import Settings
from backend.db import connect
from backend.mailfaq.transport import ImapInbox


def mail_by_id(inbox, mid):
    receipts = inbox.latest(150)
    matches = [item.raw for item in receipts if not item.error and
               str(BytesParser(policy=policy.default).parsebytes(item.raw)["Message-ID"]) == mid]
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one message: {len(matches)}")
    return matches[0]


def main():
    command_id = UUID(os.environ["II_QA_REPLY_ID"])
    assembly = Assembly(Settings.load())
    with connect(assembly.dsn) as conn:
        row = conn.execute("""
            SELECT a.*, r.actor_email FROM assembly_mail_replies a
            JOIN mail_requests r ON r.id = a.request_id
            WHERE a.command_id = %s AND a.state = 'sent'
            """, (command_id,)).fetchone()
    assert row
    original, reply, guest = row["in_reply_to"], row["message_id"], row["actor_email"]
    guest_box = ImapInbox(replace(assembly.mail, imap_user=guest + "*operator",
                                imap_password=os.environ["II_IMAP_OPERATOR_PASSWORD"]))
    assert mail_by_id(assembly.inbox, original)
    assert mail_by_id(guest_box, original)
    raw = mail_by_id(assembly.bot_inbox, reply)
    assert mail_by_id(guest_box, reply)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(message["In-Reply-To"]) == original and original in str(message["References"])
    assert str(message["Cc"]) == guest
    smtp = assembly.transport.config
    with smtplib.SMTP(smtp.smtp_host, smtp.smtp_port, timeout=5) as client:
        client.login(smtp.smtp_user, smtp.smtp_password)
        assert not client.sendmail(smtp.smtp_user, [assembly.mail.sender, guest], raw)
    assert mail_by_id(assembly.bot_inbox, reply)
    assert mail_by_id(guest_box, reply)
    with connect(assembly.dsn) as conn:
        counts = conn.execute("""
            SELECT (SELECT count(*) FROM mail_inbound WHERE message_id=%s) AS receipts,
                   (SELECT count(*) FROM faq_entries WHERE request_id=%s) AS faq
            """, (reply, row["request_id"])).fetchone()
    assert counts == {"receipts": 1, "faq": 1}
    print("IMAP: quality and employee received request; bot and employee received Reply; replay yielded one receipt and FAQ.")


if __name__ == "__main__":
    main()
