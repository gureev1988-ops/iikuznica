"""Durable DION ingress, feed routing and delivery reconciliation."""

from collections.abc import Callable
from html import escape
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from backend.dialog.clarifications import QUESTION, match_options, numbered
from backend.contracts.api import MessageRequest
from backend.db import connect, lock_source_catalog
from backend.dialog import Actor, DialogueError, DialogueService
from backend.runtime import KnownDeliveryFailure, PendingDelivery, enqueue, reconcile_delivery, require_lease, send_once

from .audit import record_dispatch, record_outcome
from .client import MAX_TEXT_LENGTH, MAX_UPDATE_ID, DionClient, DionRejected, as_object, as_uuid, update_id

DELIVER = "dion.deliver"
_PREFLIGHT_FAILURE = "dion_preflight_unavailable"
_APPROVE = {"да", "подтверждаю", "/confirm"}
_DECLINE = {"нет", "отмена", "/decline"}


class DionPreflightUnavailable(RuntimeError):
    """Delivery preparation failed before any provider send was attempted."""


def queue_reply(
    conn,
    *,
    effect_key: str,
    conversation_id: UUID,
    case_id: UUID | None,
    turn_id: UUID | None,
    actor_id: str,
    text: str,
    source_refs: list[dict] | None = None,
    offer_id: UUID | None = None,
    clarification_id: UUID | None = None,
    mail_request_id: UUID | None = None,
    reply_message_id: UUID | None = None,
) -> bool:
    """Stage a reply in the caller's transaction with the original case and principal."""
    row = conn.execute(
        """
        SELECT f.bot_id, f.chat_id
        FROM dion_feeds f JOIN conversations c ON c.id = f.conversation_id
        WHERE f.conversation_id = %s AND c.owner_id = %s
        """,
        (conversation_id, actor_id),
    ).fetchone()
    if row is None:
        return False

    if reply_message_id is None and case_id is not None:
        original = conn.execute(
            """
            SELECT message_id FROM dion_updates
            WHERE bot_id = %s AND chat_id = %s AND case_id = %s AND kind = 'input'
            ORDER BY update_id LIMIT 1
            """,
            (row["bot_id"], row["chat_id"], case_id),
        ).fetchone()
        if original is not None:
            reply_message_id = original["message_id"]

    conn.execute(
        """
        INSERT INTO dion_outbox (
            effect_key, conversation_id, case_id, turn_id, actor_id, text, source_refs,
            reply_message_id, offer_id, clarification_id, mail_request_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
        """,
        (
            effect_key, conversation_id, case_id, turn_id, actor_id, text, Jsonb(source_refs or []),
            reply_message_id, offer_id, clarification_id, mail_request_id,
        ),
    )
    enqueue(conn, queue="background", kind=DELIVER, key=effect_key, payload={"effect_key": effect_key})
    return True


def lock_delivery_receipts(conn, bot_id: UUID) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), 31009)", (str(bot_id),))


def _classify(update: dict[str, Any], bot_id: UUID) -> tuple[str, UUID | None, UUID | None]:
    if "message_failed" in update:
        failure = as_object(update["message_failed"])
        message = as_object(failure.get("message"))
        message_id = as_uuid(
            failure.get("intermediate_id") or failure.get("message_id") or message.get("message_id")
        )
        chat_id = as_uuid(
            failure.get("chat_id") or as_object(failure.get("chat") or message.get("chat")).get("id")
        )
        return ("message_failed" if message_id else "ignored", chat_id, message_id)

    message = as_object(update.get("message"))
    chat = as_object(message.get("chat"))
    sender = as_object(message.get("from"))
    chat_id = as_uuid(chat.get("id"))
    message_id = as_uuid(message.get("message_id"))
    sender_id = as_uuid(sender.get("id"))
    if message.get("message_type") != "regular" or chat.get("type") != "dm":
        return "ignored", chat_id, message_id
    if sender_id == bot_id and sender.get("is_bot") is True and chat_id and message_id:
        return "message", chat_id, as_uuid(message.get("intermediate_id")) or message_id
    if (
        sender.get("is_bot") is False and sender_id and chat_id and message_id
        and isinstance(message.get("text"), str) and message["text"].strip()
    ):
        return "input", chat_id, message_id
    return "ignored", chat_id, message_id


class DionAdapter:
    def __init__(
        self,
        dsn: str | None,
        client: DionClient,
        dialogue: DialogueService,
        resolve_actor: Callable[[str], Actor | None],
        *,
        public_base_url: str | None = None,
    ):
        self.dsn = dsn
        self.client = client
        self.dialogue = dialogue
        self.resolve_actor = resolve_actor
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None

    @property
    def handlers(self):
        return {DELIVER: self.deliver}

    def poll_once(self, *, timeout: int = 30, limit: int = 100) -> int:
        bot_id = self.client.bot_id
        with connect(self.dsn) as conn:
            # A session lock prevents simultaneous polls for the same bot. Commit the
            # lookup before long polling so no SQL transaction stays open during I/O.
            locked = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s), 31008) AS acquired", (str(bot_id),)
            ).fetchone()["acquired"]
            if not locked:
                return 0
            cursor = conn.execute(
                "SELECT last_update_id FROM dion_cursors WHERE bot_id = %s", (bot_id,)
            ).fetchone()
            conn.commit()
            last = cursor["last_update_id"] if cursor else None
            offset = min(last + 1, MAX_UPDATE_ID) if last is not None else None
            updates = self.client.get_updates(offset=offset, limit=limit, timeout=timeout)
            inserted = 0
            for item in sorted(updates, key=lambda item: update_id(item["update_id"])):
                external_id = update_id(item["update_id"])
                if last is not None and external_id <= last:
                    continue
                kind, chat_id, message_id = _classify(item, bot_id)
                # Keep unrelated update IDs for ordering without storing other chats' content.
                payload = Jsonb({} if kind == "ignored" else item)
                inserted += conn.execute(
                    """
                    INSERT INTO dion_updates (
                        bot_id, update_id, kind, chat_id, message_id, payload, processed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)
                    ON CONFLICT DO NOTHING
                    """,
                    (bot_id, external_id, kind, chat_id, message_id, payload, kind == "ignored"),
                ).rowcount
                last = external_id
            if updates:
                conn.execute(
                    """
                    INSERT INTO dion_cursors (bot_id, last_update_id) VALUES (%s, %s)
                    ON CONFLICT (bot_id) DO UPDATE SET last_update_id = EXCLUDED.last_update_id
                    """,
                    (bot_id, last),
                )
            self._reconcile_pending(conn, bot_id)
        return inserted

    def _actor(self, external_id: UUID) -> Actor | None:
        actor = self.resolve_actor(str(external_id))
        if actor is None or "employee" not in actor.roles:
            return None
        return Actor(actor.id, actor.roles, actor.email, channel="dion")

    def route_once(self) -> int:
        """Route one feed's available inputs in order, publishing case jobs on commit."""
        bot_id = self.client.bot_id
        with connect(self.dsn) as conn:
            self._reconcile_pending(conn, bot_id)
            first = conn.execute(
                """
                SELECT u.chat_id FROM dion_updates u
                WHERE u.bot_id = %s AND u.kind = 'input' AND u.processed_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM dion_updates earlier
                      WHERE earlier.bot_id = u.bot_id AND earlier.chat_id = u.chat_id
                        AND earlier.kind = 'input' AND earlier.processed_at IS NULL
                        AND earlier.update_id < u.update_id
                  )
                ORDER BY u.update_id LIMIT 1 FOR UPDATE OF u SKIP LOCKED
                """,
                (bot_id,),
            ).fetchone()
            if first is None:
                return 0
            rows = conn.execute(
                """
                SELECT * FROM dion_updates
                WHERE bot_id = %s AND chat_id = %s AND kind = 'input' AND processed_at IS NULL
                ORDER BY update_id FOR UPDATE
                """,
                (bot_id, first["chat_id"]),
            ).fetchall()
            routed = 0
            for row in rows:
                if self._reply_waiting_for_receipt(conn, row):
                    break
                case_id, turn_id = self._route(conn, row)
                conn.execute(
                    """
                    UPDATE dion_updates SET processed_at = now(), case_id = %s, turn_id = %s
                    WHERE bot_id = %s AND update_id = %s
                    """,
                    (case_id, turn_id, bot_id, row["update_id"]),
                )
                routed += 1
            return routed

    @staticmethod
    def _reply_waiting_for_receipt(conn, row) -> bool:
        target = as_object(row["payload"]["message"].get("reply_to_message"))
        message_id = as_uuid(target.get("message_id"))
        if message_id is None:
            return False
        waiting = conn.execute(
            """
            SELECT 1 FROM dion_updates u
            WHERE u.bot_id = %s AND u.chat_id = %s AND u.kind = 'message'
              AND u.processed_at IS NULL AND u.payload->'message'->>'message_id' = %s
              AND EXISTS (
                  SELECT 1 FROM dion_feeds f
                  JOIN dion_outbox o ON o.conversation_id = f.conversation_id
                  JOIN delivery_attempts a ON a.channel = 'dion' AND a.effect_key = o.effect_key
                  WHERE f.bot_id = u.bot_id AND f.chat_id = u.chat_id AND a.state = 'sending'
                    AND a.expires_at > clock_timestamp()
              )
            """,
            (row["bot_id"], row["chat_id"], str(message_id)),
        ).fetchone()
        return waiting is not None

    def _route(self, conn, row) -> tuple[UUID | None, UUID | None]:
        message = row["payload"]["message"]
        user_id = as_uuid(as_object(message["from"])["id"])
        actor = self._actor(user_id)
        if actor is None:
            self._discard_input(conn, row)
            return None, None
        feed = conn.execute(
            "SELECT * FROM dion_feeds WHERE bot_id = %s AND chat_id = %s",
            (row["bot_id"], row["chat_id"]),
        ).fetchone()
        if feed is None:
            conversation_id = self.dialogue.ensure_conversation(conn, actor, uuid4())
            conn.execute(
                """
                INSERT INTO dion_feeds (bot_id, chat_id, external_user_id, conversation_id)
                VALUES (%s, %s, %s, %s)
                """,
                (row["bot_id"], row["chat_id"], user_id, conversation_id),
            )
        elif feed["external_user_id"] == user_id:
            conversation_id = feed["conversation_id"]
        else:
            self._discard_input(conn, row)
            return None, None

        try:
            with conn.transaction():
                return self._route_message(conn, actor, conversation_id, row)
        except DialogueError as exc:
            if exc.status in (403, 404, 409, 422, 429, 503):
                text = (
                    "Сервис занят. Пожалуйста, отправьте сообщение ещё раз."
                    if exc.status in (429, 503)
                    else "Действие недоступно. Проверьте, к какому сообщению вы отвечаете."
                )
                self._notice(conn, actor, conversation_id, row, exc.details.get("message", text))
                return None, None
            raise

    @staticmethod
    def _discard_input(conn, row):
        conn.execute(
            "UPDATE dion_updates SET payload = '{}' WHERE bot_id = %s AND update_id = %s",
            (row["bot_id"], row["update_id"]),
        )

    def _reply_target(self, conn, conversation_id, row):
        reply_to = as_uuid(as_object(row["payload"]["message"].get("reply_to_message")).get("message_id"))
        if reply_to is None:
            return None
        target = conn.execute(
            """
            SELECT o.case_id,
                   CASE WHEN prep.envelope IS NULL THEN o.offer_id
                        ELSE (prep.envelope->>'offer_id')::uuid END AS offer_id,
                   CASE WHEN prep.envelope IS NULL THEN o.clarification_id
                        ELSE (prep.envelope->>'clarification_id')::uuid END AS clarification_id,
                   o.mail_request_id, o.effect_key, true AS linked
            FROM dion_outbox o
            LEFT JOIN delivery_attempts a
                ON a.effect_key = o.effect_key AND a.channel = 'dion' AND a.state = 'sent'
            LEFT JOIN dion_dispatches prep ON prep.attempt_id = a.id
            WHERE o.conversation_id = %s AND o.confirmed_message_id = %s
            UNION ALL
            SELECT case_id, null::uuid, null::uuid, null::uuid, null::text, true FROM dion_updates
            WHERE bot_id = %s AND chat_id = %s AND message_id = %s AND kind = 'input'
            LIMIT 1
            """,
            (conversation_id, reply_to, row["bot_id"], row["chat_id"], reply_to),
        ).fetchone()
        return target or {
            "case_id": None, "offer_id": None, "clarification_id": None,
            "mail_request_id": None, "effect_key": None, "linked": False,
        }

    def _route_message(self, conn, actor, conversation_id, row):
        text = row["payload"]["message"]["text"].strip()
        command, *words = text.split(maxsplit=1)
        argument = words[0] if words else ""
        command = command.casefold().rstrip(".!?")
        target = self._reply_target(conn, conversation_id, row)
        if command == "/mail" or (target and target["mail_request_id"] is not None):
            return self._mail_followup(conn, actor, conversation_id, row, command, argument, text, target)
        if target is not None and not target["linked"] and not command.startswith("/"):
            self._notice(
                conn, actor, conversation_id, row,
                "Не удалось определить, к какому обращению относится ответ. "
                "Используйте команду из исходного сообщения.",
            )
            return None, None
        replying_to_clarification = target is not None and target["clarification_id"] is not None
        if (
            command in _APPROVE | _DECLINE
            and (command.startswith("/") or not replying_to_clarification)
            and (not argument or command.startswith("/") or as_uuid(argument.strip()) is not None)
        ):
            return self._offer_decision(conn, actor, conversation_id, row, command, argument, target)

        fields = {"text": text}
        implicit = None
        if target is None and not command.startswith("/"):
            actions = self.dialogue.open_actions(conn, actor, conversation_id)
            sent = {r["clarification_id"] for r in conn.execute("""
                SELECT clarification_id FROM dion_outbox WHERE conversation_id=%s
                AND clarification_id IS NOT NULL AND confirmed_message_id IS NOT NULL
                """, (conversation_id,))}
            opened = [item for item in actions["clarifications"] if item["id"] in sent]
            relevant = [item for item in opened
                        if text.isdecimal() or match_options(text, item["options"], implicit=True)]
            if relevant and len(opened) > 1:
                self._notice(conn, actor, conversation_id, row, "Ответьте на нужное сообщение с уточнением.")
                return None, None
            # Only a matching answer can implicitly select a list. With a semantic
            # interpreter the dialogue service can also recognize a free reply or
            # start a separate case, without consuming the previous clarification.
            semantic = getattr(self.dialogue.engine, "interpret_choice", None)
            if len(opened) == 1 and (relevant or (semantic and not QUESTION.search(text))):
                implicit = opened[0]["id"]
        if command == "/answer" or replying_to_clarification or implicit:
            clarification_id = target["clarification_id"] if target else implicit
            if command == "/answer":
                identifier, _, text = argument.strip().partition(" ")
                clarification_id = as_uuid(identifier)
            actions = self.dialogue.open_actions(conn, actor, conversation_id)
            clarification = next(
                (item for item in actions["clarifications"] if item["id"] == clarification_id), None
            )
            if clarification is None or not text.strip():
                self._notice(conn, actor, conversation_id, row, "Ответьте на сообщение с открытым уточнением.")
                return None, None
            fields = {"text": text.strip(), "clarification_id": clarification["id"],
                      "clarification_version": clarification["version"]}
        elif target and target["case_id"] is not None:
            fields["case_id"] = target["case_id"]

        accepted = self.dialogue.admit(
            conn, actor, conversation_id, MessageRequest(client_message_id=f"dion:{row['message_id']}", **fields)
        )
        return accepted.case_id, accepted.turn_id

    def _mail_followup(self, conn, actor, conversation_id, row, command, argument, text, target):
        from backend.mailfaq import MailError, send_followup

        expected_inbound_id = None
        if command == "/mail":
            args = argument.split(maxsplit=1)
            request_id = as_uuid(args[0]) if args else None
            text = args[1] if len(args) > 1 else ""
        else:
            request_id = target["mail_request_id"]
            expected_inbound_id = as_uuid(target["effect_key"].removeprefix("mail:"))
        request = conn.execute(
            """
            SELECT case_id, turn_id, last_inbound_id FROM mail_requests
            WHERE id = %s AND conversation_id = %s AND actor_id = %s FOR UPDATE
            """,
            (request_id, conversation_id, actor.id),
        ).fetchone()
        if (
            request is None or not text.strip()
            or (expected_inbound_id is not None and request["last_inbound_id"] != expected_inbound_id)
        ):
            self._notice(conn, actor, conversation_id, row, "Ответьте на актуальный встречный вопрос.")
            return None, None
        try:
            with conn.transaction():
                send_followup(
                    conn, request_id, actor_id=actor.id, client_id=f"dion:{row['message_id']}", text=text,
                )
        except MailError as exc:
            if exc.status not in (404, 409, 422):
                raise
            self._notice(conn, actor, conversation_id, row, "Ответьте на актуальный встречный вопрос.")
            return None, None
        self._notice(
            conn, actor, conversation_id, row, "Ответ добавлен в почтовую переписку.",
            case_id=request["case_id"], turn_id=request["turn_id"],
        )
        return request["case_id"], request["turn_id"]

    def _offer_decision(self, conn, actor, conversation_id, row, command, argument, target):
        actions = self.dialogue.open_actions(conn, actor, conversation_id)["offers"]
        selected = as_uuid(argument.strip()) if argument else None
        if target is not None and not argument:
            selected = target["offer_id"]
        if selected is not None:
            eligible = [offer for offer in actions if offer["id"] == selected]
        elif not argument and target is None:
            eligible = actions
        else:
            eligible = []
        if len(eligible) != 1:
            if selected is not None:
                self._notice(conn, actor, conversation_id, row, "Указанное предложение уже закрыто или недоступно.")
            elif actions:
                action_command = "/confirm" if command in _APPROVE else "/decline"
                choices = [
                    f"{action_command} {offer['id']} — обращение {offer['case_id']}"
                    for offer in actions[:20]
                ]
                prompt = "Уточните, какое предложение вы имеете в виду. Ответьте на него или отправьте команду:"
                self._notice(conn, actor, conversation_id, row, "\n".join([prompt, *choices]))
            else:
                self._notice(conn, actor, conversation_id, row, "Сейчас нет открытых предложений для подтверждения.")
            return None, None

        offer = eligible[0]
        confirm = command in _APPROVE
        decide = self.dialogue.confirm if confirm else self.dialogue.decline
        decide(conn, actor, offer["id"], offer["version"])
        text = "Предложение подтверждено." if confirm else "Предложение отклонено."
        self._notice(conn, actor, conversation_id, row, text, case_id=offer["case_id"], turn_id=offer["turn_id"])
        return None, None

    def _notice(self, conn, actor, conversation_id, row, text, *, case_id=None, turn_id=None):
        queue_reply(
            conn, effect_key=f"dion-input:{row['bot_id']}:{row['update_id']}",
            conversation_id=conversation_id, case_id=case_id, turn_id=turn_id, actor_id=actor.id,
            text=text, reply_message_id=row["message_id"],
        )

    def deliver(self, job) -> None:
        effect_key = job.payload["effect_key"]
        bot_id = self.client.bot_id
        with connect(self.dsn) as conn:
            outbox = conn.execute(
                """
                SELECT o.*, f.chat_id, f.external_user_id
                FROM dion_outbox o
                JOIN dion_feeds f ON f.conversation_id = o.conversation_id
                WHERE o.effect_key = %s AND f.bot_id = %s
                """,
                (effect_key, bot_id),
            ).fetchone()
        if outbox is None:
            raise LookupError("DION outbox effect does not belong to this bot")

        def send():
            try:
                with connect(self.dsn) as conn:
                    envelope = self._prepare_dispatch(conn, job, outbox)
            except KnownDeliveryFailure:
                raise
            except Exception:
                # No provider I/O has run. Journal the failure as safe to retry.
                raise KnownDeliveryFailure(_PREFLIGHT_FAILURE) from None
            try:
                provider_id = self.client.send_message(
                    chat_id=outbox["chat_id"], text=envelope["text"], reply_to=outbox["reply_message_id"]
                )
            except DionRejected as exc:
                raise KnownDeliveryFailure(str(exc)) from None
            return PendingDelivery(provider_id)

        outcome = send_once(self.dsn, channel="dion", effect_key=effect_key, send=send, job=job)
        if outcome.state in {"failed", "delivery_unknown"}:
            with connect(self.dsn) as conn:
                lock_source_catalog(conn)
                lock_delivery_receipts(conn, bot_id)
                attempt = conn.execute(
                    "SELECT state, provider_id FROM delivery_attempts WHERE id = %s FOR UPDATE",
                    (outcome.attempt_id,),
                ).fetchone()
                if attempt["state"] == outcome.state:
                    record_outcome(
                        conn, outbox, outcome.attempt_id, outcome.state, provider_id=attempt["provider_id"],
                    )
        if outcome.state == "failed" and outcome.detail == _PREFLIGHT_FAILURE:
            raise DionPreflightUnavailable(_PREFLIGHT_FAILURE)
        self.reconcile_pending(bot_id)

    def _prepare_dispatch(self, conn, job, outbox) -> dict:
        require_lease(conn, job)
        lock_source_catalog(conn)
        attempt = conn.execute(
            """
            SELECT id FROM delivery_attempts
            WHERE channel = 'dion' AND state = 'sending'
              AND effect_key = %s AND job_id = %s AND lease_token = %s
            FOR UPDATE
            """,
            (outbox["effect_key"], job.id, job.lease_token),
        ).fetchone()
        if attempt is None:
            raise DionPreflightUnavailable("DION sending attempt lost before provider I/O")
        envelope = self._delivery_text(conn, outbox)
        record_dispatch(conn, outbox, attempt["id"], envelope)
        return envelope

    def _delivery_text(self, conn, outbox) -> dict:
        actor = self._actor(outbox["external_user_id"])
        if actor is None or actor.id != outbox["actor_id"]:
            raise KnownDeliveryFailure("dion_destination_not_authorized")
        # Preparation may supersede an action or append a case event.
        # Take the write lock up front to avoid concurrent share-to-write upgrades.
        lock_source_catalog(conn)
        conversation = conn.execute(
            "SELECT 1 FROM conversations WHERE id = %s AND owner_id = %s FOR UPDATE",
            (outbox["conversation_id"], actor.id),
        ).fetchone()
        if conversation is None:
            raise KnownDeliveryFailure("dion_destination_not_authorized")
        try:
            refs = self._delivery_refs(conn, outbox)
            reevaluate_faq = outbox["effect_key"].startswith("turn:")
            payload = self.dialogue.delivery_envelope(
                conn, actor, outbox["conversation_id"], outbox["text"], refs,
                turn_id=outbox["turn_id"], reevaluate_faq=reevaluate_faq,
            )
        except DialogueError as exc:
            if exc.status in (403, 404):
                raise KnownDeliveryFailure("dion_destination_not_authorized") from None
            raise
        if payload is None:
            raise KnownDeliveryFailure("dion_source_no_longer_available")
        text, revised_refs, keep_actions, condition_sources = payload
        footnote_refs = revised_refs if reevaluate_faq else outbox["source_refs"]
        offer_id = outbox["offer_id"] if keep_actions else None
        clarification_id = outbox["clarification_id"] if keep_actions else None
        substitute = reevaluate_faq and (
            text != outbox["text"] or footnote_refs != outbox["source_refs"]
            or (not keep_actions and bool(outbox["offer_id"] or outbox["clarification_id"]))
        )

        footer = []
        if offer_id or clarification_id:
            actions = self.dialogue.open_actions(conn, actor, outbox["conversation_id"])
            if offer_id:
                if not any(offer["id"] == offer_id for offer in actions["offers"]):
                    raise KnownDeliveryFailure("dion_offer_no_longer_open")
                footer.append(f"Ответьте «да» или «нет» на это сообщение. Можно отправить /confirm {offer_id}")
            if clarification_id:
                clarification = next(
                    (item for item in actions["clarifications"] if item["id"] == clarification_id), None
                )
                if clarification is None:
                    raise KnownDeliveryFailure("dion_clarification_no_longer_open")
                if text == clarification["prompt"]:
                    text = numbered(text, clarification["options"])
                footer.append(
                    f"Ответьте на это сообщение номером или текстом. Можно отправить /answer {clarification_id} и ответ."
                )

        if outbox["mail_request_id"]:
            request = conn.execute(
                """
                SELECT 1 FROM mail_requests
                WHERE id = %s AND conversation_id = %s AND actor_id = %s
                    AND state IN ('counter_question', 'partial') AND last_inbound_id = %s
                """,
                (
                    outbox["mail_request_id"], outbox["conversation_id"], actor.id,
                    as_uuid(outbox["effect_key"].removeprefix("mail:")),
                ),
            ).fetchone()
            if request is None:
                raise KnownDeliveryFailure("dion_mail_followup_no_longer_open")
            footer.append(
                f"Ответьте на это сообщение или отправьте /mail {outbox['mail_request_id']} и ваш ответ."
            )

        if outbox["case_id"]:
            case_link = f"Обращение {outbox['case_id']}"
            if self.public_base_url and outbox["turn_id"]:
                case_link += f"\n{self.public_base_url}/v1/turns/{outbox['turn_id']}"
            footer.append(case_link)
        if footnote_refs:
            links = [
                ref["url"] for ref in footnote_refs if ref.get("url", "").startswith("/v1/sources/")
            ]
            for url in dict.fromkeys(links[:10]):
                footer.append(f"{self.public_base_url or ''}{url}")
        suffix = _clip("\n\n" + "\n".join(footer), 3000) if footer else ""
        for url in dict.fromkeys(ref.get("related_url") for ref in footnote_refs or ()):
            if url and url.startswith("https://"):
                line = f"\nСвязанный документ: {url}"
                if _html_length(suffix) + _html_length(line) <= 3000:
                    suffix += line
        return {
            "text": _clip(text, MAX_TEXT_LENGTH - _html_length(suffix)) + suffix,
            "source_refs": footnote_refs,
            "condition_sources": condition_sources,
            "substitute": substitute,
            "offer_id": str(offer_id) if offer_id else None,
            "clarification_id": str(clarification_id) if clarification_id else None,
            "mail_request_id": str(outbox["mail_request_id"]) if outbox["mail_request_id"] else None,
            "reply_message_id": str(outbox["reply_message_id"]) if outbox["reply_message_id"] else None,
        }

    @staticmethod
    def _delivery_refs(conn, outbox):
        from backend.documents import get_fragment
        from backend.mailfaq import current_faq_notification

        refs = outbox["source_refs"]
        if not refs or not outbox["effect_key"].startswith("mail:"):
            return refs
        # A published FAQ can be covered by a current public document. The original
        # recipient still receives the answer after checking the original citation.
        lock_source_catalog(conn)
        enforce_current = []
        for ref in refs:
            revision_id = as_uuid(ref.get("revision_id"))
            fragment_id = as_uuid(ref.get("fragment_id"))
            if (
                ref.get("kind") == "faq" and revision_id and fragment_id
                and current_faq_notification(conn, revision_id, fragment_id)
            ):
                fragment = get_fragment(conn, revision_id, fragment_id, outbox["actor_id"])
                quote = ref.get("quote")
                if (
                    fragment is not None and fragment.kind == "faq"
                    and fragment.document_id == as_uuid(ref.get("document_id"))
                    and isinstance(quote, str) and quote.strip() and quote in fragment.text
                ):
                    continue
            enforce_current.append(ref)
        return enforce_current

    def reconcile_pending(self, bot_id: UUID | None = None) -> int:
        if bot_id is None:
            bot_id = self.client.bot_id
        with connect(self.dsn) as conn:
            return self._reconcile_pending(conn, bot_id)

    def _reconcile_pending(self, conn, bot_id: UUID) -> int:
        # One reconciliation per bot at a time, with the cursor batch or the
        # inbound routing transaction. This lock never spans provider I/O.
        lock_source_catalog(conn)
        lock_delivery_receipts(conn, bot_id)
        rows = conn.execute(
            """
            SELECT u.update_id, u.kind, u.payload, a.id AS attempt_id, o.effect_key,
                   o.conversation_id, o.case_id, o.turn_id, a.provider_id
            FROM delivery_attempts a
            JOIN dion_outbox o ON o.effect_key = a.effect_key
            JOIN dion_feeds f ON f.conversation_id = o.conversation_id
            JOIN dion_updates u ON u.bot_id = f.bot_id AND u.message_id::text = a.provider_id
              AND (u.chat_id = f.chat_id OR (u.kind = 'message_failed' AND u.chat_id IS NULL))
            WHERE a.channel = 'dion' AND a.state = 'delivery_unknown' AND f.bot_id = %s
              AND u.processed_at IS NULL AND u.kind IN ('message', 'message_failed')
            ORDER BY u.update_id FOR UPDATE OF a, u
            """,
            (bot_id,),
        ).fetchall()
        resolved = set()
        for row in rows:
            if row["attempt_id"] not in resolved:
                state = "sent" if row["kind"] == "message" else "failed"
                reconcile_delivery(
                    self.dsn, attempt_id=row["attempt_id"], state=state,
                    detail="dion_message_failed" if state == "failed" else None, conn=conn,
                )
                message_id = None
                if state == "sent":
                    message_id = as_uuid(row["payload"]["message"]["message_id"])
                    conn.execute(
                        "UPDATE dion_outbox SET confirmed_message_id = %s WHERE effect_key = %s",
                        (message_id, row["effect_key"]),
                    )
                record_outcome(
                    conn, row, row["attempt_id"], state,
                    provider_id=row["provider_id"], message_id=message_id,
                )
                resolved.add(row["attempt_id"])
            conn.execute(
                "UPDATE dion_updates SET processed_at = now() WHERE bot_id = %s AND update_id = %s",
                (bot_id, row["update_id"]),
            )
        return len(resolved)


def _html_length(text: str) -> int:
    return len(escape(text))


def _clip(text: str, limit: int) -> str:
    """Keep the escaped Bot API request within the provider's size limit."""
    if _html_length(text) <= limit:
        return text
    size = 1  # Reserve the final ellipsis.
    for index, character in enumerate(text):
        size += _html_length(character)
        if size > limit:
            return text[:index] + "…"
    return text
