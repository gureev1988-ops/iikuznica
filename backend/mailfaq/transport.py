"""SMTP and IMAP ports plus a local mailbox with deterministic fault injection."""

from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import formatdate
import imaplib
import os
import re
import smtplib
import ssl
from threading import Lock
from typing import Protocol

from backend.config import HACKATHON_QUALITY_EMAIL


@dataclass(frozen=True)
class MailConfig:
    sender: str = "bot@example.test"
    recipient: str = "official@example.test"
    reply_to: str = "bot@example.test"
    trusted_senders: tuple[str, ...] = ("official@example.test",)
    authserv_id: str = "mail-gateway.example.test"
    mailbox: str = "INBOX"
    mode: str = "simulator"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_ssl: bool = False
    private_plain: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    timeout: float = 10.0
    poll_limit: int = 100
    max_inbound_bytes: int = 2 * 1024 * 1024
    trusted_bounce_senders: tuple[str, ...] = ()
    trust_gateway_mailbox_policy: bool = False
    hackathon: bool = False

    @classmethod
    def for_hackathon(cls):
        return cls(
            recipient=HACKATHON_QUALITY_EMAIL, trusted_senders=(HACKATHON_QUALITY_EMAIL,),
            mailbox="HACKATHON", hackathon=True,
        )

    @property
    def scoped_recipient(self) -> str | None:
        return self.recipient if self.hackathon else None

    @classmethod
    def from_env(cls, *, quality_address: str | None = None):
        selected = {name: os.getenv(f"MAIL_{name.upper()}") for name in (
            "sender", "recipient", "reply_to", "authserv_id", "mailbox", "mode",
            "smtp_host", "smtp_user", "smtp_password", "imap_host", "imap_user", "imap_password",
        )}
        values = {key: value for key, value in selected.items() if value is not None}
        for name in ("smtp_port", "imap_port", "poll_limit", "max_inbound_bytes"):
            if (value := os.getenv(f"MAIL_{name.upper()}")) is not None:
                values[name] = int(value)
        for name in ("trusted_senders", "trusted_bounce_senders"):
            if (value := os.getenv(f"MAIL_{name.upper()}")) is not None:
                values[name] = tuple(part.strip() for part in value.split(",") if part.strip())
        if os.getenv("MAIL_SMTP_SSL") is not None:
            values["smtp_ssl"] = os.environ["MAIL_SMTP_SSL"].casefold() in {"1", "true", "yes"}
        if os.getenv("MAIL_TRUST_GATEWAY_MAILBOX_POLICY") is not None:
            values["trust_gateway_mailbox_policy"] = os.environ[
                "MAIL_TRUST_GATEWAY_MAILBOX_POLICY"
            ].casefold() in {"1", "true", "yes"}
        if os.getenv("MAIL_TIMEOUT") is not None:
            values["timeout"] = float(os.environ["MAIL_TIMEOUT"])
        if quality_address is not None:
            recipient = values.setdefault("recipient", quality_address)
            trusted = values.setdefault("trusted_senders", (quality_address,))
            if recipient.casefold() != quality_address or tuple(address.casefold() for address in trusted) != (quality_address,):
                raise ValueError("Integrated official mail must use the designated quality mailbox")
        result = cls(**values)
        if result.mode not in {"simulator", "network", "disabled"}:
            raise ValueError("MAIL_MODE must be simulator, network or disabled")
        if result.mode == "network" and any(
            not getattr(result, key) for key in (
                "smtp_host", "imap_host", "imap_user", "imap_password", "authserv_id",
                "sender", "recipient", "reply_to", "trusted_senders",
            )
        ):
            raise ValueError("Network mail configuration is incomplete")
        if min(result.timeout, result.max_inbound_bytes, result.poll_limit) <= 0:
            raise ValueError("Mail timeout and limits must be positive")
        return result

    @property
    def cursor_key(self) -> str:
        if self.mode == "network":
            return f"imaps://{self.imap_host}:{self.imap_port}/{self.imap_user}/{self.mailbox}"
        return f"{self.mode}:{self.mailbox}"


@dataclass(frozen=True)
class Outbound:
    message_id: str
    sender: str
    recipient: str
    cc: str
    reply_to: str
    subject: str
    body: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()

    def bytes(self) -> bytes:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = self.sender
        message["To"] = self.recipient
        if self.cc.casefold() != self.recipient.casefold():
            message["Cc"] = self.cc
        message["Reply-To"] = self.reply_to
        message["Date"] = formatdate(usegmt=True)
        message["Subject"] = self.subject
        message["Message-ID"] = self.message_id
        if self.in_reply_to:
            message["In-Reply-To"] = self.in_reply_to
        if self.references:
            message["References"] = " ".join(self.references)
        message.set_content(self.body, cte="quoted-printable")
        return message.as_bytes()


@dataclass(frozen=True)
class Receipt:
    uid: int
    raw: bytes
    error: str | None = None


@dataclass(frozen=True)
class InboxPage:
    uidvalidity: str
    receipts: tuple[Receipt, ...]


class MailTransport(Protocol):
    def send(self, outbound: Outbound) -> str: ...


class MailInbox(Protocol):
    def fetch(self, uidvalidity: str | None, last_uid: int, limit: int) -> InboxPage: ...


class SmtpTransport:
    def __init__(self, config: MailConfig):
        self.config = config

    def send(self, outbound: Outbound) -> str:
        from backend.runtime import KnownDeliveryFailure

        raw = outbound.bytes()
        config = self.config
        client = None
        try:
            try:
                if config.smtp_ssl:
                    client = smtplib.SMTP_SSL(
                        config.smtp_host, config.smtp_port, timeout=config.timeout,
                        context=ssl.create_default_context(),
                    )
                else:
                    client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout)
                    if not config.private_plain:
                        client.starttls(context=ssl.create_default_context())
                client.ehlo_or_helo_if_needed()
                if config.smtp_user:
                    client.login(config.smtp_user, config.smtp_password)
                status, _ = client.mail(outbound.sender)
                if status != 250:
                    raise KnownDeliveryFailure(f"SMTP MAIL rejected ({status})")
                recipients = [outbound.recipient]
                if outbound.cc.casefold() != outbound.recipient.casefold():
                    recipients.append(outbound.cc)
                for recipient in recipients:
                    status, _ = client.rcpt(recipient)
                    if status not in {250, 251}:
                        raise KnownDeliveryFailure(f"SMTP RCPT rejected ({status})")
            except (OSError, smtplib.SMTPException) as exc:
                raise KnownDeliveryFailure("SMTP connection or envelope failed before DATA") from exc

            try:
                status, _ = client.data(raw)
            except smtplib.SMTPDataError as exc:
                raise KnownDeliveryFailure(f"SMTP DATA rejected ({exc.smtp_code})") from exc
            if status != 250:
                raise KnownDeliveryFailure(f"SMTP DATA rejected ({status})")
            return outbound.message_id
        finally:
            if client:
                try:
                    client.close()
                except OSError:
                    pass


class ImapInbox:
    def __init__(self, config: MailConfig):
        self.config = config

    def fetch(self, uidvalidity: str | None, last_uid: int, limit: int) -> InboxPage:
        config = self.config
        with self.open() as client:
            status, _ = client.select(config.mailbox, readonly=True)
            if status != "OK":
                raise OSError("IMAP mailbox selection failed")
            _, values = client.response("UIDVALIDITY")
            current = values[0].decode("ascii") if values and values[0] else ""
            if not current.isdecimal():
                raise OSError("IMAP server did not return UIDVALIDITY")
            if uidvalidity != current:
                last_uid = 0
            status, data = client.uid("SEARCH", None, "UID", f"{last_uid + 1}:*")
            if status != "OK" or not data or data[0] is None:
                raise OSError("IMAP UID search failed")
            uids = sorted({int(value) for value in data[0].split() if int(value) > last_uid})
            receipts = [self._read(client, uid) for uid in uids[:limit]]
            return InboxPage(current, tuple(receipts))

    def open(self):
        config = self.config
        if config.private_plain:
            client = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=config.timeout)
        else:
            client = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=config.timeout,
                                      ssl_context=ssl.create_default_context())
        try:
            client.login(config.imap_user, config.imap_password)
        except BaseException:
            client.shutdown()
            raise
        return client

    def latest(self, limit):
        with self.open() as client:
            status, _ = client.select(self.config.mailbox, readonly=True)
            if status != "OK":
                raise OSError("IMAP mailbox selection failed")
            status, data = client.uid("SEARCH", None, "ALL")
            if status != "OK" or not data or data[0] is None:
                raise OSError("IMAP UID search failed")
            uids = sorted((int(value) for value in data[0].split()), reverse=True)
            return tuple(self._read(client, uid) for uid in uids[:limit])

    def _read(self, client, uid: int) -> Receipt:
        status, data = client.uid("FETCH", str(uid), "(UID RFC822.SIZE)")
        size = next((
            result for item in (data or ()) if isinstance(item, bytes)
            if (result := re.search(rb"RFC822\.SIZE (\d+)", item, re.I))
        ), None)
        if status != "OK" or size is None:
            raise OSError("IMAP size fetch failed")
        if int(size[1]) > self.config.max_inbound_bytes:
            return Receipt(uid, b"", "message_too_large")
        status, data = client.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
        if status != "OK":
            raise OSError("IMAP message fetch failed")
        for item in data or ():
            if isinstance(item, tuple) and re.search(rb"\bUID " + str(uid).encode() + rb"\b", item[0]):
                if len(item[1]) > self.config.max_inbound_bytes:
                    return Receipt(uid, b"", "message_too_large")
                return Receipt(uid, item[1])
        raise OSError("IMAP response did not contain the requested UID")


class MailSimulator:
    """Local mailbox and outbound sink with no network side effects."""

    def __init__(self, config: MailConfig | None = None):
        self.config = config or MailConfig()
        self._lock = Lock()
        self._uidvalidity = 1
        self._messages: list[bytes] = []
        self.sent: list[Outbound] = []
        self.failure: str | None = None

    def send(self, outbound: Outbound) -> str:
        from backend.runtime import KnownDeliveryFailure

        with self._lock:
            if self.failure == "before_acceptance":
                raise KnownDeliveryFailure("Simulated SMTP rejection")
            self.sent.append(outbound)
            if self.failure == "after_acceptance":
                raise TimeoutError("Simulated SMTP timeout after acceptance")
            return outbound.message_id

    def inject(self, raw: bytes) -> int:
        with self._lock:
            self._messages.append(raw)
            return len(self._messages)

    def reply(
        self, outbound: Outbound, body: str, *, sender: str | None = None,
        message_id: str, trusted: bool = True, auto_submitted: str | None = None,
        references: bool = True, subject: str | None = None,
    ) -> int:
        sender = sender or self.config.recipient
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = sender
        message["To"] = outbound.reply_to
        message["Subject"] = subject if subject is not None else f"Re: {outbound.subject}"
        message["Message-ID"] = message_id
        if references:
            message["In-Reply-To"] = outbound.message_id
            message["References"] = " ".join((*outbound.references, outbound.message_id))
        if trusted:
            message["Authentication-Results"] = f"{self.config.authserv_id}; auth=pass smtp.auth={sender}"
        if auto_submitted is not None:
            message["Auto-Submitted"] = auto_submitted
        message.set_content(body)
        return self.inject(message.as_bytes())

    def reset_uidvalidity(self, messages: list[bytes] | None = None) -> None:
        with self._lock:
            self._uidvalidity += 1
            if messages is not None:
                self._messages = list(messages)

    def fetch(self, uidvalidity: str | None, last_uid: int, limit: int) -> InboxPage:
        with self._lock:
            current = str(self._uidvalidity)
            start = last_uid if uidvalidity == current else 0
            receipts = tuple(
                Receipt(index, raw) if len(raw) <= self.config.max_inbound_bytes
                else Receipt(index, b"", "message_too_large")
                for index, raw in enumerate(self._messages[start:start + limit], start + 1)
            )
            return InboxPage(current, receipts)
