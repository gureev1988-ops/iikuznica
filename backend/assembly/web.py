"""Browser identity and the shared, server-authenticated quality mailbox."""
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from hashlib import sha256
import hmac
import imaplib
import os
from pathlib import Path
import re
from secrets import token_urlsafe
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.db import connect
from backend.dialog import Actor
from backend.documents import list_fragments
from backend.mailfaq.message import MESSAGE_ID
from backend.mailfaq.transport import ImapInbox, MailConfig, Outbound, SmtpTransport
from backend.runtime import PermanentJobFailure, enqueue, reconcile_delivery, require_lease, send_once

from .smtp_gateway import AUTHSERV, BOT, DOMAIN, QUALITY
from .demo_project import display_example, display_report_text, display_text
from .quality import DEMO_SCOPE, SCOPE, REPORT_JOB, REPORT_SUBJECT, build_router, deliver_report, reconcile_reports, report_terminal

GUEST_COOKIE = "__Host-ii-guest"
JUDGE_COOKIE = "__Host-ii-mail"
GUEST_SECONDS = 30 * 86400
JUDGE_SECONDS = 8 * 3600
REPLY_KIND = "assembly.mail.reply"


class Login(BaseModel):
    login: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class BrowserSession(BaseModel):
    conversation_id: UUID
    mail: bool
    standard_demo_login: bool


class MailAccess(BaseModel):
    mail: bool


class InboxMessage(BaseModel):
    message_id: str
    subject: str
    sender: str
    cc: str
    text: str


class ReplyState(BaseModel):
    state: str
    command_id: UUID | None = None
    message_id: str | None = None


class Reply(BaseModel):
    command_id: UUID
    message_id: str = Field(max_length=200)
    text: str = Field(min_length=1, max_length=8000)


def digest(token):
    return sha256(token.encode()).digest()


def guest_actor(id_):
    return Actor(f"guest:{id_}", frozenset({"employee"}), f"guest-{id_.hex}@{DOMAIN}", "guest")


def set_cookie(response, name, token, max_age):
    response.set_cookie(name, token, max_age=max_age, httponly=True, secure=True, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"


def browser_origin(value):
    base = urlsplit(value)
    # Validate one authority, including an optional port or bracketed IPv6 address.
    if (base.scheme != "https" or not base.hostname or base.path not in ("", "/")
            or "?" in value or "#" in value or any(ch.isspace() for ch in value)
            or not re.fullmatch(r"(?:[a-zA-Z0-9.-]+|\[[0-9a-fA-F:.]+\])(?::[0-9]+)?", base.netloc)):
        raise ValueError("Expected a single HTTPS browser origin")
    host = f"[{base.hostname}]" if ":" in base.hostname else base.hostname
    return "https://" + host + (f":{base.port}" if base.port not in (None, 443) else "")


class Assembly:
    def __init__(self, settings, *, credentials=None):
        self.settings = settings
        self.dsn = settings.dsn
        credentials = os.environ if credentials is None else credentials
        origins = [browser_origin(settings.public_base_url)]
        if legacy := credentials.get("II_LEGACY_BROWSER_ORIGIN"):
            origins.append(browser_origin(legacy))
        self.browser_origins = {}
        for origin in origins:
            self.browser_origins[origin] = origin
            if urlsplit(origin).port is None:
                self.browser_origins[origin + ":443"] = origin
        self.judge_login = credentials["II_JUDGE_LOGIN"]
        self.judge_password = credentials["II_JUDGE_PASSWORD"]
        self.demo_login = credentials.get("II_DEMO_EXPERT_LOGIN")
        self.demo_password = credentials.get("II_DEMO_EXPERT_PASSWORD")
        bot_password = credentials["II_BOT_PASSWORD"]
        quality_password = credentials["II_QUALITY_PASSWORD"]
        if min(map(len, (self.judge_password, bot_password, quality_password))) < 16:
            raise ValueError("Mail and judge passwords need at least 16 characters")
        self.mail = MailConfig(
            mode="network", sender=BOT, recipient=QUALITY, reply_to=BOT, trusted_senders=(QUALITY,),
            authserv_id=AUTHSERV, smtp_host=os.getenv("II_SMTP_HOST", "smtp"), smtp_port=1587,
            smtp_user=BOT, smtp_password=bot_password, imap_host=os.getenv("II_IMAP_HOST", "imap"),
            imap_port=1143, imap_user=BOT, imap_password=bot_password, private_plain=True,
        )
        quality_mail = replace(self.mail, smtp_user=QUALITY, smtp_password=quality_password,
                               imap_user=QUALITY, imap_password=quality_password)
        self.inbox = ImapInbox(quality_mail)
        self.bot_inbox = ImapInbox(self.mail)
        self.transport = SmtpTransport(quality_mail)

    def check_origin(self, request):
        # Caddy preserves the public Host while terminating TLS. Neither Host nor forwarded
        # headers grant access: the configured origin must also match the request authority.
        origins, hosts = request.headers.getlist("Origin"), request.headers.getlist("Host")
        if len(origins) == len(hosts) == 1 and request.headers.get("Sec-Fetch-Site") != "cross-site":
            origin = self.browser_origins.get(origins[0].lower())
            if origin and origin == self.browser_origins.get("https://" + hosts[0].lower()):
                return
        raise HTTPException(403, "Invalid request origin")

    def session(self, conn, request):
        token = request.cookies.get(GUEST_COOKIE)
        if token:
            return conn.execute("""
                SELECT id, conversation_id FROM guest_sessions WHERE token_sha256 = %s AND expires_at > now()
                """, (digest(token),)).fetchone()
        return None

    def actor_by_id(self, actor_id):
        try:
            id_ = UUID(actor_id.removeprefix("guest:"))
        except ValueError:
            return None
        with connect(self.dsn) as conn:
            row = conn.execute("SELECT id FROM guest_sessions WHERE id = %s AND expires_at > now()", (id_,)).fetchone()
        return guest_actor(row["id"]) if row else None

    def dependency(self):
        bearer = HTTPBearer(auto_error=False)

        def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
            if request.headers.get("Authorization"):
                actor = self.settings.authenticate_token(credentials.credentials if credentials else None)
                if actor is not None:
                    return actor
            else:
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    self.check_origin(request)
                with connect(self.dsn) as conn:
                    row = self.session(conn, request)
                if row is not None:
                    return guest_actor(row["id"])
            raise HTTPException(401, "A valid session is required", headers={"WWW-Authenticate": "Bearer"})

        return authenticate

    def judge_active(self, request, *, operator_only=False):
        token = request.cookies.get(JUDGE_COOKIE)
        if token:
            with connect(self.dsn) as conn:
                return conn.execute("SELECT 1 FROM judge_sessions WHERE token_sha256 = %s AND expires_at > now() AND (NOT %s OR NOT demo_only)",
                                    (digest(token), operator_only)).fetchone() is not None
        return False

    def authorized_inbox(self, request, *, operator_only=False):
        if not self.judge_active(request, operator_only=operator_only):
            raise HTTPException(401, "Войдите в общий ящик")

    def inbound(self):
        try:
            receipts = self.inbox.latest(150)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise HTTPException(503, "Почтовый ящик временно недоступен") from exc
        messages = []
        for item in receipts:
            if item.error:
                continue
            mail = BytesParser(policy=policy.default).parsebytes(item.raw)
            body = mail.get_body(preferencelist=("plain",))
            subject = str(mail["Subject"])
            text = body.get_content() if body is not None else ""
            text = display_report_text(text.replace("\r\n", "\n")) if subject == REPORT_SUBJECT else display_text(text)
            messages.append(dict(message_id=str(mail["Message-ID"]), subject=subject,
                                 sender=str(mail["From"]), cc=str(mail.get("Cc", "")), text=text))
        return messages

    @property
    def handlers(self):
        return {REPLY_KIND: self.reply_job, REPORT_JOB: lambda job: deliver_report(self, job)}

    @property
    def terminal_handlers(self):
        return {REPLY_KIND: self.reply_terminal, REPORT_JOB: report_terminal}

    def reply_terminal(self, conn, job, reason):
        conn.execute("""
            UPDATE assembly_mail_replies SET state = 'failed', updated_at = now()
            WHERE command_id = %s AND state = 'pending'
            """, (job.payload["command_id"],))

    def reply_job(self, job):
        command_id = job.payload["command_id"]
        with connect(self.dsn) as conn:
            row = conn.execute("""
                SELECT a.*, r.actor_email, r.token, r.case_number
                FROM assembly_mail_replies a JOIN mail_requests r ON r.id = a.request_id
                WHERE a.command_id = %s
                """, (command_id,)).fetchone()
        if row is None:
            raise PermanentJobFailure("mail_reply_not_found")
        mid = row["in_reply_to"]
        outbound = Outbound(row["message_id"], QUALITY, BOT, row["actor_email"], QUALITY,
                            f"Re: Обращение ОК{row['case_number']}", row["reply"],
                            mid, (*row["references_ids"], mid))
        outcome = send_once(self.dsn, channel="smtp", effect_key=f"judge:{command_id}", job=job,
                            send=lambda: self.transport.send(outbound))
        if outcome.state in {"failed", "sending"}:
            raise RuntimeError("SMTP reply not accepted")
        state = "sent" if outcome.state == "sent" else "uncertain"
        with connect(self.dsn) as conn:
            require_lease(conn, job)
            conn.execute("UPDATE assembly_mail_replies SET state = %s, updated_at = now() WHERE command_id = %s",
                         (state, command_id))

    def reconcile_mail(self, service):
        reconcile_reports(self)
        with connect(self.dsn) as conn:
            unknown = conn.execute("""
                SELECT o.id, o.attempt_id, o.message_id
                FROM mail_outbound o JOIN mail_requests r ON r.id = o.request_id
                WHERE o.state = 'delivery_unknown' AND r.recipient = %s
                ORDER BY o.updated_at LIMIT 20
                """, (QUALITY,)).fetchall()
            replies = conn.execute("""
                SELECT a.command_id, a.message_id, d.id AS attempt_id FROM assembly_mail_replies a
                JOIN delivery_attempts d ON d.effect_key = 'judge:' || a.command_id::text AND d.channel = 'smtp'
                WHERE a.state = 'uncertain' AND d.state = 'delivery_unknown'
                ORDER BY a.updated_at LIMIT 20
                """).fetchall()
        for item in unknown:
            if self.inbox.contains(item["message_id"]):
                service.reconcile_outbound(item["id"], item["attempt_id"], "sent", detail="IMAP Message-ID receipt")
        for item in replies:
            if self.bot_inbox.contains(item["message_id"]):
                with connect(self.dsn) as conn:
                    reconcile_delivery(self.dsn, conn=conn, attempt_id=item["attempt_id"], state="sent",
                                       provider_id=item["message_id"], detail="IMAP Message-ID receipt")
                    conn.execute("""
                        UPDATE assembly_mail_replies SET state='sent', updated_at=now() WHERE command_id=%s
                        """, (item["command_id"],))
        return False

    def mount(self, app, backend):
        from .analytics_web import mount_analytics
        mount_analytics(app, backend)
        from .guidance import browser_guidance

        app.include_router(browser_guidance(self))
        app.include_router(build_router(self))
        frontend = Path(__file__).with_name("frontend")
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(frontend / "index.html", headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            })

        @app.get("/healthz")
        def health():
            with connect(self.dsn) as conn:
                n = conn.execute("SELECT count(*) AS n FROM sources WHERE source_key='assembly-corpus' AND status='active'").fetchone()["n"]
            if n != 4:
                raise HTTPException(503, "Corpus not ready")
            return {"database": "ready", "regulations": n}

        @app.post("/v1/browser/session", response_model=BrowserSession)
        def session(request: Request, response: Response):
            self.check_origin(request)
            with connect(self.dsn) as conn:
                row = self.session(conn, request)
                if row is None:
                    token = token_urlsafe(32)
                    row = conn.execute("""
                        INSERT INTO guest_sessions (token_sha256, expires_at)
                        VALUES (%s, now() + make_interval(secs => %s)) RETURNING id, conversation_id
                        """, (digest(token), GUEST_SECONDS)).fetchone()
                    set_cookie(response, GUEST_COOKIE, token, GUEST_SECONDS)
                backend.dialog.ensure_conversation(conn, guest_actor(row["id"]), row["conversation_id"])
            response.headers["Cache-Control"] = "no-store"
            return {"conversation_id": row["conversation_id"], "mail": self.judge_active(request),
                    "standard_demo_login": self.demo_login == "admin" and self.demo_password == "admin"}

        @app.post("/v1/browser/mail/login", response_model=MailAccess)
        def login(request: Request, response: Response, body: Login):
            self.check_origin(request)
            with connect(self.dsn) as conn:
                guest = self.session(conn, request)
                if not guest:
                    raise HTTPException(401, "Откройте Демо DION")
                conn.execute("SELECT pg_advisory_xact_lock(31008)")
                total = conn.execute("SELECT coalesce(sum(attempts),0) AS n FROM judge_login_limits WHERE reset_at > now()").fetchone()["n"]
                if total >= 60:
                    raise HTTPException(429, "Попробуйте войти позже")
                attempts = conn.execute("""
                    INSERT INTO judge_login_limits VALUES (%s, now() + interval '15 minutes', 1)
                    ON CONFLICT (guest_id) DO UPDATE SET
                        attempts = CASE WHEN judge_login_limits.reset_at <= now() THEN 1 ELSE judge_login_limits.attempts + 1 END,
                        reset_at = CASE WHEN judge_login_limits.reset_at <= now()
                                        THEN EXCLUDED.reset_at ELSE judge_login_limits.reset_at END
                    RETURNING attempts
                    """, (guest["id"],)).fetchone()["attempts"]
                operator = (hmac.compare_digest(digest(body.login), digest(self.judge_login))
                            & hmac.compare_digest(digest(body.password), digest(self.judge_password)))
                demo = self.demo_login and self.demo_password and (
                    hmac.compare_digest(digest(body.login), digest(self.demo_login))
                    & hmac.compare_digest(digest(body.password), digest(self.demo_password)))
                if attempts <= 8 and (operator or demo):
                    token = token_urlsafe(32)
                    old = request.cookies.get(JUDGE_COOKIE)
                    if old:
                        conn.execute("DELETE FROM judge_sessions WHERE token_sha256 = %s", (digest(old),))
                    conn.execute("INSERT INTO judge_sessions VALUES (%s, now() + make_interval(secs => %s), %s)",
                                 (digest(token), JUDGE_SECONDS, not operator))
                    conn.execute("DELETE FROM judge_login_limits WHERE guest_id = %s", (guest["id"],))
                    set_cookie(response, JUDGE_COOKIE, token, JUDGE_SECONDS)
                    return {"mail": True}
            raise HTTPException(429 if attempts > 8 else 401, "Попробуйте войти позже" if attempts > 8 else "Неверный логин или пароль")

        @app.post("/v1/browser/mail/logout", response_model=MailAccess)
        def logout(request: Request, response: Response):
            self.check_origin(request)
            if token := request.cookies.get(JUDGE_COOKIE):
                with connect(self.dsn) as conn:
                    conn.execute("DELETE FROM judge_sessions WHERE token_sha256 = %s", (digest(token),))
            set_cookie(response, JUDGE_COOKIE, "", 0)
            return {"mail": False}

        @app.get("/v1/browser/mail/inbox", response_model=list[InboxMessage])
        def inbox(request: Request, response: Response):
            self.authorized_inbox(request, operator_only=True)
            response.headers["Cache-Control"] = "no-store"
            return self.inbound()

        @app.post("/v1/browser/mail/reply", status_code=202, response_model=ReplyState)
        def send_reply(request: Request, body: Reply):
            self.check_origin(request)
            self.authorized_inbox(request)
            if not MESSAGE_ID.fullmatch(body.message_id) or not body.text.strip():
                raise HTTPException(422, "Укажите письмо и текст ответа")
            with connect(self.dsn) as conn:
                original = conn.execute(f"""
                    SELECT o.request_id,o.references_ids,r.state FROM mail_outbound o
                    JOIN mail_requests r ON r.id=o.request_id
                    WHERE o.message_id=%s AND o.state='sent' AND {DEMO_SCOPE} FOR UPDATE OF r
                    """, (body.message_id, *SCOPE)).fetchone()
                if original is None:
                    raise HTTPException(404, "Неизвестная учебная цепочка")
                existing = conn.execute(
                    "SELECT command_id FROM assembly_mail_replies WHERE command_id=%s", (body.command_id,)).fetchone()
                if not existing and original["state"] in ("resolved","withdrawn"):
                    raise HTTPException(409, "Обращение уже завершено")
                conn.execute("""
                    INSERT INTO assembly_mail_replies (command_id, request_id, message_id, reply, state,
                                                       in_reply_to, references_ids)
                    VALUES (%s,%s,%s,%s,'pending',%s,%s) ON CONFLICT DO NOTHING
                    """, (body.command_id, original["request_id"], f"<judge-{body.command_id}@{DOMAIN}>",
                          body.text, body.message_id, original["references_ids"]))
                row = conn.execute("SELECT * FROM assembly_mail_replies WHERE command_id = %s FOR UPDATE",
                                   (body.command_id,)).fetchone()
                if row["in_reply_to"] != body.message_id or row["reply"] != body.text:
                    raise HTTPException(409, "Команда уже использована")
                enqueue(conn, queue="background", kind=REPLY_KIND, key=str(body.command_id),
                        payload={"command_id": str(body.command_id)}, max_attempts=7)
            return {"command_id": body.command_id, "state": row["state"]}

        @app.get("/v1/browser/mail/reply/{command_id}", response_model=ReplyState)
        def reply_status(command_id: UUID, request: Request, response: Response):
            self.authorized_inbox(request)
            response.headers["Cache-Control"] = "no-store"
            with connect(self.dsn) as conn:
                row = conn.execute(f"""
                    SELECT a.state,a.message_id FROM assembly_mail_replies a
                    JOIN mail_requests r ON r.id=a.request_id WHERE a.command_id=%s AND {DEMO_SCOPE}
                    """, (command_id,*SCOPE)).fetchone()
            if row is None:
                raise HTTPException(404)
            return row

        get_actor = self.dependency()

        @app.get("/v1/browser/catalog")
        def catalog(actor=Depends(get_actor)):
            from backend.dialog.service import _source_ref
            with connect(self.dsn) as conn:
                fragments = list_fragments(conn, actor.id)
            documents = {}
            faq = []
            for f in fragments:
                source = dict(_source_ref(f, f.text), title=f.title, anchor=f.anchor, version=f.version)
                if f.kind == "faq":
                    faq.append(display_example(source))
                else:
                    documents.setdefault(str(f.document_id), []).append(source)
            return {"documents": list(documents.values()), "faq": faq}

        @app.get("/v1/browser/correspondence")
        def correspondence(actor=Depends(get_actor)):
            with connect(self.dsn) as conn:
                rows = conn.execute("""
                    SELECT r.id, r.case_number, r.turn_id, r.case_id, r.state, o.state AS delivery
                    FROM mail_requests r JOIN mail_outbound o ON o.request_id = r.id AND o.business_key = 'initial'
                    WHERE actor_id = %s AND recipient = %s ORDER BY r.created_at
                    """, (actor.id, QUALITY)).fetchall()
            return rows
