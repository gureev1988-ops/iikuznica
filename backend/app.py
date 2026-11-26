"""Composition root for HTTP, interactive and background worker processes."""

from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI, HTTPException

from backend.config import Settings
from backend.contracts.engine import build_engine
from backend.db import connect
from backend.dialog import DialogueService, create_router
from backend.documents import BatchSource, ConfluenceConfig, ConfluenceSource, DocumentSync, PageSnapshot, make_router
from backend.dion import DionAdapter, DionClient, DionConfig, queue_reply
from backend.local_mail import LocalMailbox
from backend.mailfaq import MailConfig, MailService, build_router
from backend.mailfaq.transport import ImapInbox, SmtpTransport
from backend.runtime import operations_router, recover_journal, run_once


class Backend:
    def __init__(self, settings: Settings, *, engine=None, mail_config=None, mailbox=None, sources=None, dion_client=None):
        self.settings = settings
        self.dsn = settings.dsn
        self.closeables = []
        self.engine = engine or build_engine(settings.engine)
        self.mail_config = mail_config or MailConfig.from_env()
        self.mailbox = mailbox
        if self.mailbox is None and self.mail_config.mode == "simulator":
            self.mailbox = LocalMailbox(os.getenv("II_LOCAL_MAILBOX", ".ii-local/mail.sqlite"), self.mail_config.max_inbound_bytes)
        transport = self.mailbox or SmtpTransport(self.mail_config)
        inbox = self.mailbox or ImapInbox(self.mail_config)
        self.mail = MailService(self.dsn, self.mail_config, transport=transport, inbox=inbox)
        self.dialog = DialogueService(dsn=self.dsn, engine=self.engine, mail_config=self.mail_config,
                                     max_pending=settings.max_pending, queue_reply=queue_reply, resolve_actor=self.resolve_actor)
        if sources is None:
            sources = {}
            batch = settings.integrations.get("batch")
            if batch is not None:
                sources["batch"] = BatchSource(PageSnapshot(**page) for page in batch)
            if conf := settings.integrations.get("confluence"):
                connector = ConfluenceSource(ConfluenceConfig(**conf))
                sources["confluence"] = connector
                self.closeables.append(connector)
        self.documents = DocumentSync(self.dsn, sources)
        self.dion = None
        if dion_client is None and (cfg := settings.integrations.get("dion")):
            dion_client = DionClient(DionConfig(**cfg))
            self.closeables.append(dion_client)
        if dion_client is not None:
            self.dion = DionAdapter(self.dsn, dion_client, self.dialog, settings.by_dion_id,
                                    public_base_url=settings.public_base_url)

    def resolve_actor(self, actor_id, channel):
        identity = self.settings.by_actor_id(actor_id)
        return identity.actor(channel) if identity else None

    def close(self):
        for item in self.closeables:
            item.close()

    def observe(self, name, operation):
        try:
            result = operation()
        except Exception as error:
            self._record_health(name, "unavailable", type(error).__name__)
            raise
        self._record_health(name, "available", None)
        return result

    def _record_health(self, name, state, error_kind):
        with connect(self.dsn) as conn:
            conn.execute("""
                INSERT INTO integration_status (name, state, checked_at, error_kind)
                VALUES (%s,%s,clock_timestamp(),%s)
                ON CONFLICT (name) DO UPDATE SET state=EXCLUDED.state,
                checked_at=EXCLUDED.checked_at,error_kind=EXCLUDED.error_kind""",
                         (name, state, error_kind))

    def health(self, name, mode):
        if mode == "disabled":
            return {"mode": mode, "state": "disabled"}
        with connect(self.dsn) as conn:
            row = conn.execute("SELECT state,checked_at,error_kind FROM integration_status WHERE name=%s", (name,)).fetchone()
        return {"mode": mode, **(row or {"state": "not_checked"})}

    def document_job(self, job):
        return self.observe("documents", lambda: self.documents.handle_job(job))

    def run_job(self, queue):
        if queue == "interactive":
            handlers = {"answer_turn": self.dialog.answer_job}
            terminal = {"answer_turn": self.dialog.terminal_outcome}
            limit = self.settings.interactive_concurrency
        else:
            handlers = {"documents.sync": self.document_job, **self.mail.handlers}
            if self.dion:
                handlers.update(self.dion.handlers)
            terminal = {"documents.sync": self.documents.terminal_job}
            limit = self.settings.background_concurrency
        return run_once(self.dsn, queue, handlers, max_running=limit, terminal_handlers=terminal)

    def polling(self):
        operations = {"recovery": lambda: recover_journal(self.dsn)}
        if self.mail_config.mode != "disabled":
            operations["mail"] = lambda: self.observe("mail", self.mail.poll_inbox)
        if self.dion:
            operations["dion"] = lambda: self.observe("dion", lambda: self.dion.poll_once(timeout=10))
            operations["dion-route"] = self.dion.route_once
        return operations


def create_app(settings: Settings | None = None, *, services: Backend | None = None):
    backend = services or Backend(settings or Settings.load())

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if services is None:
                backend.close()

    app = FastAPI(title="ИИкузница", version="1.0.0", lifespan=lifespan)
    app.state.backend = backend
    get_actor = backend.settings.actor_dependency()

    def require_ops(actor=Depends(get_actor)):
        if "ops" not in actor.roles:
            raise HTTPException(403, "Operations role required")
        return actor

    app.include_router(create_router(backend.dialog, get_actor))
    app.include_router(build_router(backend.dsn, get_actor, service=backend.mail))
    app.include_router(make_router(backend.documents, require_ops))
    modes = {
        "mail": backend.mail_config.mode,
        "dion": "network" if backend.dion else "disabled",
        "documents": "network" if "confluence" in backend.settings.integrations else
                     "batch" if "batch" in backend.settings.integrations else "disabled",
    }
    probes = {name: (lambda n=name, m=mode: backend.health(n, m)) for name, mode in modes.items()}
    app.include_router(operations_router(backend.dsn, require_ops, probes))
    return app
