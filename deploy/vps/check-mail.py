"""Probe both private test mailboxes and authenticated SMTP from the running API."""

import imaplib
import smtplib
import time

from backend.assembly.web import Assembly
from backend.config import Settings


def check():
    assembly = Assembly(Settings.load())
    for mailbox in (assembly.bot_inbox, assembly.inbox):
        config = mailbox.config
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=5) as smtp:
            smtp.login(config.smtp_user, config.smtp_password)
        with mailbox.open() as connection:
            status, _ = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise OSError("INBOX unavailable")


for attempt in range(12):
    try:
        check()
    except (OSError, imaplib.IMAP4.error, smtplib.SMTPException):
        if attempt == 11:
            raise SystemExit("SMTP authentication or IMAP INBOX check failed") from None
        time.sleep(2)
    else:
        print("SMTP authentication and both IMAP inboxes ready")
        break
