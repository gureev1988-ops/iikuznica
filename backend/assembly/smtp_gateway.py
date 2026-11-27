"""Private SMTP submission server for the test domain, backed by Dovecot Maildir."""
from email import policy
from email.parser import BytesParser
import fcntl
from hashlib import sha256
import hmac
import logging
import os
from pathlib import Path
import re
import signal
from tempfile import NamedTemporaryFile
from threading import Event

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from backend.mailfaq.message import MESSAGE_ID

DOMAIN = "assembly.test"
BOT = f"bot@{DOMAIN}"
QUALITY = f"quality@{DOMAIN}"
AUTHSERV = f"smtp.{DOMAIN}"
GUEST = re.compile(r"guest-[a-f0-9]{32}@assembly\.test", re.ASCII)


def store(root, recipient, mid, raw):
    folder = root / recipient
    for directory in ("new", "cur", "tmp"):
        (folder / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    key = sha256(mid.encode("ascii")).hexdigest()
    # The private SMTP process is a single writer. The lock also covers a rolling restart.
    with (folder / ".submission").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (folder / "new" / key).exists() or any((folder / "cur").glob(key + ":*")):
            return
        with NamedTemporaryFile(dir=folder / "tmp", delete=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, folder / "new" / key)
        dir_fd = os.open(folder / "new", os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class Submission:
    def __init__(self, root):
        self.root = root

    async def handle_DATA(self, server, session, envelope):
        sender = session.auth_data
        if sender != envelope.mail_from.casefold():
            return "550 SMTP login must match envelope sender"
        target = QUALITY if sender == BOT else BOT
        recipients = {r.casefold() for r in envelope.rcpt_tos}
        copies = recipients - {target}
        report = sender == BOT and recipients == {QUALITY}
        if not report and (target not in recipients or len(copies) != 1 or not GUEST.fullmatch(next(iter(copies)))):
            return "550 Only the designated mailbox and one provisioned employee alias are allowed"
        message = BytesParser(policy=policy.SMTP).parsebytes(envelope.original_content)
        mids = message.get_all("Message-ID", [])
        source = message.get_all("From", [])
        if (len(mids) != 1 or not MESSAGE_ID.fullmatch(str(mids[0])) or not str(mids[0]).isascii()
                or len(source) != 1 or len(source[0].addresses) != 1
                or source[0].addresses[0].addr_spec.casefold() != sender):
            return "550 Invalid From or Message-ID"
        if report and not re.fullmatch(r"<quality-report-[0-9a-f-]{36}@assembly\.test>", str(mids[0])):
            return "550 Invalid report identity"
        del message["Authentication-Results"]
        message["Authentication-Results"] = f"{AUTHSERV}; auth=pass smtp.auth={sender}"
        try:
            # Deliver the copy first. Seeing the main recipient's message proves both writes occurred.
            for recipient in (*copies, target):
                store(self.root, recipient, str(mids[0]), message.as_bytes())
        except OSError:
            logging.exception("SMTP Maildir write failed")
            return "451 Could not store both copies"
        return "250 Stored"


def main():
    passwords = {BOT.encode(): os.environ["II_BOT_PASSWORD"].encode(), QUALITY.encode(): os.environ["II_QUALITY_PASSWORD"].encode()}

    def authenticate(server, session, envelope, mechanism, data):
        if isinstance(data, LoginPassword):
            password = passwords.get(data.login)
            if password is not None and hmac.compare_digest(password, data.password):
                return AuthResult(success=True, auth_data=data.login.decode("ascii"))
        return AuthResult(success=False, handled=False)

    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    controller = Controller(Submission(Path(os.getenv("II_MAILDIR", "/mail"))), hostname="0.0.0.0",
                            port=1587, authenticator=authenticate, auth_required=True,
                            auth_require_tls=False, data_size_limit=2 * 1024 * 1024)
    controller.start()
    try:
        stop.wait()
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
