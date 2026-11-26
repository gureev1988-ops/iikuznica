"""Parse and classify authenticated replies without putting raw mail in the corpus."""

from dataclasses import asdict, dataclass
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from hashlib import sha256
from html.parser import HTMLParser
import re
import unicodedata

from .transport import MailConfig, Receipt


MESSAGE_ID = re.compile(r"<[^<>\s@\x00-\x1f\x7f]+@[^<>\s@\x00-\x1f\x7f]+>")
TOKEN = re.compile(r"\[case:([A-Za-z0-9_-]{24,64})\]", re.I)
REPLY_BOUNDARY = re.compile(
    r"^(?:--\s*$|_{3,}|-{2,}\s*(?:original message|исходное сообщение|forwarded|"
    r"пересылаемое сообщение)|on .+wrote:\s*$|.+\b(?:написал[аи]?|пише(?:т|шь)):\s*$|"
    r"(?:from|от|de):\s*.+@|sent from (?:my|outlook)|"
    r"отправлено (?:с|из)|(?:best regards|kind regards|с уважением)(?:[,!. ].*)?$)",
    re.I,
)
SECTION = re.compile(
    r"^(resolution|answer|correction|pending|question|withdrawal|"
    r"решение|ответ|исправление|ожидается|вопрос|уточнение|отзыв)\s*:\s*(.*)$", re.I,
)
LABEL = {
    "resolution": "resolution", "answer": "resolution", "решение": "resolution", "ответ": "resolution",
    "correction": "correction", "исправление": "correction",
    "pending": "pending", "ожидается": "pending",
    "question": "question", "вопрос": "question", "уточнение": "question",
    "withdrawal": "withdrawal", "отзыв": "withdrawal",
}
EMAIL_ADDRESS = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-zа-яё]{2,}(?![\w.-])", re.I)
URL = re.compile(r"\b(?:https?://|mailto:|www\.)[^\s<]+", re.I)
TELEPHONE = re.compile(r"(?<![\w.])\+?(?:\d[ \t().-]*){7,}\d(?![\w.])")
AUTH_RESULT = re.compile(r"^\s*(\w+)\s*=\s*pass\b", re.I)
AUTH_FIELD = re.compile(r"""\b(smtp\.auth|header\.from)\s*=\s*(?:"([^"]+)"|([^\s;()]+))""", re.I)
ACK = re.compile(
    r"^(?:thanks|thank you|received|acknowledged|ok|спасибо|получено|принято|понятно|ок)"
    r"[.!\s]*$", re.I,
)
COUNTER = re.compile(
    r"^(?:пожалуйста[,\s]+)?(?:уточните|сообщите|пришлите|предоставьте|нужно уточнить|"
    r"please (?:clarify|provide|send|specify)|could you|can you|what|which|when|"
    r"како[йея]|какие|когда|где)\b", re.I,
)


@dataclass(frozen=True)
class ParsedMail:
    message_id: str | None
    sender: str
    trusted: bool
    thread_ids: tuple[str, ...]
    tokens: tuple[str, ...]
    kind: str
    public_text: str = ""
    prompt: str = ""
    pending: tuple[str, ...] = ()
    reason: str | None = None
    correction: bool = False

    def record(self) -> dict:
        return asdict(self)


class _HtmlBody(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.omit: list[str] = []

    def handle_starttag(self, tag, attrs):
        is_void = tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                          "meta", "param", "source", "track", "wbr"}
        classes = (dict(attrs).get("class") or "").casefold().split()
        hidden = tag in {"blockquote", "script", "style"} or any(
            name in {"gmail_quote", "gmail_signature", "protonmail_quote", "moz-signature"}
            for name in classes
        )
        if (self.omit or hidden) and not is_void:
            self.omit.append(tag)
        if tag in {"br", "p", "div", "li"} and not self.omit:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.omit and self.omit[-1] == tag:
            self.omit.pop()
        if tag in {"p", "div", "li"} and not self.omit:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.omit:
            self.parts.append(data)


def _message_ids(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for header in values for value in MESSAGE_ID.findall(str(header))))


def _address(message) -> str:
    headers = message.get_all("From", [])
    if len(headers) != 1 or getattr(headers[0], "defects", ()):
        return ""
    addresses = getattr(headers[0], "addresses", ())
    if len(addresses) != 1:
        return ""
    address = addresses[0]
    if not address.username or not address.domain or not address.addr_spec.isprintable():
        return ""
    return address.addr_spec.casefold()


def _authenticated(message, sender: str, config: MailConfig, *, dsn: bool) -> bool:
    """Use only the result generated inside the configured ingress trust boundary.

    The receiving gateway must strip externally supplied Authentication-Results
    for its authserv-id and apply the domain's mailbox submission policy.
    """
    candidates = config.trusted_senders
    if dsn:
        candidates += config.trusted_bounce_senders
    if not sender or sender not in {candidate.casefold() for candidate in candidates}:
        return False
    headers = []
    for header in message.get_all("Authentication-Results", []):
        service, _, result = str(header).partition(";")
        service = service.casefold().split()
        if service and service[0] == config.authserv_id.casefold():
            headers.append(result)
    if len(headers) != 1:
        return False
    authenticated_mailboxes = []
    aligned_dmarc = False
    for result in headers[0].split(";"):
        method = AUTH_RESULT.match(result)
        if not method:
            continue
        fields = {
            name.casefold(): (quoted or unquoted).casefold()
            for name, quoted, unquoted in AUTH_FIELD.findall(result)
        }
        if method[1].casefold() == "auth":
            authenticated_mailboxes.append(fields.get("smtp.auth"))
        if method[1].casefold() == "dmarc" and fields.get("header.from") == sender.rpartition("@")[2]:
            aligned_dmarc = True
    if authenticated_mailboxes:
        return set(authenticated_mailboxes) == {sender}
    return config.trust_gateway_mailbox_policy and aligned_dmarc


def _top_reply(body: str) -> str:
    visible = []
    for raw in body.splitlines():
        line = raw.strip().replace("\x00", "")
        if REPLY_BOUNDARY.search(line):
            break
        if not line.startswith(">"):
            visible.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(visible)).strip()


def clean_public_text(text: str) -> str:
    """Keep shareable text only, apart from the protected original MIME message."""
    text = _top_reply(text)
    text = TOKEN.sub("", text)
    text = URL.sub("[ссылка удалена]", text)
    text = EMAIL_ADDRESS.sub("[адрес удалён]", text)
    text = TELEPHONE.sub("[телефон удалён]", text)
    text = "".join(char for char in text if char == "\n" or unicodedata.category(char)[0] != "C")
    return text[:8000].strip()


def _body(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content(errors="replace")
    except (KeyError, LookupError, UnicodeError, ValueError):
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        parser = _HtmlBody()
        parser.feed(content)
        content = "".join(parser.parts)
    return _top_reply(content)


def _sections(body: str) -> dict[str, str]:
    values: dict[str, list[str]] = {}
    key = "plain"
    for line in body.splitlines():
        found = SECTION.match(line)
        if found:
            key = LABEL[found[1].casefold()]
            line = found[2]
        values.setdefault(key, []).append(line)
    return {name: "\n".join(lines).strip() for name, lines in values.items()}


def _counter_prompt(text: str) -> bool:
    return bool(COUNTER.match(text) or text.endswith("?"))


def _split_plain_reply(text: str) -> tuple[str, str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    for index, sentence in enumerate(sentences[1:], 1):
        if _counter_prompt(sentence):
            answer = " ".join(sentences[:index])
            if not (_counter_prompt(answer) or ACK.fullmatch(answer)):
                return answer, " ".join(sentences[index:])
    return text, ""


def _nested(part):
    nested = part.get_payload()
    return nested if isinstance(nested, list) else []


def parse_receipt(receipt: Receipt, config: MailConfig) -> tuple[ParsedMail, str]:
    if receipt.error:
        parsed = ParsedMail(None, "", False, (), (), "unrecognized", reason=receipt.error)
        return parsed, sha256(f"{receipt.uid}:{receipt.error}".encode()).hexdigest()

    fingerprint = sha256(receipt.raw).hexdigest()
    try:
        message = BytesParser(policy=policy.default).parsebytes(receipt.raw)
        return _parse_message(message, config), fingerprint
    except (MessageError, UnicodeError, ValueError, IndexError, RecursionError):
        return ParsedMail(None, "", False, (), (), "unrecognized", reason="malformed_mime"), fingerprint


def _parse_message(message, config: MailConfig) -> ParsedMail:
    sender = _address(message)
    ids = _message_ids(message.get_all("Message-ID", []))
    message_id = ids[0] if len(ids) == 1 and len(message.get_all("Message-ID", [])) == 1 else None
    thread_ids = _message_ids(message.get_all("In-Reply-To", []) + message.get_all("References", []))
    reports = [
        part for part in message.walk()
        if part.get_content_type() == "message/delivery-status"
    ]
    if reports:
        fields = [block for part in reports for block in _nested(part)]
        original_ids = _message_ids([block.get("Original-Message-ID", "") for block in fields])
        embedded = [part for part in message.walk() if part.get_content_type() == "message/rfc822"]
        original_ids += _message_ids([
            original.get("Message-ID", "") for part in embedded
            for original in _nested(part)
        ])
        thread_ids = tuple(dict.fromkeys((*thread_ids, *original_ids)))
        actions = {str(block.get("Action", "")).casefold() for block in fields}
    else:
        actions = set()
    body = _body(message)
    token_values = tuple(dict.fromkeys(TOKEN.findall(str(message.get("Subject", "")) + "\n" + body)))
    trusted = _authenticated(message, sender, config, dsn=bool(reports))
    base = dict(
        message_id=message_id, sender=sender, trusted=trusted,
        thread_ids=thread_ids, tokens=token_values,
    )
    if not trusted:
        parsed = ParsedMail(**base, kind="unrecognized", reason="untrusted_origin")
    elif reports:
        kind = "delivery_failed" if "failed" in actions else "delivery_notice"
        parsed = ParsedMail(**base, kind=kind)
    elif (str(message.get("Auto-Submitted", "no")).split(";")[0].strip().casefold() != "no"
          or str(message.get("Precedence", "")).casefold() in {"bulk", "junk", "list", "auto_reply"}
          or message.get("X-Autoreply") or message.get("X-Autorespond")):
        parsed = ParsedMail(**base, kind="automatic", reason="auto_reply")
    else:
        sections = _sections(body)
        text = clean_public_text(sections.get("correction", sections.get("resolution", sections.get("plain", ""))))
        prompt = clean_public_text(sections.get("question", ""))
        if text and not prompt:
            text, prompt = _split_plain_reply(text)
        pending = tuple(part for part in clean_public_text(sections.get("pending", "")).splitlines() if part)
        if "withdrawal" in sections:
            parsed = ParsedMail(**base, kind="withdrawal", prompt=clean_public_text(sections["withdrawal"]))
        elif text and not (ACK.fullmatch(text) or _counter_prompt(text)):
            kind = "correction" if "correction" in sections else "substantive"
            if prompt or pending:
                kind = "partial"
            parsed = ParsedMail(
                **base, kind=kind, public_text=text, prompt=prompt, pending=pending,
                correction="correction" in sections,
            )
        elif prompt or pending or (text and _counter_prompt(text)):
            parsed = ParsedMail(**base, kind="counter_question", prompt=prompt or text, pending=pending)
        else:
            parsed = ParsedMail(**base, kind="unrecognized", reason="no_substantive_reply")
    return parsed
