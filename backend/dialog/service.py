"""Transactional conversation commands and answer execution."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from backend.assembly.fast_answers import VerifiedAnswer, potential_faq
from backend.contracts.api import AcceptedMessage, MessageRequest
from backend.contracts.engine import (
    AnswerEngine,
    AnswerRequest,
    AnswerResult,
    HistoryMessage,
    ModelAttemptFailure,
    ModelAttemptResult,
    SourceSnapshot,
)
from backend.db import append_event, connect, lock_source_catalog
from backend.documents.links import related_document_url
from .clarifications import QUESTION, UNKNOWN, match_options, numbered, offer_decision


@dataclass(frozen=True)
class Actor:
    id: str
    roles: frozenset[str]
    email: str | None = None
    channel: str = "web"


class DialogueError(Exception):
    def __init__(self, status: int, code: str, **details):
        super().__init__(code)
        self.status = status
        self.code = code
        self.details = details


class EvidenceChanged(Exception):
    """A once applicable source changed after the answer snapshot was taken."""


class InvalidAnswer(Exception):
    """The engine returned an answer the application cannot verify."""


@dataclass(frozen=True)
class _AttemptRecorder:
    dsn: str | None
    job: object
    turn_id: UUID
    case_id: UUID

    def call(self, invoke, *, role: str, model: str, route: str, trace_id: str | None = None):
        from backend.runtime import ModelCallFailure, ModelResult, call_model

        def physical_call():
            try:
                result = invoke()
            except ModelAttemptFailure as error:
                usage = error.result
                raise ModelCallFailure(
                    str(error), input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                    cost=usage.cost, route=usage.route, model=usage.model,
                ) from error
            if isinstance(result, ModelAttemptResult):
                return ModelResult(
                    result.value, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                    cost=result.cost, route=result.route, model=result.model,
                )
            return result

        try:
            return call_model(
                self.dsn, physical_call, role=role, trace_id=trace_id or str(uuid4()), model=model,
                route=route, turn_id=self.turn_id, case_id=self.case_id, job=self.job,
            )
        except ModelCallFailure as error:
            if isinstance(error.__cause__, ModelAttemptFailure):
                raise error.__cause__ from None
            raise


def _employee(actor: Actor) -> None:
    if "employee" not in actor.roles:
        raise DialogueError(403, "employee_role_required")


def _changed_reply_offer():
    return DialogueError(409, "reply_offer_changed", message=(
        "Предложение изменилось. Проверьте последнюю реплику бота и ответьте на неё."
    ))


def _conversation(conn, actor: Actor, conversation_id: UUID, *, write: bool = False):
    _employee(actor)
    lock = "FOR UPDATE" if write else "FOR SHARE"
    row = conn.execute(
        f"SELECT * FROM conversations WHERE id = %s AND owner_id = %s {lock}",
        (conversation_id, actor.id),
    ).fetchone()
    if row is None:
        raise DialogueError(404, "conversation_not_found")
    return row


def _turn(conn, actor: Actor, turn_id: UUID, *, write: bool = False):
    _employee(actor)
    found = conn.execute(
        "SELECT conversation_id FROM turns WHERE id = %s", (turn_id,)
    ).fetchone()
    if found is None:
        raise DialogueError(404, "turn_not_found")
    conversation = _conversation(conn, actor, found["conversation_id"], write=write)
    turn = conn.execute("SELECT * FROM turns WHERE id = %s", (turn_id,)).fetchone()
    return conversation, turn


def _accepted(turn) -> AcceptedMessage:
    return AcceptedMessage(
        conversation_id=turn["conversation_id"], case_id=turn["case_id"], turn_id=turn["id"]
    )


def _topic_turns(conn, case_id, turn_id):
    """Bound model and mail history after an employee switches subjects inside a case."""
    rows = conn.execute("""
        WITH topics AS (
            SELECT id, count(*) FILTER (WHERE request @> '{"starts_topic": true}')
                OVER (ORDER BY created_at,id) AS topic,
                bool_or(request @> '{"starts_topic": true}') OVER () AS has_boundary
            FROM turns WHERE case_id=%s
        )
        SELECT id FROM topics WHERE has_boundary AND topic=(SELECT topic FROM topics WHERE id=%s)
        """, (case_id, turn_id)).fetchall()
    return [row["id"] for row in rows] or None


def _action(row: dict) -> dict:
    fields = ("id", "turn_id", "case_id", "version", "status", "prompt", "options", "text")
    return {key: row[key] for key in fields if key in row}


def _public_action(row: dict) -> dict:
    return {
        key: str(value) if isinstance(value, UUID) else value
        for key, value in _action(row).items()
    }


def _event(conn, turn: dict, kind: str, payload: dict) -> int:
    return append_event(conn, turn["conversation_id"], turn["case_id"], turn["id"], kind, payload)


def _source_ref(fragment, quote: str, claim: str | None = None) -> dict:
    reference = {
        "document_id": str(fragment.document_id),
        "revision_id": str(fragment.revision_id),
        "fragment_id": str(fragment.fragment_id),
        "kind": fragment.kind,
        "quote": quote,
        "url": f"/v1/sources/{fragment.revision_id}/fragments/{fragment.fragment_id}",
    }
    if claim is not None:
        reference["claim"] = claim
    return reference


def _fragment_key(fragment) -> tuple[UUID, UUID, UUID]:
    return fragment.document_id, fragment.revision_id, fragment.fragment_id


def _reference_key(reference: dict) -> tuple[UUID, UUID, UUID]:
    return tuple(UUID(str(reference[key])) for key in ("document_id", "revision_id", "fragment_id"))


class DialogueService:
    def __init__(
        self,
        *,
        dsn: str | None = None,
        engine: AnswerEngine,
        mail_config=None,
        max_pending: int | None = 128,
        deadline_seconds: float = 60,
        queue_reply: Callable | None = None,
        resolve_actor: Callable[[str, str], Actor | None] | None = None,
        hackathon: bool = False,
    ):
        self.dsn = dsn
        self.engine = engine
        self.mail_config = mail_config
        self.max_pending = max_pending
        self.deadline_seconds = deadline_seconds
        self.queue_reply = queue_reply
        self.resolve_actor = resolve_actor
        self.hackathon = hackathon

    def ensure_conversation(self, conn, actor: Actor, conversation_id: UUID) -> UUID:
        _employee(actor)
        lock_source_catalog(conn)
        conn.execute(
            "INSERT INTO conversations (id, owner_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (conversation_id, actor.id),
        )
        _conversation(conn, actor, conversation_id, write=True)
        return conversation_id

    def admit(
        self, conn, actor: Actor, conversation_id: UUID, body: MessageRequest, *, offer_id: UUID | None = None
    ) -> AcceptedMessage:
        with conn.transaction():
            return self._admit(conn, actor, conversation_id, body, offer_id=offer_id)

    def _admit(self, conn, actor: Actor, conversation_id: UUID, body: MessageRequest, *, offer_id=None) -> AcceptedMessage:
        self.ensure_conversation(conn, actor, conversation_id)
        bound = "reply_to_offer" in body.model_fields_set
        request = body.model_dump(mode="json", exclude=set() if bound else {"reply_to_offer"})
        existing = conn.execute(
            "SELECT * FROM turns WHERE conversation_id = %s AND request_id = %s",
            (conversation_id, body.client_message_id),
        ).fetchone()
        if existing:
            if existing["request"].get("message") != request:
                raise DialogueError(409, "client_message_id_reused")
            return _accepted(existing)

        case_id = body.case_id
        shown = body.reply_to_offer
        if shown and (case_id is None or body.clarification_id is not None):
            raise DialogueError(422, "offer_case_required")
        text = body.text.strip() if body.text is not None else ""
        message_sources = None
        decision = offer_decision(text)
        offer = None
        if body.clarification_id is None and text.isdecimal():
            raise DialogueError(422, "question_words_required", message="Напишите вопрос словами.")
        if body.clarification_id is not None:
            case_id, question, text, message_sources = self._resolve_clarification(conn, actor, conversation_id, body, text)
        else:
            if body.option_id is not None or body.clarification_version is not None:
                raise DialogueError(422, "clarification_id_required")
            if not text:
                raise DialogueError(422, "message_text_required")
            question = text
        if case_id is None:
            case_id = conn.execute(
                "INSERT INTO cases (conversation_id) VALUES (%s) RETURNING id", (conversation_id,)
            ).fetchone()["id"]
        elif body.clarification_id is None:
            try:
                self._require_case_ready(conn, actor, conversation_id, case_id)
            except DialogueError as exc:
                if shown and exc.code == "clarification_id_required":
                    raise _changed_reply_offer() from exc
                raise
            if (actor.channel != "dion" or offer_id is not None) and (
                    decision or callable(getattr(self.engine, "interpret_offer", None)) or offer_id or bound):
                offer = conn.execute("""
                    SELECT o.*, t.result AS original_result FROM offers o JOIN turns t ON t.id=o.turn_id
                    WHERE o.case_id=%s AND o.status='open'
                    """, (case_id,)).fetchone()
            if bound and offer:
                conditions, _ = self._conditions(conn, actor, case_id)
                if conditions != offer["payload"]["conditions"] or not self._result_visible(
                        conn, actor, offer["original_result"], current=True):
                    offer = None
            if bound:
                expected = (shown.id, shown.version) if shown else None
                actual = (offer["id"], offer["version"]) if offer else None
                changed = expected != actual
            else:
                # A legacy client can refer implicitly to the first offer in a case. After replacement
                # it must target the visible offer explicitly, as its old draft could refer to either.
                changed = bool(offer and not offer_id and conn.execute(
                    "SELECT 1 FROM offers WHERE case_id=%s AND id<>%s LIMIT 1", (case_id, offer["id"]),
                ).fetchone())
            if changed:
                raise _changed_reply_offer()
        if offer_id is not None and (not offer or offer["id"] != offer_id):
            raise DialogueError(409, "offer_is_closed")
        pending = {"id": str(offer["id"]), "version": offer["version"]} if offer and not decision else None

        if not offer and (body.case_id is not None or body.clarification_id is not None):
            closed = conn.execute("""
                UPDATE offers SET status = 'superseded', updated_at = now()
                WHERE case_id = %s AND status = 'open' RETURNING *
                """, (case_id,)).fetchall()
            for superseded in closed:
                append_event(conn, conversation_id, case_id, superseded["turn_id"], "offer.superseded",
                             {"offer": _public_action(superseded)})
        turn = conn.execute(
            """
            INSERT INTO turns (conversation_id, case_id, request_id, question, request, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp()) RETURNING *
            """,
            (
                conversation_id, case_id, body.client_message_id, question,
                Jsonb({"message": request, "channel": actor.channel, **({"reply_offer": pending} if pending else {})}),
            ),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO messages (conversation_id, case_id, turn_id, client_message_id, role, text, source_refs, created_at)
            VALUES (%s, %s, %s, %s, 'user', %s, %s, clock_timestamp())
            """,
            (
                conversation_id, case_id, turn["id"], body.client_message_id, text,
                Jsonb(message_sources) if message_sources is not None else None,
            ),
        )
        if offer and decision:
            receipt = self._offer_receipt(conn, actor, offer["id"], offer["version"], decision)
            self._publish_offer_reply(conn, actor, turn, receipt)
            return _accepted(turn)
        self._enqueue(conn, actor, turn)
        _event(conn, turn, "turn.accepted", {"status": turn["status"], "attempt": turn["attempt"]})
        return _accepted(turn)

    def _offer_receipt(self, conn, actor, offer_id, version, decision):
        self._decide_offer(conn, actor, offer_id, version, decision, consent_in_history=True)
        result = {"action": "answer", "sources": [], "mail_decision": decision, "text": "Письмо не отправлено."}
        if decision == "confirmed":
            mail = conn.execute("SELECT id, case_number FROM mail_requests WHERE offer_id=%s", (offer_id,)).fetchone()
            result.update(mail_request_id=str(mail["id"]), case_number=mail["case_number"],
                          text=f"Обращение ОК{mail['case_number']} принято для отправки в Отдел качества.")
        return result

    def _publish_offer_reply(self, conn, actor, turn, result, *, remains_open=None):
        conn.execute("UPDATE turns SET status='completed',result=%s,updated_at=now() WHERE id=%s",
                     (Jsonb(result), turn["id"]))
        conn.execute("""INSERT INTO messages(conversation_id,case_id,turn_id,role,text,created_at)
                        VALUES (%s,%s,%s,'assistant',%s,clock_timestamp())""",
                     (turn["conversation_id"], turn["case_id"], turn["id"], result["text"]))
        _event(conn, turn, "turn.completed", {"status": "completed", "result": self._public_result(result)})
        if self.queue_reply is not None and actor.channel == "dion":
            self.queue_reply(conn, effect_key=f"offer-reply:{turn['id']}", conversation_id=turn["conversation_id"],
                             case_id=turn["case_id"], turn_id=turn["id"], actor_id=actor.id, text=result["text"],
                             source_refs=result.get("condition_sources", []), offer_id=remains_open)

    def _require_case_ready(self, conn, actor: Actor, conversation_id: UUID, case_id: UUID,
                            *, clarification_id: str | None = None) -> None:
        case = conn.execute(
            "SELECT id FROM cases WHERE id = %s AND conversation_id = %s",
            (case_id, conversation_id),
        ).fetchone()
        if case is None:
            raise DialogueError(404, "case_not_found")
        pending = conn.execute(
            "SELECT 1 FROM turns WHERE case_id = %s AND status IN ('queued', 'processing')",
            (case_id,),
        ).fetchone()
        if pending is not None:
            raise DialogueError(409, "case_has_pending_turn")
        for clarification in conn.execute(
            """
            SELECT c.*, t.result FROM clarifications c JOIN turns t ON t.id = c.turn_id
            WHERE c.case_id = %s AND c.status = 'open'
            """,
            (case_id,),
        ):
            if self._result_visible(conn, actor, clarification["result"], current=True):
                if str(clarification["id"]) == clarification_id:
                    continue
                raise DialogueError(409, "clarification_id_required")
            self._supersede_clarification(conn, conversation_id, clarification)

    @staticmethod
    def _supersede_clarification(conn, conversation_id, row):
        conn.execute("UPDATE clarifications SET status = 'superseded', updated_at = now() WHERE id = %s", (row["id"],))
        append_event(conn, conversation_id, row["case_id"], row["turn_id"], "clarification.superseded", {
            "clarification_id": str(row["id"]), "version": row["version"], "status": "superseded",
        })

    def _resolve_clarification(self, conn, actor, conversation_id, body, text):
        row = conn.execute(
            """
            SELECT c.*, t.question, t.result, cases.conditions, cases.condition_sources FROM clarifications c
            JOIN turns t ON t.id = c.turn_id JOIN cases ON cases.id = c.case_id
            WHERE t.conversation_id = %s AND c.id = %s
            """,
            (conversation_id, body.clarification_id),
        ).fetchone()
        if row is None or body.case_id not in (None, row["case_id"]):
            raise DialogueError(404, "clarification_not_found")
        if row["status"] != "open" or body.clarification_version not in (None, row["version"]):
            raise DialogueError(409, "clarification_changed")
        if bool(body.option_id) == bool(text):
            raise DialogueError(422, "select_option_or_enter_text")
        if not self._result_visible(conn, actor, row["result"], current=True):
            self._supersede_clarification(conn, conversation_id, row)
            return row["case_id"], row["question"], text or row["question"], None
        if conn.execute("SELECT 1 FROM turns WHERE case_id=%s AND status IN ('queued','processing')",
                        (row["case_id"],)).fetchone():
            raise DialogueError(409, "case_has_pending_turn")
        conditions = dict(row["conditions"])
        condition_sources = dict(row["condition_sources"])
        key = row["clarification_key"] or f"clarification:{row['id']}"
        message_sources = None
        matches = match_options(text, row["options"]) if text else None
        additions = {}
        route = None
        if (text and not text.isdecimal() and getattr(self.engine, "interpret_choice", None)
                and not any(text.strip().casefold() == o["text"].casefold() for o in row["options"])):
            from backend.contracts.engine import EngineUnavailable
            try:
                visible_conditions, _ = self._conditions(conn, actor, row["case_id"])
                route, option_id, additions = self.engine.interpret_choice(
                    {**row, "conditions": visible_conditions}, text,
                    _AttemptRecorder(self.dsn, None, row["turn_id"], row["case_id"]))
            except EngineUnavailable:
                route = "ambiguous"
            if route == "new_question":
                return None, text, text, None
            if route == "about_choices":
                # A question about the list is not a choice. Keep it available for the next reply.
                return row["case_id"], text, text, self._result_refs(row["result"])
            if route == "ambiguous":
                raise DialogueError(422, "clarification_choice_ambiguous",
                                    message=numbered(row["prompt"], row["options"]))
            if route == "selected":
                matches = [o for o in row["options"] if o["id"] == option_id]
            elif route == "unknown":
                matches = [o for o in row["options"] if o["id"] == "unknown"] or None
            elif route == "free":
                matches = None
        if matches is not None and len(matches) != 1:
            issue = "invalid" if not matches else "ambiguous"
            hint = (f"Выберите номер от 1 до {len(row['options'])}." if not matches
                    else "Подходят несколько вариантов. Уточните выбор.")
            raise DialogueError(422, f"clarification_choice_{issue}", message=numbered(hint, row["options"]))
        if matches is None and text and (QUESTION.search(text) or text.endswith("?")) and route is None:
            # Preserve the open list in its original case when the employee changes the subject.
            return None, text, text, None
        if body.option_id is not None or matches:
            selected_id = body.option_id or matches[0]["id"]
            option = next((option for option in row["options"] if option["id"] == selected_id), None)
            if option is None:
                raise DialogueError(422, "clarification_option_not_found")
            text = text or option["text"]
            selected = option.get("conditions") or {key: option["text"]}
            message_sources = self._result_refs(row["result"])
            value = option["text"]
            conditions.update(selected)
            condition_sources.update(dict.fromkeys(selected, message_sources))
        else:
            value = "не знаю" if route == "unknown" or (route is None and UNKNOWN.search(text)) else text
            conditions[key] = value
            condition_sources.pop(key, None)
        for fact, literal in additions.items():
            if fact != key:
                conditions[fact] = literal
                condition_sources.pop(fact, None)
        conn.execute(
            "UPDATE cases SET conditions = %s, condition_sources = %s, updated_at = now() WHERE id = %s",
            (Jsonb(conditions), Jsonb(condition_sources), row["case_id"]),
        )
        conn.execute(
            "UPDATE clarifications SET status = 'resolved', resolved_value=%s, updated_at = now() WHERE id = %s",
            (value, row["id"]),
        )
        row["status"] = "resolved"
        append_event(
            conn, conversation_id, row["case_id"], row["turn_id"],
            "clarification.resolved", {"clarification": _public_action(row)},
        )
        return row["case_id"], row["question"], text, message_sources

    def _enqueue(self, conn, actor, turn) -> None:
        from backend.runtime import QueueFull, enqueue

        try:
            enqueue(
                conn, queue="interactive", kind="answer_turn",
                key=f"{turn['id']}:{turn['attempt']}",
                payload={
                    "turn_id": str(turn["id"]), "attempt": turn["attempt"],
                    "actor_id": actor.id, "channel": actor.channel,
                },
                deadline_at=datetime.now(UTC) + timedelta(seconds=self.deadline_seconds),
                max_pending=self.max_pending,
            )
        except QueueFull as exc:
            raise DialogueError(503, "interactive_queue_full") from exc

    def retry(self, conn, actor: Actor, turn_id: UUID, attempt: int | None = None) -> AcceptedMessage:
        with conn.transaction():
            return self._retry(conn, actor, turn_id, attempt)

    def _retry(self, conn, actor: Actor, turn_id: UUID, attempt: int | None) -> AcceptedMessage:
        lock_source_catalog(conn)
        _, turn = _turn(conn, actor, turn_id, write=True)
        if attempt is not None and attempt < turn["attempt"]:
            return _accepted(turn)
        if attempt is not None and attempt > turn["attempt"]:
            raise DialogueError(409, "turn_attempt_changed")
        if attempt is None and turn["attempt"] > 1:
            return _accepted(turn)
        if turn["status"] != "technical_failure":
            raise DialogueError(409, "turn_is_not_technically_failed")
        self._require_case_ready(conn, actor, turn["conversation_id"], turn["case_id"],
                                 clarification_id=turn["request"].get("message", {}).get("clarification_id"))
        later_turn = conn.execute(
            "SELECT 1 FROM turns WHERE case_id = %s AND (created_at, id) > (%s, %s)",
            (turn["case_id"], turn["created_at"], turn["id"]),
        ).fetchone()
        if later_turn is not None:
            raise DialogueError(409, "turn_has_been_superseded")
        turn = conn.execute(
            """
            UPDATE turns SET status = 'queued', error = NULL, attempt = attempt + 1, updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (turn_id,),
        ).fetchone()
        # Retrying through the web preserves the original delivery channel.
        original_actor = Actor(actor.id, actor.roles, actor.email, turn["request"].get("channel", actor.channel))
        self._enqueue(conn, original_actor, turn)
        _event(conn, turn, "turn.retried", {"status": turn["status"], "attempt": turn["attempt"]})
        return _accepted(turn)

    def confirm(self, conn, actor: Actor, offer_id: UUID, version: int) -> dict:
        with conn.transaction():
            return self._decide_offer(conn, actor, offer_id, version, "confirmed")

    def decline(self, conn, actor: Actor, offer_id: UUID, version: int) -> dict:
        with conn.transaction():
            return self._decide_offer(conn, actor, offer_id, version, "declined")

    def _decide_offer(self, conn, actor, offer_id, version, decision, *, consent_in_history=False):
        _employee(actor)
        lock_source_catalog(conn)
        found = conn.execute("SELECT turn_id FROM offers WHERE id = %s", (offer_id,)).fetchone()
        if found is None:
            raise DialogueError(404, "offer_not_found")
        _, turn = _turn(conn, actor, found["turn_id"], write=True)
        offer = conn.execute("SELECT * FROM offers WHERE id = %s", (offer_id,)).fetchone()
        if offer["version"] != version:
            raise DialogueError(409, "offer_changed")
        if offer["status"] == decision:
            return self._offer_response(conn, actor, offer, turn)
        if offer["status"] != "open":
            raise DialogueError(409, "offer_is_closed")
        if decision == "confirmed":
            conditions, _ = self._conditions(conn, actor, turn["case_id"])
            if (
                conditions != offer["payload"]["conditions"]
                or not self._result_visible(conn, actor, turn["result"], current=True)
            ):
                raise DialogueError(409, "offer_evidence_changed")
            if self.mail_config is None:
                raise DialogueError(503, "mail_unavailable")
            if not actor.email:
                raise DialogueError(422, "employee_email_required")
        offer = conn.execute(
            "UPDATE offers SET status = %s, updated_at = now() WHERE id = %s RETURNING *",
            (decision, offer_id),
        ).fetchone()
        if decision == "confirmed":
            from backend.mailfaq import MailError, create_request

            if not consent_in_history:
                conn.execute("""INSERT INTO messages(conversation_id,case_id,turn_id,role,text,created_at)
                                VALUES (%s,%s,%s,'user',%s,clock_timestamp())""",
                             (turn["conversation_id"], turn["case_id"], turn["id"], "Подтверждаю отправку обращения."))
            question, public_conditions, transcript, result = self._mail_context(
                conn, actor, turn["conversation_id"], offer, turn["result"], turn["question"])
            try:
                create_request(
                    conn, self.mail_config, offer_id=offer_id,
                    conversation_id=turn["conversation_id"], case_id=turn["case_id"], turn_id=turn["id"],
                    actor_id=actor.id, actor_email=actor.email, channel=turn["request"].get("channel", actor.channel),
                    question=question, conditions=public_conditions, dialogue=transcript, result=result,
                )
            except MailError as exc:
                raise DialogueError(exc.status, exc.code) from exc
        _event(conn, turn, f"offer.{decision}", {"offer": _public_action(offer)})
        return self._offer_response(conn, actor, offer, turn)

    def _mail_context(self, conn, actor, conversation_id, offer, result, original_question):
        history = self.history(conn, actor, conversation_id)["messages"]
        topic = _topic_turns(conn, offer["case_id"], offer["turn_id"])
        if topic:
            turn_ids = set(topic)
            history = [message for message in history if message["turn_id"] in turn_ids]
        question = offer["payload"].get("question") or original_question
        conditions = offer["payload"]["conditions"]
        if actor.channel == "guest":
            from backend.mailfaq.shared_context import shared_context
            return shared_context(conn, offer["case_id"], offer["turn_id"], history, question, conditions)
        return question, conditions, history, result

    def _offer_response(self, conn, actor, offer, turn):
        response = _action(offer)
        if not self._result_visible(conn, actor, turn["result"], current=True):
            response["text"] = ""
        return response

    def _actor_for_job(self, job) -> Actor:
        actor_id, channel = job.payload["actor_id"], job.payload["channel"]
        if self.resolve_actor is None:
            return Actor(actor_id, frozenset({"employee"}), channel=channel)
        actor = self.resolve_actor(actor_id, channel)
        if actor is None or actor.id != actor_id or actor.channel != channel:
            raise DialogueError(403, "employee_role_required")
        _employee(actor)
        return actor

    def answer_job(self, job) -> None:
        """Interactive queue handler. Engine I/O runs outside of database transactions."""
        actor = self._actor_for_job(job)
        context = self._answer_context(job, actor)
        if context is None:
            return
        if context.pending_offer is not None:
            offer = context.pending_offer
            interpreter = getattr(self.engine, "interpret_offer", None)
            intent = "unavailable"
            if offer:
                intent = interpreter(context.question, offer=offer, context=context)["intent"] if interpreter else "clarify"
            if self._apply_offer_reply(job, context, intent):
                return
            context = self._answer_context(job, actor)
            if context is None:
                return
        result = self.engine.answer(context)
        self._finish_answer(job, context, result)

    def _visible_offer(self, conn, actor, turn, conditions):
        binding = turn["request"].get("reply_offer")
        if not binding:
            return None
        origin = conn.execute("""
            SELECT o.*, t.result, t.question AS original_question FROM offers o JOIN turns t ON t.id=o.turn_id
            WHERE o.id=%s AND o.version=%s AND o.case_id=%s AND o.status='open'
            """, (binding["id"], binding["version"], turn["case_id"])).fetchone()
        if (origin and conditions == origin["payload"]["conditions"]
                and self._result_visible(conn, actor, origin["result"], current=True)):
            return origin
        return None

    def _apply_offer_reply(self, job, request, intent):
        """Revalidate the exact offer after interpretation; a worker retry cannot repeat the effect."""
        from backend.runtime import finish, require_lease

        actor = self._actor_for_job(job)
        with connect(self.dsn) as conn:
            require_lease(conn, job)
            lock_source_catalog(conn)
            _, turn = _turn(conn, actor, request.turn_id, write=True)
            if not self._job_is_current(turn, job):
                return True
            conditions, _ = self._conditions(conn, actor, turn["case_id"])
            offer = self._visible_offer(conn, actor, turn, conditions)
            keep = None
            if not offer or intent == "unavailable":
                result = {"action": "answer", "sources": [], "text": "Предложение уже недоступно. Отправку по этой реплике не выполняю."}
            elif intent in {"new_question", "related_question"}:
                conn.execute("UPDATE offers SET status='superseded',updated_at=now() WHERE id=%s", (offer["id"],))
                append_event(conn, turn["conversation_id"], turn["case_id"], offer["turn_id"], "offer.superseded",
                             {"offer": _public_action(dict(offer, status="superseded"))})
                update = {}
                if intent == "new_question":
                    update["starts_topic"] = True
                    conn.execute("UPDATE cases SET conditions='{}',condition_sources='{}',updated_at=now() WHERE id=%s",
                                 (turn["case_id"],))
                conn.execute("UPDATE turns SET request=(request-'reply_offer') || %s WHERE id=%s",
                             (Jsonb(update), turn["id"]))
                return False
            elif intent in {"confirm", "decline"}:
                decision = "confirmed" if intent == "confirm" else "declined"
                result = self._offer_receipt(conn, actor, offer["id"], offer["version"], decision)
            else:
                keep = offer["id"]
                question = offer["payload"].get("question") or offer["original_question"]
                if intent == "ask_details":
                    subject, _, _, shared = self._mail_context(conn, actor, turn["conversation_id"], offer,
                                                              offer["result"], offer["original_question"])
                    quotes = [ref["quote"] for ref in shared.get("sources", ())]
                    text = f"В Отдел качества уйдёт вопрос «{subject}»."
                    if quotes:
                        text += "\n\nК обращению приложу выдержки из источников:\n" + "\n".join(f"«{quote}»" for quote in quotes)
                    text += "\n\nВ письмо также войдёт переписка по этому вопросу. Отправить обращение?"
                else:
                    text = f"Уточните, пожалуйста, передать вопрос «{question}» в Отдел качества?"
                result = {"action": "answer", "text": text, "sources": [], "pending_offer_id": str(keep),
                          "condition_sources": self._result_refs(offer["result"])}
            self._publish_offer_reply(conn, actor, turn, result, remains_open=keep)
            finish(conn, job)
            return True

    def _answer_context(self, job, actor):
        from backend.documents import list_fragments
        from backend.runtime import require_lease

        with connect(self.dsn) as conn:
            require_lease(conn, job)
            lock_source_catalog(conn)
            _, turn = _turn(conn, actor, UUID(job.payload["turn_id"]), write=True)
            if not self._job_is_current(turn, job):
                return None
            if turn["status"] == "queued":
                conn.execute("UPDATE turns SET status = 'processing', updated_at = now() WHERE id = %s", (turn["id"],))
                _event(conn, turn, "turn.processing", {"status": "processing", "attempt": turn["attempt"]})
            conditions, _ = self._conditions(conn, actor, turn["case_id"])
            attempts = _AttemptRecorder(self.dsn, job, turn["id"], turn["case_id"])
            if turn["request"].get("reply_offer"):
                pending = {}
                offer = self._visible_offer(conn, actor, turn, conditions)
                if offer:
                    prompt = offer["text"]
                    prior = conn.execute("""
                        SELECT result FROM turns WHERE case_id=%s AND result->>'pending_offer_id'=%s
                        AND (created_at,id)<(%s,%s) ORDER BY created_at DESC,id DESC LIMIT 1
                        """, (turn["case_id"], str(offer["id"]), turn["created_at"], turn["id"])).fetchone()
                    if prior and self._result_visible(conn, actor, prior["result"], current=True):
                        prompt = prior["result"]["text"]
                    pending = dict(question=offer["payload"].get("question") or offer["original_question"],
                                   prompt=prompt, conditions=conditions)
                return AnswerRequest(turn["question"], turn["id"], turn["case_id"], conditions, (),
                                     job.deadline_at, SourceSnapshot(()), attempts=attempts, pending_offer=pending)
            topic = _topic_turns(conn, turn["case_id"], turn["id"])
            history = []
            for row in conn.execute("""
                SELECT m.role, m.text, t.result, t.question, m.source_refs,
                    a.id AS dispatch_attempt_id, d.envelope->'condition_sources' AS condition_sources
                FROM messages m LEFT JOIN turns t ON t.id = m.turn_id
                LEFT JOIN delivery_attempts a ON a.id = m.id AND a.channel = 'dion' AND a.state = 'sent'
                LEFT JOIN dion_dispatches d ON d.attempt_id = a.id
                WHERE m.case_id = %s AND m.turn_id IS DISTINCT FROM %s
                  AND (%s::uuid[] IS NULL OR t.id=ANY(%s::uuid[]))
                ORDER BY m.created_at, m.id
                """, (turn["case_id"], turn["id"], topic, topic)):
                if not self._message_visible(conn, actor, row, row["result"]):
                    continue
                result = (row["result"] or {}) if row["role"] == "assistant" and row["source_refs"] is None else {}
                history.append(HistoryMessage(row["role"], row["text"], row["question"],
                                              result.get("resolved_question"), result.get("action")))
            clarification = None
            pending_clarification = None
            clarification_id = turn["request"]["message"].get("clarification_id")
            if clarification_id:
                resolved = conn.execute("""
                    SELECT c.*, m.text, original.question AS original_question, original.result AS original_result
                    FROM clarifications c JOIN turns original ON original.id = c.turn_id
                    JOIN messages m ON m.turn_id = %s AND m.role = 'user'
                    WHERE c.id = %s AND c.case_id = %s
                    """, (turn["id"], clarification_id, turn["case_id"])).fetchone()
                if resolved and resolved["status"] == "open" and self._result_visible(
                        conn, actor, resolved["original_result"], current=True):
                    pending_clarification = resolved["id"]
                    original_question = resolved["original_question"]
                    choices = numbered(resolved["prompt"], resolved["options"])
                    history = [HistoryMessage("user", original_question),
                               HistoryMessage("assistant", choices, original_question, action="clarify")]
                elif resolved and resolved["status"] == "resolved":
                    selected = match_options(resolved["text"], resolved["options"])
                    value = resolved["resolved_value"] or (
                        selected[0]["text"] if selected and len(selected) == 1 else resolved["text"])
                    clarification = dict(value=value, original=resolved["text"], prompt=resolved["prompt"],
                                         key=resolved["clarification_key"],
                                         choices=tuple(o["text"] for o in resolved["options"] if o["id"] != "unknown"))
            count = conn.execute("""SELECT count(*) AS n FROM clarifications WHERE case_id=%s AND status='resolved'
                                    AND (%s::uuid[] IS NULL OR turn_id=ANY(%s::uuid[]))""",
                                 (turn["case_id"], topic, topic)).fetchone()["n"]
            fragments = list_fragments(conn, actor.id, hackathon=self.hackathon)
            return AnswerRequest(
                question=turn["question"], turn_id=turn["id"], case_id=turn["case_id"],
                conditions=conditions, history=tuple(history), deadline=job.deadline_at,
                sources=SourceSnapshot(fragments=fragments), clarification=clarification,
                clarification_count=count, pending_clarification=pending_clarification, attempts=attempts,
            )

    def _finish_answer(self, job, request, result: AnswerResult) -> None:
        from backend.runtime import finish, require_lease

        actor = self._actor_for_job(job)
        with connect(self.dsn) as conn:
            require_lease(conn, job)
            lock_source_catalog(conn)
            _, turn = _turn(conn, actor, request.turn_id, write=True)
            if not self._job_is_current(turn, job):
                return
            conditions, condition_sources = self._conditions(conn, actor, turn["case_id"])
            if conditions != dict(request.conditions):
                raise EvidenceChanged("Case conditions changed during the turn")
            continuing_clarification = None
            if request.pending_clarification is not None:
                # The answer draws on a previous prompt. Recheck its visibility and keep its list active.
                origin = conn.execute("""
                    SELECT c.*, t.result FROM clarifications c JOIN turns t ON t.id = c.turn_id
                    WHERE c.id=%s AND c.case_id=%s AND c.status='open'
                    """, (request.pending_clarification, turn["case_id"])).fetchone()
                if not origin or not self._result_visible(conn, actor, origin["result"], current=True):
                    raise EvidenceChanged("Clarification changed during the turn")
                continuing_clarification = origin
                condition_sources.extend(ref for ref in self._result_refs(origin["result"]) if ref not in condition_sources)
                if result.action != "answer" or result.escalation_question:
                    result = AnswerResult("clarify", "Уточнение поможет выбрать применимый пункт регламента.\n"
                                          + numbered(origin["prompt"], origin["options"]), allow_faq=False)
            if isinstance(result, VerifiedAnswer):
                from backend.documents import list_fragments

                current = list_fragments(conn, actor.id, hackathon=self.hackathon)
                questions = result.checked_questions or (request.question,)
                if current != request.sources.fragments or any(
                    potential_faq(conn, question, conditions, current) for question in questions
                ):
                    raise EvidenceChanged("Verified answer source snapshot changed during the turn")
            elif actor.channel == "guest":
                from backend.documents import list_fragments
                current = list_fragments(conn, actor.id, kind="document", hackathon=self.hackathon)
                expected = {_fragment_key(fragment) for fragment in request.sources.fragments
                            if fragment.kind == "document"}
                if expected != {_fragment_key(fragment) for fragment in current}:
                    raise EvidenceChanged("Regulation snapshot changed during the turn")
            public = self._verify_engine_answer(conn, actor, request, result)
            if result.allow_faq and result.action == "escalate" and not result.citations and actor.channel != "guest":
                faq = self._applicable_faq(conn, actor, turn["question"], conditions)
                if faq and not faq.partial:
                    if faq.conflict_revision_id:
                        public = self._regulation_first(conn, actor, turn, faq, conditions) or public
                    else:
                        public = self._faq_result(faq)
            if condition_sources:
                public["condition_sources"] = condition_sources
            if result.resolved_question:
                public["resolved_question"] = result.resolved_question
            if continuing_clarification is not None:
                public["pending_clarification_id"] = str(continuing_clarification["id"])
            self._store_result(conn, actor, turn, conditions, public, result, clarification=continuing_clarification)
            finish(conn, job)

    @staticmethod
    def _job_is_current(turn: dict, job) -> bool:
        return turn["attempt"] == job.payload["attempt"] and turn["status"] in ("queued", "processing")

    def _conditions(self, conn, actor: Actor, case_id: UUID) -> tuple[dict, list[dict]]:
        row = conn.execute("SELECT conditions, condition_sources FROM cases WHERE id = %s", (case_id,)).fetchone()
        applicable = {}
        references = []
        for key, value in row["conditions"].items():
            sources = row["condition_sources"].get(key, [])
            if self._refs_visible(conn, actor, sources, current=True):
                applicable[key] = value
                references.extend(source for source in sources if source not in references)
        return applicable, references

    def _applicable_faq(self, conn, actor, question, conditions):
        from backend.mailfaq import find_faq

        faq = find_faq(conn, question, conditions, hackathon=self.hackathon)
        if faq is not None and self._refs_visible(conn, actor, [faq.source_ref()], current=True):
            return faq
        return None

    def _regulation_first(self, conn, actor, turn, faq, conditions):
        from backend.documents import get_fragment
        from backend.mailfaq.faq import _conditions_key, _key, regulation_section

        section = regulation_section(conn, faq.conflict_revision_id, _key(turn["question"]), _conditions_key(conditions))
        if section is None:
            return None
        row, text = section
        source = get_fragment(conn, faq.conflict_revision_id, row["id"], actor.id,
                              current=True, hackathon=self.hackathon)
        if source is None:
            return None
        return {"action": "answer", "text": f"Регламент\n{text} [1]\n\nFAQ Отдела качества\n{faq.text} [2]",
                "sources": [_source_ref(source, text), faq.source_ref()],
                "notice": "Руководствуйтесь действующим регламентом. Разъяснение расходится с ним.",
                "offer_question": turn["question"]}

    @staticmethod
    def _faq_result(faq) -> dict:
        result = {"action": "answer", "text": faq.text, "sources": [faq.source_ref()]}
        notices = []
        if faq.conflict_revision_id is not None:
            notices.append("Официальное разъяснение расходится с действующей редакцией документа.")
        if faq.partial:
            notices.append("Разъяснение пока охватывает только часть вопроса.")
        if notices:
            result["notice"] = " ".join(notices)
        return result

    @staticmethod
    def _delivery_body(result: dict) -> str:
        text = result["text"]
        return f"{text}\n\n{result['notice']}" if result.get("notice") else text

    def _verify_engine_answer(self, conn, actor, request, result: AnswerResult) -> dict:
        if not isinstance(result, AnswerResult) or result.action not in {"answer", "clarify", "escalate", "out_of_scope"}:
            raise InvalidAnswer("Unknown answer action")
        if not result.text.strip() or (result.action == "answer" and not result.citations):
            raise InvalidAnswer("Missing answer text or evidence")
        snapshot = {_fragment_key(fragment): fragment for fragment in request.sources.fragments}
        def verify(citations):
            refs = []
            for citation in citations:
                fragment = snapshot.get((citation.document_id, citation.revision_id, citation.fragment_id))
                if fragment is None or not citation.quote.strip() or citation.quote not in fragment.text:
                    raise InvalidAnswer("Evidence does not match the allowed source snapshot")
                ref = _source_ref(fragment, citation.quote, citation.claim)
                if citation.linked_document and fragment.kind == "document":
                    related = related_document_url(conn, fragment, citation.quote, citation.linked_document)
                    if related:
                        ref["related_url"] = related
                refs.append(ref)
            if not self._refs_visible(conn, actor, refs, current=True):
                raise EvidenceChanged("Evidence changed during the turn")
            return refs

        refs = verify(result.citations)
        parts = [{"action": part.action, "text": part.text, "sources": verify(part.citations)}
                 for part in result.parts]
        return {"action": result.action, "text": result.text, "sources": refs, "parts": parts}

    def _store_result(self, conn, actor, turn, conditions, public, result, *, clarification=None):
        offer = None
        if public["action"] == "clarify" and clarification is None:
            options = [
                {"id": item.id, "text": item.text, "conditions": dict(item.conditions)}
                for item in result.options
            ]
            if (
                len({option["id"] for option in options}) != len(options)
                or any(not option["id"].strip() or not option["text"].strip() for option in options)
            ):
                raise InvalidAnswer("Clarification options need unique non-empty IDs and labels")
            clarification = conn.execute(
                """
                INSERT INTO clarifications (turn_id, case_id, prompt, options, clarification_key)
                VALUES (%s, %s, %s, %s, %s) RETURNING *
                """,
                (turn["id"], turn["case_id"], public["text"], Jsonb(options), result.clarification_key),
            ).fetchone()
            public["text"] = numbered(public["text"], options)
        target = public.get("offer_question") or result.escalation_question
        if public["action"] == "escalate" or (public["action"] == "answer" and target and
                                             any(ref["kind"] == "document" for ref in public["sources"])):
            target = target or turn["question"]
            offer = conn.execute(
                "INSERT INTO offers (turn_id, case_id, text, payload) VALUES (%s, %s, %s, %s) RETURNING *",
                (turn["id"], turn["case_id"], public["text"], Jsonb({"conditions": conditions, "question": target})),
            ).fetchone()
        if actor.channel == "guest":
            from backend.assembly.statistics import record_facts
            record_facts(conn, turn, public, conditions, target if offer is not None else None)
        conn.execute(
            "UPDATE turns SET status = 'completed', result = %s, updated_at = now() WHERE id = %s",
            (Jsonb(public), turn["id"]),
        )
        conn.execute(
            """
            INSERT INTO messages (conversation_id, case_id, turn_id, role, text, created_at)
            VALUES (%s, %s, %s, 'assistant', %s, clock_timestamp())
            """,
            (turn["conversation_id"], turn["case_id"], turn["id"], public["text"]),
        )
        payload = {"status": "completed", "result": self._public_result(public)}
        if clarification is not None:
            payload["clarification"] = _public_action(clarification)
        if offer is not None:
            payload["offer"] = _public_action(offer)
        _event(conn, turn, "turn.completed", payload)
        if self.queue_reply is not None and actor.channel == "dion":
            self.queue_reply(
                conn, effect_key=f"turn:{turn['id']}:{turn['attempt']}",
                conversation_id=turn["conversation_id"], case_id=turn["case_id"], turn_id=turn["id"],
                actor_id=actor.id, text=self._delivery_body(public), source_refs=public["sources"],
                offer_id=offer["id"] if offer else None,
                clarification_id=clarification["id"] if clarification else None,
            )

    def terminal_outcome(self, conn, job, reason: str) -> None:
        """Interactive queue terminal handler, committed with the terminal job status."""
        actor = Actor(job.payload["actor_id"], frozenset({"employee"}), channel=job.payload["channel"])
        _, turn = _turn(conn, actor, UUID(job.payload["turn_id"]), write=True)
        if not self._job_is_current(turn, job):
            return
        conn.execute(
            "UPDATE turns SET status = 'technical_failure', error = %s, updated_at = now() WHERE id = %s",
            (reason, turn["id"]),
        )
        _event(conn, turn, "turn.technical_failure", {"status": "technical_failure", "error": reason})
        if self.queue_reply is not None and actor.channel == "dion":
            try:
                self._actor_for_job(job)
            except DialogueError:
                return
            self.queue_reply(
                conn, effect_key=f"turn:{turn['id']}:{turn['attempt']}:failed",
                conversation_id=turn["conversation_id"], case_id=turn["case_id"], turn_id=turn["id"],
                actor_id=actor.id, text="Ответ пока не готов. Можно повторить вопрос.",
            )

    def _refs_visible(self, conn, actor: Actor, refs: list[dict], *, current: bool = False) -> bool:
        from backend.documents import get_fragment

        if not refs:
            return True
        for ref in refs:
            try:
                key = _reference_key(ref)
                quote = ref["quote"]
                if not isinstance(quote, str) or not quote.strip():
                    return False
            except (KeyError, TypeError, ValueError):
                return False
            fragment = get_fragment(conn, key[1], key[2], actor.id, current=current, hackathon=self.hackathon)
            if (
                fragment is None or _fragment_key(fragment) != key
                or fragment.kind != ref.get("kind") or quote not in fragment.text
            ):
                return False
        return True

    @staticmethod
    def _result_refs(result: dict | None) -> list[dict]:
        if result is None:
            return []
        return result.get("sources", []) + result.get("condition_sources", []) + [
            ref for part in result.get("parts", []) for ref in part["sources"]
        ]

    def _result_visible(self, conn, actor, result: dict | None, *, current: bool = False) -> bool:
        action = result.get("action") if result is not None else None
        return self._refs_visible(conn, actor, self._result_refs(result), current=current or action in {"clarify", "escalate"})

    def _message_visible(self, conn, actor, message: dict, turn_result: dict | None) -> bool:
        if message["source_refs"] is not None:
            conditions = message.get("condition_sources")
            if conditions is None and message.get("dispatch_attempt_id") is not None:
                conditions = (turn_result or {}).get("condition_sources")
                if conditions is None:
                    return False
            return self._refs_visible(
                conn, actor, message["source_refs"] + (conditions or []), current=message["role"] == "user",
            )
        return message["role"] != "assistant" or self._result_visible(conn, actor, turn_result)

    @staticmethod
    def _public_result(result: dict) -> dict:
        return {key: result[key] for key in ("action", "text", "sources", "notice", "parts", "mail_request_id", "case_number") if key in result}

    def _actions(self, conn, actor, conversation_id, *, only_open: bool = False):
        statement = """
            SELECT a.*, t.result FROM {table} a JOIN turns t ON t.id = a.turn_id
            WHERE t.conversation_id = %s {open_filter} ORDER BY a.created_at, a.id
        """
        actions = {}
        for table in ("clarifications", "offers"):
            rows = conn.execute(
                statement.format(table=table, open_filter="AND a.status = 'open'" if only_open else ""),
                (conversation_id,),
            )
            actions[table] = []
            for row in rows:
                if not self._result_visible(conn, actor, row["result"], current=row["status"] == "open"):
                    continue
                if table == "offers" and row["status"] == "open":
                    conditions, _ = self._conditions(conn, actor, row["case_id"])
                    if conditions != row["payload"]["conditions"]:
                        continue
                actions[table].append(_action(row))
        return actions

    def open_actions(self, conn, actor: Actor, conversation_id: UUID) -> dict:
        _employee(actor)
        lock_source_catalog(conn)
        _conversation(conn, actor, conversation_id, write=True)
        return self._actions(conn, actor, conversation_id, only_open=True)

    @staticmethod
    def _mail_followups(conn, actor: Actor, conversation_id: UUID) -> list[dict]:
        return conn.execute(
            """
            SELECT id AS request_id, case_id, turn_id, state, pending FROM mail_requests
            WHERE conversation_id = %s AND actor_id = %s AND state IN ('counter_question', 'partial')
            ORDER BY created_at, id
            """,
            (conversation_id, actor.id),
        ).fetchall()

    @staticmethod
    def _turn_view(turn, cursor, result, actions, followups) -> dict:
        return {
            "turn_id": turn["id"], "case_id": turn["case_id"],
            "conversation_id": turn["conversation_id"], "cursor": cursor,
            "status": turn["status"], "attempt": turn["attempt"], "question": turn["question"],
            "error": turn["error"], "result": result,
            "mail_followups": [followup for followup in followups if followup["turn_id"] == turn["id"]],
            **{key: [action for action in rows if action["turn_id"] == turn["id"]] for key, rows in actions.items()},
        }

    def get_turn(self, conn, actor: Actor, turn_id: UUID) -> dict:
        _employee(actor)
        lock_source_catalog(conn)
        conversation, turn = _turn(conn, actor, turn_id)
        result = turn["result"]
        if result is not None:
            result = self._public_result(result) if self._result_visible(conn, actor, result) else None
        actions = self._actions(conn, actor, turn["conversation_id"])
        followups = self._mail_followups(conn, actor, turn["conversation_id"])
        return self._turn_view(turn, conversation["event_cursor"], result, actions, followups)

    def history(self, conn, actor: Actor, conversation_id: UUID) -> dict:
        _employee(actor)
        lock_source_catalog(conn)
        conversation = _conversation(conn, actor, conversation_id)
        cursor = conversation["event_cursor"]
        actions = self._actions(conn, actor, conversation_id)
        followups = self._mail_followups(conn, actor, conversation_id)
        turns = []
        results = {}
        for turn in conn.execute(
            "SELECT * FROM turns WHERE conversation_id = %s ORDER BY created_at, id", (conversation_id,)
        ):
            results[turn["id"]] = result = turn["result"]
            if not self._result_visible(conn, actor, result):
                result = None
            turns.append(self._turn_view(turn, cursor, self._public_result(result) if result else None, actions, followups))
        numbered_turns = {row["turn_id"]: row for row in actions["clarifications"] if row["options"]}
        visible_messages = []
        for row in conn.execute(
            """
            SELECT m.id, m.case_id, m.turn_id, m.client_message_id, m.role, m.text, m.created_at, m.source_refs,
                a.id AS dispatch_attempt_id, d.envelope->'condition_sources' AS condition_sources
            FROM messages m
            LEFT JOIN delivery_attempts a ON a.id = m.id AND a.channel = 'dion' AND a.state = 'sent'
            LEFT JOIN dion_dispatches d ON d.attempt_id = a.id
            WHERE m.conversation_id = %s ORDER BY m.created_at, m.id
            """,
            (conversation_id,),
        ):
            if self._message_visible(conn, actor, row, results.get(row["turn_id"])):
                row.pop("dispatch_attempt_id")
                row.pop("condition_sources")
                clarification = numbered_turns.get(row["turn_id"])
                if row["role"] == "assistant" and clarification and row["text"] == clarification["prompt"]:
                    row["text"] = numbered(row["text"], clarification["options"])
                visible_messages.append(row)
        return {
            "conversation_id": conversation_id, "cursor": cursor, "messages": visible_messages,
            "turns": turns, "mail_followups": followups,
        }

    def get_source(self, conn, actor: Actor, revision_id: UUID, fragment_id: UUID):
        from backend.documents import get_fragment

        _employee(actor)
        lock_source_catalog(conn)
        source = get_fragment(conn, revision_id, fragment_id, actor.id, hackathon=self.hackathon)
        if source is None:
            raise DialogueError(404, "source_not_found")
        return source

    def delivery_text(self, conn, actor: Actor, conversation_id: UUID, text: str, source_refs: list[dict]) -> str | None:
        payload = self.delivery_payload(conn, actor, conversation_id, text, source_refs)
        return payload[0] if payload is not None else None

    def delivery_payload(
        self, conn, actor: Actor, conversation_id: UUID, text: str, source_refs: list[dict], *,
        turn_id: UUID | None = None, reevaluate_faq: bool = False,
    ) -> tuple[str, list[dict], bool] | None:
        payload = self.delivery_envelope(
            conn, actor, conversation_id, text, source_refs, turn_id=turn_id, reevaluate_faq=reevaluate_faq,
        )
        return payload[:3] if payload is not None else None

    def delivery_envelope(
        self, conn, actor: Actor, conversation_id: UUID, text: str, source_refs: list[dict], *,
        turn_id: UUID | None = None, reevaluate_faq: bool = False,
    ) -> tuple[str, list[dict], bool, list[dict]] | None:
        """Recheck delayed content and return its internal condition evidence."""
        _employee(actor)
        lock_source_catalog(conn)
        _conversation(conn, actor, conversation_id, write=reevaluate_faq and turn_id is not None)
        condition_sources = []
        keep_actions = True
        if reevaluate_faq and turn_id is not None:
            turn = conn.execute(
                "SELECT * FROM turns WHERE id = %s AND conversation_id = %s", (turn_id, conversation_id)
            ).fetchone()
            if turn is None:
                return None
            if not self._result_visible(conn, actor, turn["result"], current=True):
                return None
            result = turn["result"] or {}
            condition_sources = result.get("condition_sources", [])
            pending = result.get("pending_clarification_id")
            if pending and result.get("action") == "answer":
                keep_actions = conn.execute("""
                    SELECT 1 FROM clarifications WHERE id=%s AND case_id=%s AND status='open'
                    """, (pending, turn["case_id"])).fetchone() is not None
        if self._refs_visible(conn, actor, source_refs, current=True):
            return text, source_refs, keep_actions, condition_sources
        return None

    def _supersede_pending_action(self, conn, turn):
        action = (turn["result"] or {}).get("action")
        if action == "clarify":
            row = conn.execute(
                "SELECT * FROM clarifications WHERE turn_id = %s AND status = 'open'", (turn["id"],)
            ).fetchone()
            if row is not None:
                self._supersede_clarification(conn, turn["conversation_id"], row)
        elif action == "escalate":
            row = conn.execute(
                """
                UPDATE offers SET status = 'superseded', updated_at = now()
                WHERE turn_id = %s AND status = 'open' RETURNING id, version
                """,
                (turn["id"],),
            ).fetchone()
            if row is not None:
                _event(conn, turn, "offer.superseded", {
                    "offer_id": str(row["id"]), "version": row["version"], "status": "superseded",
                })

    @staticmethod
    def record_delivery_replacement(
        conn, *, attempt_id: UUID, conversation_id: UUID, case_id: UUID, turn_id: UUID,
        text: str, source_refs: list[dict], condition_sources: list[dict] | None = None,
    ) -> None:
        """Project a confirmed DION replacement into the original conversation history."""
        conn.execute("SELECT id FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,))
        row = conn.execute(
            """
            INSERT INTO messages (id, conversation_id, case_id, turn_id, role, text, source_refs, created_at)
            SELECT a.id, o.conversation_id, o.case_id, o.turn_id, 'assistant', %s, %s, clock_timestamp()
            FROM delivery_attempts a JOIN dion_outbox o ON o.effect_key = a.effect_key
            WHERE a.id = %s AND a.channel = 'dion' AND a.state = 'sent'
                AND o.conversation_id = %s AND o.case_id = %s AND o.turn_id = %s
            ON CONFLICT (id) DO NOTHING RETURNING id
            """,
            (text, Jsonb(source_refs), attempt_id, conversation_id, case_id, turn_id),
        ).fetchone()
        if row is not None:
            append_event(conn, conversation_id, case_id, turn_id, "dion.reply_substituted", {
                "attempt_id": str(attempt_id), "message_id": str(row["id"]),
                "text": text, "source_refs": source_refs,
                **({"condition_sources": condition_sources} if condition_sources is not None else {}),
            })

    def events(self, conn, actor: Actor, conversation_id: UUID, after: int, *, limit: int = 100) -> list[dict]:
        _employee(actor)
        if self.resolve_actor is not None:
            current = self.resolve_actor(actor.id, actor.channel)
            if current is None or current.id != actor.id or current.channel != actor.channel:
                raise DialogueError(403, "employee_role_required")
            actor = current
        lock_source_catalog(conn)
        conversation = _conversation(conn, actor, conversation_id)
        cursor = conversation["event_cursor"]
        if after < conversation["events_floor"]:
            raise DialogueError(410, "event_cursor_expired", cursor=cursor)
        if after > cursor:
            raise DialogueError(409, "event_cursor_ahead", cursor=cursor)
        rows = conn.execute(
            """
            SELECT e.*, t.result AS turn_result FROM events e LEFT JOIN turns t ON t.id = e.turn_id
            WHERE e.conversation_id = %s AND e.cursor > %s AND e.cursor <= %s
            ORDER BY e.cursor LIMIT %s
            """,
            (conversation_id, after, cursor, limit),
        )
        events = []
        for row in rows:
            result = row.pop("turn_result")
            payload = row["payload"]
            condition_sources = payload.pop("condition_sources", None)
            uncertain_legacy_scope = False
            if condition_sources is None and row["kind"] in ("dion.dispatch_prepared", "dion.reply_substituted"):
                condition_sources = (result or {}).get("condition_sources")
                uncertain_legacy_scope = condition_sources is None and (
                    row["kind"] == "dion.reply_substituted" or payload.get("substitute") is True
                )
            has_action = any(key in payload for key in ("result", "clarification", "offer"))
            hidden = (
                uncertain_legacy_scope or (has_action and not self._result_visible(conn, actor, result))
                or not self._refs_visible(conn, actor, payload.get("source_refs", []) + (condition_sources or []))
            )
            if hidden:
                row["payload"] = {"redacted": True}
            events.append(row)
        return events
