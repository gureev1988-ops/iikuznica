"""Composition root for HTTP, interactive and background worker processes."""

from contextlib import asynccontextmanager
from functools import partial
import os

from fastapi import Depends, FastAPI, HTTPException

from backend.config import INTEGRATED_QUALITY_EMAIL, Settings
from backend.contracts.engine import build_engine
from backend.db import connect
from backend.dialog import DialogueService, create_router
from backend.documents import DocumentSync, make_router
from backend.documents.connections import configure_sources
from backend.dion import DionAdapter, DionClient, DionConfig, dion_operations_router, queue_reply
from backend.local_mail import LocalMailbox
from backend.mailfaq import MailConfig, MailService, build_router, reconcile_document_revision
from backend.mailfaq.transport import ImapInbox, SmtpTransport
from backend.runtime import operations_router, recover_journal, run_once


class Backend:
    def __init__(self, settings: Settings, *, engine=None, mail_config=None, mailbox=None, sources=None, dion_client=None):
        self.settings = settings
        self.dsn = settings.dsn
        self.closeables = []
        hackathon = settings.hackathon
        assembly = settings.profile == "assembly"
        self.assembly = None
        indexer = None
        if assembly:
            from backend.assembly.engine import A1Engine
            from backend.assembly.search import RevisionIndexer
            from backend.assembly.web import Assembly

            self.assembly = Assembly(settings)
            self.engine = A1Engine(self.dsn)
            indexer = RevisionIndexer(self.dsn)
        else:
            self.engine = build_engine(settings.engine) if hackathon else engine or build_engine(settings.engine)
        self.mail_config = (
            MailConfig.for_hackathon() if hackathon
            else self.assembly.mail if assembly
            else mail_config or MailConfig.from_env(quality_address=INTEGRATED_QUALITY_EMAIL)
        )
        self.mailbox = None if hackathon else mailbox
        if self.mailbox is None and self.mail_config.mode == "simulator":
            self.mailbox = LocalMailbox(os.getenv("II_LOCAL_MAILBOX", ".ii-local/mail.sqlite"), self.mail_config.max_inbound_bytes)
        transport = self.mailbox or SmtpTransport(self.mail_config)
        inbox = self.mailbox or ImapInbox(self.mail_config)
        self.mail = MailService(
            self.dsn, self.mail_config, transport=transport, inbox=inbox,
            resolve_actor=lambda actor_id: self.resolve_actor(actor_id, "web"),
            quality_enabled=lambda: assembly or settings.quality_admin is not None,
        )
        self.dialog = DialogueService(dsn=self.dsn, engine=self.engine, mail_config=self.mail_config,
                                     max_pending=settings.max_pending, deadline_seconds=settings.deadline_seconds,
                                     queue_reply=None if assembly else queue_reply,
                                     resolve_actor=self.resolve_actor, hackathon=hackathon)
        connection_config = None
        if hackathon or sources is None:
            sources, connection_config = configure_sources(settings)
            self.closeables.extend(source for source in sources.values() if hasattr(source, "close"))
        self.documents = DocumentSync(
            self.dsn, sources, public_only=hackathon, connection_config=connection_config, indexer=indexer,
            on_revision_change=partial(reconcile_document_revision, hackathon=True) if hackathon else None,
        )
        self.dion = None
        if hackathon or assembly:
            dion_client = None
        elif dion_client is None and (cfg := settings.integrations.get("dion")):
            dion_client = DionClient(DionConfig(**cfg))
            self.closeables.append(dion_client)
        if dion_client is not None:
            self.dion = DionAdapter(self.dsn, dion_client, self.dialog, settings.by_dion_id,
                                    public_base_url=settings.public_base_url)

    def resolve_actor(self, actor_id, channel):
        if self.assembly and actor_id.startswith("guest:"):
            return self.assembly.actor_by_id(actor_id)
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
        return self.observe(self.document_health_name, lambda: self.documents.handle_job(job))

    @property
    def document_health_name(self):
        return "documents:hackathon" if self.settings.hackathon else "documents"

    def run_job(self, queue):
        source_keys = None
        if queue == "interactive":
            handlers = {"answer_turn": self.dialog.answer_job}
            terminal = {"answer_turn": self.dialog.terminal_outcome}
            limit = self.settings.interactive_concurrency
        else:
            handlers = {"documents.sync": self.document_job}
            if self.mail_config.mode != "disabled":
                handlers.update(self.mail.handlers)
            if self.dion:
                handlers.update(self.dion.handlers)
            terminal = {"documents.sync": self.documents.terminal_job}
            if self.assembly:
                handlers.update(self.assembly.handlers)
                terminal.update(self.assembly.terminal_handlers)
            limit = self.settings.background_concurrency
            source_keys = self.document_source_keys(enabled_only=True)
        return run_once(self.dsn, queue, handlers, max_running=limit, terminal_handlers=terminal,
                        active_kinds=handlers.keys(), document_source_keys=source_keys)

    def document_source_keys(self, *, enabled_only=False):
        connections = self.documents.connections
        return [key for key, config in connections.config.items()
                if connections.visible(key) and (config.enabled or not enabled_only)]

    def polling(self):
        operations = {"recovery": lambda: any(recover_journal(self.dsn))}
        if not self.settings.hackathon:
            operations["document-schedule"] = self.documents.schedule_due
        if self.mail_config.mode != "disabled":
            operations["mail"] = lambda: self.observe("mail", self.mail.poll_inbox)
        if self.assembly:
            operations["mail-receipts"] = lambda: self.assembly.reconcile_mail(self.mail)
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
    get_actor = backend.assembly.dependency() if backend.assembly else backend.settings.actor_dependency()

    def require_ops(actor=Depends(get_actor)):
        if "ops" not in actor.roles:
            raise HTTPException(403, "Operations role required")
        return actor

    app.include_router(create_router(backend.dialog, get_actor))
    app.include_router(build_router(backend.dsn, get_actor, service=backend.mail, is_quality=backend.settings.is_quality))
    app.include_router(make_router(backend.documents, require_ops))
    app.include_router(dion_operations_router(backend.dsn, require_ops))
    source_types = {config.kind for config in backend.documents.connections.config.values() if config.enabled}
    modes = {
        "mail": backend.mail_config.mode,
        "dion": "network" if backend.dion else "disabled",
        "documents": "network" if source_types & {"confluence", "http_json"} else
                     "batch" if "batch" in source_types else "disabled",
    }
    probes = {name: (lambda n=name, m=mode: backend.health(n, m)) for name, mode in modes.items()}
    probes["documents"] = lambda: backend.health(backend.document_health_name, modes["documents"])
    app.include_router(operations_router(backend.dsn, require_ops, probes, document_source_keys=backend.document_source_keys()))
    if backend.assembly:
        backend.assembly.mount(app, backend)
    return app
