"""Transactional mail intake, correspondence and delivery."""

from email.headerregistry import Address
import os
import re
from uuid import UUID, uuid4
from secrets import token_urlsafe

from psycopg.types.json import Jsonb

from backend.db import append_event, connect
from backend.runtime import (
    DeliveryStateConflict, KnownDeliveryFailure, PermanentJobFailure, enqueue, reconcile_delivery,
    require_lease, send_once,
)

from .faq import MailError, lock_sources, publish_faq, withdraw_faq
from .message import ParsedMail, parse_receipt
from .transport import ImapInbox, InboxPage, MailConfig, MailSimulator, Outbound, SmtpTransport

SEND_KIND = "mail.send"
POLL_KIND = "mail.poll"
WAITING_STATES = ("waiting_send", "waiting_reply", "counter_question", "partial", "delivery_failed")


def _email(value: str | None) -> str:
    try:
        address = Address(addr_spec=value or "")
    except (ValueError, TypeError):
        raise MailError(422, "invalid_mail_address") from None
    result = address.addr_spec
    if not address.username or not address.domain or not result.isascii() or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", result,
    ):
        raise MailError(422, "invalid_mail_address")
    return result


def _subject(request: dict) -> str:
    if request.get("channel", "guest") != "guest":
        return f"Разъяснение [case:{request['token']}]"
    return f"Обращение ОК{request['case_number']}"


def _queue_send(conn, outbound_id: UUID, *, job_key: str | None = None, max_attempts: int = 3) -> None:
    enqueue(
        conn, queue="background", kind=SEND_KIND, key=job_key or str(outbound_id),
        payload={"outbound_id": str(outbound_id)}, max_attempts=max_attempts,
    )


def _outbound(
    conn, request: dict, key: str, body: str, *,
    in_reply_to: str | None = None, references: tuple[str, ...] = (),
) -> UUID:
    outbound_id = uuid4()
    message_id = f"<{outbound_id}@{request['sender'].rpartition('@')[2]}>"
    conn.execute(
        """
        INSERT INTO mail_outbound (id, request_id, business_key, message_id, body,
                                   in_reply_to, references_ids)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (outbound_id, request["id"], key, message_id, body, in_reply_to, Jsonb(list(references))),
    )
    _queue_send(conn, outbound_id, max_attempts=7 if request["channel"] == "guest" else 3)
    return outbound_id


def create_request(
    conn, config: MailConfig, *, offer_id: UUID, conversation_id: UUID, case_id: UUID,
    turn_id: UUID, actor_id: str, actor_email: str | None, channel: str, question: str,
    conditions: dict, dialogue: list | None = None, result: dict | None = None,
) -> UUID:
    """Persist consent and its exact outbound effect in the caller's transaction."""
    if config.mode == "disabled":
        raise MailError(503, "mail_disabled")
    existing = conn.execute(
        "SELECT id FROM mail_requests WHERE offer_id = %s", (offer_id,),
    ).fetchone()
    if existing:
        return existing["id"]
    confirmed = conn.execute(
        """
        SELECT 1 FROM offers o
        JOIN conversations c ON c.id = %s
        WHERE o.id = %s AND o.case_id = %s AND o.turn_id = %s AND o.status = 'confirmed'
            AND c.owner_id = %s
        """,
        (conversation_id, offer_id, case_id, turn_id, actor_id),
    ).fetchone()
    if not confirmed:
        raise MailError(409, "mail_requires_confirmed_offer")
    request_id, token = uuid4(), token_urlsafe(24)
    row = conn.execute(
        """
        INSERT INTO mail_requests (id, offer_id, conversation_id, case_id, turn_id, actor_id,
            actor_email, channel, question, conditions, recipient, sender, reply_to, token, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'waiting_send')
        ON CONFLICT (offer_id) DO NOTHING RETURNING *
        """,
        (
            request_id, offer_id, conversation_id, case_id, turn_id, actor_id, _email(actor_email),
            channel, question, Jsonb(conditions), _email(config.recipient), _email(config.sender),
            _email(config.reply_to), token,
        ),
    ).fetchone()
    if row is None:
        return conn.execute(
            "SELECT id FROM mail_requests WHERE offer_id = %s", (offer_id,),
        ).fetchone()["id"]
    body = _initial_body(conn, row, dialogue or [], result or {})
    if channel != "guest":
        body += f"\n\n[case:{token}]"
    _outbound(conn, row, "initial", body)
    return request_id


def _initial_body(conn, request, dialogue, result):
    """Freeze evidence and the visible transcript in the initial outbox entry."""
    dialogue = [m for m in dialogue if m["case_id"] == request["case_id"]]
    initial = next((m["text"] for m in dialogue if m["role"] == "user"), request["question"])
    findings = [p["text"] for p in result.get("parts", ()) if p["action"] == "answer"]
    if result.get("text"):
        findings.append(result["text"])
    for ref in result.get("sources", ()):
        source = conn.execute("""
            SELECT r.title, r.version, f.anchor FROM source_fragments f
            JOIN source_revisions r ON r.id=f.revision_id
            WHERE f.id=%s AND r.id=%s AND r.source_id=%s
            """, (ref["fragment_id"], ref["revision_id"], ref["document_id"])).fetchone()
        if source:
            findings.append(f"{source['title']}, редакция {source['version']}, {source['anchor']}: {ref['quote']}")
            if request["channel"] == "guest" and ref["kind"] == "document":
                base = os.environ.get("II_PUBLIC_URL", "").rstrip("/")
                findings.append(f"{base}/#/regulations/{ref['document_id']}/{ref['revision_id']}?fragment={ref['fragment_id']}")
    prompts = {row["clarification_key"]: row["prompt"] for row in conn.execute("""
        SELECT clarification_key,prompt FROM clarifications WHERE case_id=%s AND status='resolved'
        ORDER BY created_at,id""", (request["case_id"],))}
    unknown = [prompts.get(key, key) for key, value in request["conditions"].items()
               if value.casefold() == "не знаю"]
    reason = ("Не удалось уточнить условия: " + ", ".join(unknown) if unknown
              else "Требуется основание для решения по вопросу: " + request["question"])
    labels = {"user": "Сотрудник", "assistant": "Бот", "system": "Система"}
    transcript = "\n".join(f"{labels[m['role']]}: {m['text']}" for m in dialogue)
    known = "; ".join(f"{prompts.get(k, k)} — {v}" for k, v in request["conditions"].items())
    if known:
        findings.append("Условия обращения: " + known)
    return "\n".join([
        "1. Вопрос от сотрудника", initial, "",
        "2. Почему направлено обращение", "Что нашли:",
        *dict.fromkeys(findings or ["Подтверждённый вывод в доступных источниках отсутствует."]),
        "Чего не хватило: " + reason, "Что просим уточнить: " + request["question"], "",
        "3. Диалог до отправки", transcript or "Сотрудник подтвердил отправку обращения.",
    ])


def _request_for_update(conn, request_id: UUID) -> dict:
    result = conn.execute(
        """
        SELECT c.id, c.owner_id FROM mail_requests r JOIN conversations c ON c.id = r.conversation_id
        WHERE r.id = %s FOR UPDATE OF c
        """,
        (request_id,),
    ).fetchone()
    if result is None:
        raise MailError(404, "mail_request_not_found")
    return conn.execute(
        "SELECT * FROM mail_requests WHERE id = %s FOR UPDATE", (request_id,),
    ).fetchone()


def send_followup(
    conn, request_id: UUID, *, actor_id: str, client_id: str, text: str, recipient: str | None = None,
) -> UUID:
    request = _request_for_update(conn, request_id)
    if ((recipient is not None and request["recipient"] != recipient)
            or request["actor_id"] != actor_id or not _has_access(conn, request)):
        raise MailError(404, "mail_request_not_found")
    clean = text.strip()
    if not clean:
        raise MailError(422, "empty_mail_followup")
    key = f"followup:{client_id}"
    body = clean if request["channel"] == "guest" else f"{clean}\n\n[case:{request['token']}]"
    prior = conn.execute(
        "SELECT id, body FROM mail_outbound WHERE request_id = %s AND business_key = %s",
        (request_id, key),
    ).fetchone()
    if prior:
        if prior["body"] != body:
            raise MailError(409, "mail_followup_key_reused")
        return prior["id"]
    if request["state"] not in {"counter_question", "partial"}:
        raise MailError(409, "mail_followup_not_expected")
    root = conn.execute(
        "SELECT message_id FROM mail_outbound WHERE request_id = %s AND business_key = 'initial'",
        (request_id,),
    ).fetchone()["message_id"]
    parent = conn.execute(
        "SELECT message_id, parsed FROM mail_inbound WHERE id = %s",
        (request["last_inbound_id"],),
    ).fetchone()
    parent_id = parent["message_id"] or root
    references = tuple(dict.fromkeys((root, *parent["parsed"]["thread_ids"], parent_id)))
    outbound_id = _outbound(conn, request, key, body, in_reply_to=parent_id, references=references)
    conn.execute(
        "UPDATE mail_requests SET state = 'waiting_send', updated_at = now() WHERE id = %s",
        (request_id,),
    )
    append_event(
        conn, request["conversation_id"], request["case_id"], request["turn_id"],
        "mail.followup_queued", {"request_id": str(request_id)},
    )
    return outbound_id


def _has_access(conn, request: dict) -> bool:
    return conn.execute(
        "SELECT 1 FROM conversations WHERE id = %s AND owner_id = %s",
        (request["conversation_id"], request["actor_id"]),
    ).fetchone() is not None


def _note(
    conn, request: dict, inbound_id: UUID, kind: str, text: str, refs: list[dict] | None = None,
    *, notify: bool = True,
) -> None:
    if not _has_access(conn, request):
        return
    refs = refs or []
    message_id = None
    if notify:
        message_id = uuid4()
        conn.execute(
            """
            INSERT INTO messages (id, conversation_id, case_id, turn_id, role, text, source_refs)
            VALUES (%s, %s, %s, %s, 'assistant', %s, %s)
            """,
            (message_id, request["conversation_id"], request["case_id"], request["turn_id"], text, Jsonb(refs)),
        )
    payload = {
        "request_id": str(request["id"]), "text": text, "source_refs": refs,
        "message_id": str(message_id) if message_id else None,
    }
    append_event(
        conn, request["conversation_id"], request["case_id"], request["turn_id"], f"mail.{kind}", payload,
    )
    if notify and request["channel"] == "dion":
        from backend.dion import queue_reply

        queue_reply(
            conn, effect_key=f"mail:{inbound_id}", conversation_id=request["conversation_id"],
            case_id=request["case_id"], turn_id=request["turn_id"], actor_id=request["actor_id"],
            text=text, source_refs=refs,
            mail_request_id=request["id"] if kind in {"counter_question", "partial"} else None,
        )


def _correlate(conn, parsed: ParsedMail, recipient: str | None) -> tuple[UUID | None, str]:
    rows = conn.execute(
        """
        WITH matching AS (
            SELECT request_id FROM mail_outbound WHERE message_id = ANY(%s)
            UNION
            SELECT request_id FROM mail_inbound WHERE message_id = ANY(%s) AND trusted
                AND request_id IS NOT NULL AND state = 'processed'
            UNION
            SELECT id FROM mail_requests WHERE token = ANY(%s)
        )
        SELECT m.request_id FROM matching m JOIN mail_requests r ON r.id = m.request_id
        WHERE (%s::text IS NULL OR r.recipient = %s)
        """,
        (list(parsed.thread_ids), list(parsed.thread_ids), list(parsed.tokens), recipient, recipient),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["request_id"], ""
    if rows:
        return None, "ambiguous_thread"
    return None, "no_matching_thread"


def _reconcile_reply(conn, parsed: ParsedMail, request_id: UUID) -> None:
    if not parsed.thread_ids:
        return
    rows = conn.execute(
        """
        SELECT o.id, a.id AS attempt_id, o.message_id
        FROM mail_outbound o JOIN delivery_attempts a
            ON a.effect_key = o.id::text AND a.channel = 'smtp' AND a.state = 'delivery_unknown'
        WHERE o.request_id = %s AND o.message_id = ANY(%s)
        """,
        (request_id, list(parsed.thread_ids)),
    ).fetchall()
    for row in rows:
        try:
            result = reconcile_delivery(
                None, conn=conn, attempt_id=row["attempt_id"], state="sent",
                provider_id=row["message_id"], detail="trusted_thread_reply",
            )
        except DeliveryStateConflict:
            continue
        conn.execute(
            "UPDATE mail_outbound SET state = 'sent', attempt_id = %s, updated_at = now() WHERE id = %s",
            (result.attempt_id, row["id"]),
        )


def _apply_inbound(conn, inbound_id: UUID, request: dict, parsed: ParsedMail, *, hackathon: bool = False, norm_review=None) -> None:
    conn.execute(
        "UPDATE mail_inbound SET state = 'processed', request_id = %s, reason = NULL WHERE id = %s",
        (request["id"], inbound_id),
    )
    _reconcile_reply(conn, parsed, request["id"])
    kind = parsed.kind
    if kind == "status":
        _note(conn, request, inbound_id, kind, "Отдел качества · ход работы\n" + parsed.public_text)
        return
    if kind == "automatic":
        _note(conn, request, inbound_id, kind, "Получен автоматический ответ.", notify=False)
        return
    if kind == "delivery_notice":
        _note(conn, request, inbound_id, kind, "Получено уведомление почтового сервера.", notify=False)
        return
    if kind == "delivery_failed":
        # A DSN is its own event. It must not overwrite an official answer that
        # arrived from another recipient or supersede an open counter-question.
        conn.execute(
            """
            UPDATE mail_requests SET state = 'delivery_failed', updated_at = now()
            WHERE id = %s AND state IN ('waiting_send', 'waiting_reply')
            """,
            (request["id"],),
        )
        _note(conn, request, inbound_id, kind, "Почтовый сервер сообщил о недоставке обращения.")
        return

    if kind == "withdrawal":
        if request["state"] == "withdrawn":
            return
        prior = conn.execute(
            "SELECT id, version, status FROM faq_entries WHERE request_id = %s",
            (request["id"],),
        ).fetchone()
        if prior and prior["status"] == "active":
            withdraw_faq(conn, prior["id"], prior["version"], hackathon=hackathon)
        elif not prior:
            sent = conn.execute(
                """
                SELECT 1 FROM mail_outbound o LEFT JOIN delivery_attempts a
                    ON a.channel = 'smtp' AND a.effect_key = o.id::text AND a.state = 'sent'
                WHERE o.request_id = %s AND (o.state = 'sent' OR a.id IS NOT NULL) LIMIT 1
                """,
                (request["id"],),
            ).fetchone()
            if not sent:
                conn.execute(
                    "UPDATE mail_inbound SET state = 'review', reason = 'withdrawal_before_send' WHERE id = %s",
                    (inbound_id,),
                )
                return
        conn.execute(
            "UPDATE mail_requests SET state = 'withdrawn', pending = '[]', updated_at = now() WHERE id = %s",
            (request["id"],),
        )
        conn.execute(
            """
            UPDATE mail_outbound o SET state = 'cancelled', updated_at = now()
            WHERE o.request_id = %s AND o.business_key <> 'initial' AND o.state IN ('queued', 'failed')
              AND NOT EXISTS (
                  SELECT 1 FROM delivery_attempts a WHERE a.channel = 'smtp' AND a.effect_key = o.id::text
                      AND a.state IN ('sent', 'delivery_unknown')
              )
            """,
            (request["id"],),
        )
        text = "Официальное разъяснение по обращению отозвано." if prior else "Обращение снято с рассмотрения."
        _note(conn, request, inbound_id, kind, text)
        return

    previous = conn.execute(
        """
        SELECT f.status, fragment.text FROM faq_entries f
        JOIN source_fragments fragment ON fragment.id = f.current_fragment_id
        WHERE f.request_id = %s
        """,
        (request["id"],),
    ).fetchone()
    if (request["state"] == "withdrawn" or (previous and previous["status"] == "withdrawn")) and not parsed.correction:
        conn.execute(
            "UPDATE mail_inbound SET state = 'review', reason = %s WHERE id = %s",
            ("faq_withdrawn" if previous else "request_withdrawn", inbound_id),
        )
        return

    pending = list(dict.fromkeys((*parsed.pending, *([parsed.prompt] if parsed.prompt else []))))
    if kind == "counter_question":
        state = "partial" if previous else kind
        message = "Для ответа требуется уточнение."
        if pending:
            message += "\n\n" + "\n".join(pending)
    else:
        state = "partial" if kind == "partial" else "resolved"
        message = parsed.public_text
    updated = conn.execute(
        """
        UPDATE mail_requests SET state = %s, pending = %s, last_inbound_id = %s, updated_at = now()
        WHERE id = %s RETURNING *
        """,
        (state, Jsonb(pending), inbound_id, request["id"]),
    ).fetchone()

    refs = []
    if kind == "partial" and request["channel"] == "guest":
        message = "Частичный ответ Отдела качества\n" + parsed.public_text
        if pending:
            message += "\n\nОстаётся открытым:\n" + "\n".join(pending)
    elif parsed.public_text:
        text = parsed.public_text
        if previous and request["channel"] != "guest" and request["pending"] and not parsed.correction and text != previous["text"]:
            text = previous["text"] + "\n\n" + text
        match = publish_faq(conn, updated, text, replace=parsed.correction, hackathon=hackathon)
        if match:
            message = match.text
            if updated["channel"] == "guest":
                message = f"Разъяснение Отдела качества\n{match.text}"
            refs = [match.source_ref()]
            if updated["channel"] == "guest":
                from backend.assembly.quality import review_after_mail
                review = review_after_mail(conn, match.faq_id, prepared=norm_review)
                if review.get("outcome") == "conflict" and review.get("ref"):
                    refs.insert(0, review["ref"])
                    message = (f"Действующий регламент: {review['ref']['quote']} [1]\n\n"
                               f"Разъяснение Отдела качества: {match.text} [2]\n\n"
                               "Руководствуйтесь регламентом. Передать расхождение в Отдел качества?")
                    # A verified conflict gets its own offer. Consent to the first letter is already spent.
                    turn = conn.execute("""
                        INSERT INTO turns(conversation_id,case_id,request_id,question,status,request,result)
                        VALUES (%s,%s,%s,%s,'completed',%s,%s) RETURNING id
                        """, (updated["conversation_id"], updated["case_id"], f"mail-conflict:{inbound_id}",
                              updated["question"], Jsonb({"channel": "guest"}), Jsonb({
                                  "action": "answer", "text": message, "sources": refs}))).fetchone()
                    superseded = conn.execute("""UPDATE offers SET status='superseded',updated_at=now()
                        WHERE case_id=%s AND status='open' RETURNING id,turn_id,case_id,version,status,text""",
                                              (updated["case_id"],)).fetchall()
                    from backend.dialog.service import _public_action
                    for old in superseded:
                        append_event(conn, updated["conversation_id"], old["case_id"], old["turn_id"],
                                     "offer.superseded", {"offer": _public_action(old)})
                    conn.execute("""INSERT INTO offers(turn_id,case_id,text,payload) VALUES (%s,%s,%s,%s)""",
                                 (turn["id"], updated["case_id"], message, Jsonb({
                                     "conditions": updated["conditions"], "question": updated["question"]})))
                    updated = dict(updated, turn_id=turn["id"])
            elif match.conflict_revision_id:
                message += "\n\nОфициальное разъяснение расходится с текущей редакцией документа."
        if pending:
            message += "\n\nОжидается уточнение:\n" + "\n".join(pending)
    _note(conn, updated, inbound_id, "resolution" if kind == "substantive" else kind, message, refs)


def _store_receipt(conn, mailbox: str, uidvalidity: str, uid: int, raw: bytes,
                   parsed: ParsedMail, fingerprint: str, config: MailConfig) -> bool:
    inbound_id = uuid4()
    inserted = conn.execute(
        """
        INSERT INTO mail_inbound (id, mailbox, uidvalidity, uid, fingerprint, message_id, trusted,
                                  raw, parsed, state, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'review', %s)
        ON CONFLICT DO NOTHING RETURNING id
        """,
        (inbound_id, mailbox, uidvalidity, uid, fingerprint, parsed.message_id, parsed.trusted,
         raw, Jsonb(parsed.record()), parsed.reason),
    ).fetchone()
    if inserted is None:
        return False
    if not parsed.trusted:
        conn.execute("UPDATE mail_inbound SET state = 'rejected' WHERE id = %s", (inbound_id,))
        return True
    request_id, reason = _correlate(conn, parsed, config.scoped_recipient)
    if request_id and parsed.kind in {"substantive", "correction", "partial", "counter_question", "unrecognized"}:
        request = _request_for_update(conn, request_id)
        if request["channel"] == "guest":
            conn.execute("UPDATE mail_inbound SET request_id=%s,state='interpreting' WHERE id=%s",
                         (request_id, inbound_id))
            enqueue(conn, queue="background", kind="mail.interpret", key=str(inbound_id),
                    payload={"request_id": str(request_id)}, max_attempts=5)
            return True
    if request_id is None or parsed.kind == "unrecognized":
        conn.execute(
            "UPDATE mail_inbound SET request_id = %s, reason = %s WHERE id = %s",
            (request_id, parsed.reason or reason, inbound_id),
        )
        return True
    _apply_inbound(conn, inbound_id, _request_for_update(conn, request_id), parsed, hackathon=config.hackathon)
    return True


def bind_inbound(conn, inbound_id: UUID, request_id: UUID, *, config: MailConfig | None = None) -> dict:
    lock_sources(conn)
    row = conn.execute("SELECT * FROM mail_inbound WHERE id = %s FOR UPDATE", (inbound_id,)).fetchone()
    hackathon = config is not None and config.hackathon
    if row is None or (hackathon and row["mailbox"] != config.cursor_key):
        raise MailError(404, "inbound_mail_not_found")
    request = _request_for_update(conn, request_id)
    if hackathon and request["recipient"] != config.recipient:
        raise MailError(404, "mail_request_not_found")
    if row["state"] == "processed" and row["request_id"] == request_id:
        return row
    if not row["trusted"] or row["state"] != "review" or row["reason"] not in {
        "no_matching_thread", "ambiguous_thread",
    }:
        raise MailError(409, "inbound_mail_cannot_be_bound")
    parsed = ParsedMail(**row["parsed"])
    _apply_inbound(conn, inbound_id, request, parsed, hackathon=hackathon)
    return conn.execute("SELECT * FROM mail_inbound WHERE id = %s", (inbound_id,)).fetchone()


class MailService:
    def __init__(
        self, dsn: str | None, config: MailConfig, *, transport=None, inbox=None,
        resolve_actor=None, quality_enabled=None,
    ):
        self.dsn = dsn
        self.config = config
        self.simulator = MailSimulator(config) if config.mode == "simulator" else None
        self.transport = transport or self.simulator or SmtpTransport(config)
        self.inbox = inbox or self.simulator or ImapInbox(config)
        self.resolve_actor = resolve_actor
        self.quality_enabled = quality_enabled

    @property
    def handlers(self):
        return {SEND_KIND: self.send_job, POLL_KIND: self.poll_job, "mail.interpret": self.interpret_job}

    def interpret_job(self, job):
        from backend.contracts.engine import EngineUnavailable
        from .interpret import interpret

        request_id = job.payload["request_id"]
        # Process one reply per durable job in thread order, even with competing workers.
        while True:
            with connect(self.dsn) as conn:
                request = conn.execute("SELECT * FROM mail_requests WHERE id=%s", (request_id,)).fetchone()
                receipt = conn.execute("""SELECT * FROM mail_inbound WHERE request_id=%s AND state='interpreting'
                                          ORDER BY created_at, uid, id LIMIT 1""", (request_id,)).fetchone()
                if receipt is None:
                    return
                chain = [r["parsed"] for r in conn.execute("""
                    SELECT parsed - 'tokens' - 'thread_ids' - 'sender' - 'message_id' AS parsed
                    FROM mail_inbound WHERE request_id=%s AND state='processed' ORDER BY created_at,uid,id
                    """, (request_id,))]
                followups = conn.execute("""SELECT body FROM mail_outbound WHERE request_id=%s
                    AND business_key <> 'initial' AND state='sent' AND created_at <= %s ORDER BY created_at,id""",
                                         (request_id, receipt["created_at"])).fetchall()
                chain.extend(dict(kind="employee_followup", text=row["body"]) for row in followups)
            parsed = ParsedMail(**receipt["parsed"])
            try:
                result = interpret(self.dsn, job, request, bytes(receipt["raw"]), parsed, chain)
            except EngineUnavailable:
                if job.attempt < job.max_attempts:
                    raise RuntimeError("mail_interpreter_unavailable") from None
                from dataclasses import replace
                result = replace(parsed, kind="unrecognized", reason="interpreter_unavailable")
            norm_review = None
            if result.kind in {"substantive", "correction"} and result.public_text:
                from backend.assembly.quality import prepare_review
                with connect(self.dsn) as conn:
                    norm_review = prepare_review(conn, request["question"], result.public_text, request["conditions"])
            with connect(self.dsn) as conn:
                require_lease(conn, job)
                lock_sources(conn)
                current = _request_for_update(conn, request_id)
                state = conn.execute("SELECT state FROM mail_inbound WHERE id=%s FOR UPDATE",
                                     (receipt["id"],)).fetchone()["state"]
                if state != "interpreting":
                    continue
                if current["updated_at"] != request["updated_at"]:
                    continue
                conn.execute("UPDATE mail_inbound SET parsed=%s WHERE id=%s",
                             (Jsonb(result.record()), receipt["id"]))
                if result.kind == "unrecognized":
                    conn.execute("UPDATE mail_inbound SET state='review',reason=%s WHERE id=%s",
                                 (result.reason, receipt["id"]))
                else:
                    _apply_inbound(conn, receipt["id"], current, result, norm_review=norm_review)
                return

    def _can_send(self, outbound: dict) -> bool:
        if self.resolve_actor is None:
            return self.config.mode == "simulator"
        actor = self.resolve_actor(outbound["actor_id"])
        if actor is None or actor.id != outbound["actor_id"] or "employee" not in actor.roles:
            return False
        try:
            return _email(actor.email).casefold() == outbound["actor_email"].casefold()
        except MailError:
            return False

    def send_job(self, job) -> None:
        if self.config.mode == "disabled":
            raise PermanentJobFailure("mail_disabled")
        outbound_id = UUID(job.payload["outbound_id"])
        with connect(self.dsn) as conn:
            row = conn.execute(
                """
                SELECT o.*, r.conversation_id, r.actor_email, r.actor_id, r.sender, r.recipient,
                    r.reply_to, r.token, r.channel, r.case_number, r.state AS request_state
                FROM mail_outbound o JOIN mail_requests r ON r.id = o.request_id
                WHERE o.id = %s
                """,
                (outbound_id,),
            ).fetchone()
            if row is None:
                raise PermanentJobFailure("mail_outbound_not_found")
            if self.config.hackathon and row["recipient"] != self.config.recipient:
                raise PermanentJobFailure("mail_outbound_outside_profile")
            if row["request_state"] == "withdrawn" or row["state"] == "cancelled":
                return
            if not _has_access(conn, row) or not self._can_send(row):
                require_lease(conn, job)
                _request_for_update(conn, row["request_id"])
                conn.execute(
                    "UPDATE mail_outbound SET state = 'access_lost', updated_at = now() WHERE id = %s",
                    (outbound_id,),
                )
                conn.execute(
                    """
                    UPDATE mail_requests SET state = 'delivery_failed', updated_at = now()
                    WHERE id = %s AND state = 'waiting_send'
                    """,
                    (row["request_id"],),
                )
                return
        mail = Outbound(
            row["message_id"], row["sender"], row["recipient"], row["actor_email"], row["reply_to"],
            _subject(row), row["body"], row["in_reply_to"], tuple(row["references_ids"]),
        )

        def deliver():
            # The journal already holds a durable sending record. Recheck the
            # conversation state at the final pre-I/O boundary.
            with connect(self.dsn) as conn:
                request = _request_for_update(conn, row["request_id"])
                outbound = conn.execute(
                    "SELECT state FROM mail_outbound WHERE id = %s", (outbound_id,),
                ).fetchone()
                if request["state"] == "withdrawn" or outbound["state"] == "cancelled":
                    raise KnownDeliveryFailure("mail_request_withdrawn")
            return self.transport.send(mail)

        result = send_once(
            self.dsn, channel="smtp", effect_key=str(outbound_id), job=job,
            send=deliver,
        )
        if result.state == "sending":
            raise RuntimeError("smtp_attempt_pending")
        cancelled = result.state == "failed" and result.detail == "mail_request_withdrawn"
        with connect(self.dsn) as conn:
            require_lease(conn, job)
            request = _request_for_update(conn, row["request_id"])
            state = conn.execute(
                "SELECT state FROM delivery_attempts WHERE id = %s", (result.attempt_id,),
            ).fetchone()["state"]
            conn.execute(
                """
                UPDATE mail_outbound SET state = %s, attempt_id = %s, updated_at = now()
                WHERE id = %s
                """,
                ("cancelled" if cancelled else state, result.attempt_id, outbound_id),
            )
            if request["state"] == "waiting_send" and not cancelled:
                if state == "sent":
                    state = "waiting_reply"
                elif state == "delivery_unknown":
                    state = "delivery_failed" if self.config.private_plain else "waiting_reply"
                elif job.attempt >= job.max_attempts:
                    state = "delivery_failed"
                else:
                    state = "waiting_send"
                conn.execute(
                    "UPDATE mail_requests SET state = %s, updated_at = now() WHERE id = %s",
                    (state, row["request_id"]),
                )
        if result.state == "failed" and not cancelled:
            raise RuntimeError(result.detail or "smtp_rejected")

    def poll_job(self, job) -> None:
        self.poll_inbox(job=job)

    def poll_inbox(self, *, job=None) -> int:
        """Read a bounded page and commit receipts, cursor and resulting effects together."""
        if self.config.mode == "disabled":
            return 0
        mailbox = self.config.cursor_key
        with connect(self.dsn) as conn:
            before = self._cursor(conn, mailbox)
        page = self.inbox.fetch(before[0], before[1], self.config.poll_limit)
        return self._ingest_page(mailbox, before, page, job=job)

    @staticmethod
    def _cursor(conn, mailbox: str, *, write: bool = False):
        lock = "FOR UPDATE" if write else ""
        row = conn.execute(
            f"SELECT uidvalidity, last_uid FROM mail_cursors WHERE mailbox = %s {lock}", (mailbox,),
        ).fetchone()
        return (row["uidvalidity"], row["last_uid"]) if row else (None, 0)

    def _ingest_page(self, mailbox: str, before, page: InboxPage, *, job=None) -> int:
        minimum = before[1] if before[0] == page.uidvalidity else 0
        receipts = sorted(
            (item for item in page.receipts if item.uid > minimum), key=lambda item: item.uid,
        )
        if not receipts and page.uidvalidity == before[0]:
            return 0
        parsed = [(receipt, *parse_receipt(receipt, self.config)) for receipt in receipts]
        with connect(self.dsn) as conn:
            if job is not None:
                require_lease(conn, job)
            lock_sources(conn)
            if self._cursor(conn, mailbox, write=True) != before:
                return 0
            inserted = 0
            for receipt, message, fingerprint in parsed:
                if (message.trusted and message.kind in {"substantive", "correction", "partial", "counter_question", "withdrawal"}
                        and self.quality_enabled is not None and not self.quality_enabled()):
                    message = ParsedMail(
                        message.message_id, message.sender, False, (), (), "unrecognized", reason="quality_identity_disabled",
                    )
                inserted += _store_receipt(
                    conn, mailbox, page.uidvalidity, receipt.uid, receipt.raw, message, fingerprint, self.config,
                )
            conn.execute(
                """
                INSERT INTO mail_cursors (mailbox, uidvalidity, last_uid) VALUES (%s, %s, %s)
                ON CONFLICT (mailbox) DO UPDATE SET uidvalidity = excluded.uidvalidity,
                    last_uid = excluded.last_uid, updated_at = now()
                """,
                (mailbox, page.uidvalidity, receipts[-1].uid if receipts else 0),
            )
        return inserted

    def reconcile_outbound(
        self, outbound_id: UUID, attempt_id: UUID, state: str, *, detail: str, retry: bool = False,
    ) -> dict:
        with connect(self.dsn) as conn:
            outbound = conn.execute(
                "SELECT * FROM mail_outbound WHERE id = %s", (outbound_id,),
            ).fetchone()
            if outbound is None:
                raise MailError(404, "mail_outbound_not_found")
            request = _request_for_update(conn, outbound["request_id"])
            if self.config.hackathon and request["recipient"] != self.config.recipient:
                raise MailError(404, "mail_outbound_not_found")
            outbound = conn.execute("SELECT state FROM mail_outbound WHERE id = %s", (outbound_id,)).fetchone()
            current = conn.execute(
                """
                SELECT a.id, a.state FROM delivery_attempts a
                WHERE a.effect_key = %s AND a.channel = 'smtp' ORDER BY a.started_at DESC, a.id DESC LIMIT 1
                FOR UPDATE
                """,
                (str(outbound_id),),
            ).fetchone()
            if current is None:
                raise MailError(409, "mail_attempt_not_found")
            if current["id"] != attempt_id:
                raise MailError(409, "stale_mail_attempt")
            try:
                result = reconcile_delivery(
                    self.dsn, conn=conn, attempt_id=current["id"], state=state, detail=detail,
                )
            except DeliveryStateConflict:
                raise MailError(409, "mail_attempt_is_not_ambiguous") from None
            if retry and (state != "failed" or request["state"] == "withdrawn" or outbound["state"] == "cancelled"
                          or not _has_access(conn, request) or not self._can_send(request)):
                raise MailError(409, "mail_retry_not_allowed")
            out_state = "queued" if retry else result.state
            conn.execute(
                """
                UPDATE mail_outbound SET state = %s, attempt_id = %s, updated_at = now() WHERE id = %s
                """,
                (out_state, result.attempt_id, outbound_id),
            )
            request_state = "waiting_send" if retry else "waiting_reply" if state == "sent" else "delivery_failed"
            conn.execute(
                """
                UPDATE mail_requests SET state = %s, updated_at = now()
                WHERE id = %s AND state IN ('waiting_send', 'waiting_reply', 'delivery_failed')
                """,
                (request_state, request["id"]),
            )
            if retry:
                _queue_send(conn, outbound_id, job_key=f"{outbound_id}:after:{result.attempt_id}")
        return {"id": outbound_id, "state": out_state, "attempt_id": result.attempt_id}
