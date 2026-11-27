"""Session-scoped DION walkthrough and the most recent user turn's physical usage."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.contracts.api import SourceReference
from backend.db import connect
from backend.documents import get_fragment
from backend.assembly.fast_answers import faq_candidate
from backend.assembly.faq_scope import compatible_ids
from eval.scope.procedures import directories, normalized


class OpenCitation(BaseModel):
    fragment_id: UUID


class Hint(BaseModel):
    stage: str
    text: str
    example: str = ""
    action: Literal["example", "open", "inbox", "none"] = "example"
    thread_id: UUID | None = None
    case_id: UUID | None = None
    source: SourceReference | None = None


class Step(BaseModel):
    ordinal: int
    role: str
    model: str | None
    provider: str | None
    status: str
    trace_id: str | None
    local: bool
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    started_at: datetime | None = None
    elapsed_ms: float | None = None


class MailEvent(BaseModel):
    state: str
    number: int
    sender: str
    recipient: str
    cc: str


class LastLog(BaseModel):
    turn_id: UUID | None = None
    status: str = "empty"
    steps: list[Step]
    mail: MailEvent | None = None
    total_input_tokens: int | None = 0
    total_output_tokens: int | None = 0
    total_cost_usd: Decimal | None = Decimal(0)
    incomplete: bool = False


# These questions exercise the same corpus clauses and outcomes as the browser
# and verified-answer acceptance cases. Private grading questions are kept separate.
EXAMPLES = {
    "simple": ("В какой срок в Дирекции информационной безопасности назначают руководителя проекта при инициации?",),
    "compound": ("В какой срок в Дирекции информационной безопасности назначают руководителя проекта при инициации? "
                 "Какой срок подготовки бюджетной оценки для торгов у Технической дирекции?",),
    "clarify": ("В какой срок должны назначить руководителя проекта?",),
    "gap": ("Кто замещает координатора проектов Дирекции ИБ на время отпуска?",
            "Какой точный процент минимальной маржинальности применяется к проекту пресейла ИБ по внутреннему регламенту контроля маржинальности?"),
}
PROMPTS = {
    "simple": "Начните с простого вопроса и откройте пункт регламента.",
    "compound": "Попробуйте составной вопрос.",
    "clarify": "Задайте вопрос без указания дирекции.",
    "gap": "Попробуйте вопрос, на который в регламентах нет ответа.",
}


def example_hint(conn, session, stage):
    examples = EXAMPLES[stage]
    if stage == "gap":
        # Exclude an example when an active explanation could answer it. Be
        # conservative if its different phrasing cannot be ruled out.
        faqs = conn.execute("""
            SELECT question_key, conditions_key FROM faq_entries WHERE status='active'
        """).fetchall()
        examples = [q for q in examples if not any(
            faq_candidate(row, directories(q), normalized(q), {}) and compatible_ids(row["question_key"], q, {})
            for row in faqs)]
        if not examples:
            return Hint(stage="done", text="", action="none")
    example = examples[session["id"].int % len(examples)]
    return Hint(stage=stage, text=PROMPTS[stage], example=example)


def next_hint(conn, session):
    conversation = session["conversation_id"]
    rows = conn.execute("""
        SELECT t.id, t.case_id, t.question, t.result, t.request, o.status AS offer,
               c.status AS clarification, c.options,
               EXISTS (SELECT 1 FROM assembly_opened_citations x WHERE x.turn_id=t.id) AS opened
        FROM turns t LEFT JOIN offers o ON o.turn_id=t.id
        LEFT JOIN clarifications c ON c.turn_id=t.id
        WHERE t.conversation_id=%s AND t.status='completed'
        ORDER BY t.created_at, t.id
    """, (conversation,)).fetchall()
    request = conn.execute("""
        SELECT r.id, r.question, r.state, o.state AS delivery, EXISTS (
           SELECT 1 FROM faq_entries f JOIN assembly_faq_uses u ON u.faq_id=f.id
           JOIN turns t ON t.id=u.turn_id WHERE f.request_id=r.id AND t.conversation_id=r.conversation_id
        ) AS reused
        FROM mail_requests r JOIN mail_outbound o ON o.request_id=r.id AND o.business_key='initial'
        WHERE r.conversation_id=%s
        ORDER BY r.created_at DESC, r.id DESC LIMIT 1
    """, (conversation,)).fetchone()
    if rows:
        latest = rows[-1]
        if latest["offer"] == "open":
            return Hint(stage="consent", text="Подтвердите отправку обращения в Отдел качества.",
                        example="Да, передай", case_id=latest["case_id"])
        if latest["offer"] == "declined" or (latest["result"] or {}).get("mail_decision") == "declined":
            return example_hint(conn, session, "gap")
        if latest["clarification"] == "open":
            options = latest["options"] or []
            preferred = next((o for o in options if o["text"] == "Дирекция ИБ"), None)
            example = str(options.index(preferred) + 1) if preferred else ("1" if options else "")
            return Hint(stage="answer", text="Ответьте на уточнение бота.", example=example, case_id=latest["case_id"],
                        action="example" if example else "none")
    if request and request["state"] not in {"withdrawn", "delivery_failed"}:
        if request["state"] != "resolved":
            if request["delivery"] != "sent":
                label = ("Проверяем результат доставки письма." if request["delivery"] == "delivery_unknown"
                         else "Письмо в очереди на отправку в Отдел качества.")
                return Hint(stage="inbox", text=label, action="none")
            return Hint(stage="inbox", text="Ответьте на письмо в разделе «Отдел качества». После этого ответ появится здесь.",
                        action="inbox", thread_id=request["id"])
        if request["reused"]:
            return Hint(stage="done", text="", action="none")
        return Hint(stage="repeat", text="Задайте вопрос ещё раз, чтобы проверить новое разъяснение.",
                    example=request["question"], thread_id=request["id"])

    stage = "simple"
    source_to_open = None
    for row in rows:
        result = row["result"] or {}
        docs = [ref for ref in result.get("sources", []) if ref["kind"] == "document"]
        if result.get("action") != "answer" or not docs:
            continue
        if stage == "simple":
            if row["opened"]:
                stage, source_to_open = "compound", None
            elif source_to_open is None:
                source_to_open = docs[0]
        elif stage == "compound" and len(result.get("parts", [])) > 1 and all(
            part["action"] == "answer" and part["sources"] for part in result["parts"]
        ):
            stage = "clarify"
        elif stage == "clarify" and row["request"].get("message", {}).get("clarification_id"):
            stage = "gap"
    if stage == "simple" and source_to_open:
        source = get_fragment(conn, UUID(source_to_open["revision_id"]), UUID(source_to_open["fragment_id"]),
                              f"guest:{session['id']}")
        if source is not None:
            return Hint(stage="open", text="Откройте процитированный пункт регламента.", action="open",
                        source=SourceReference(**source_to_open))
    return example_hint(conn, session, stage)


def latest_log(conn, session):
    turn = conn.execute("""
        SELECT id, status, result->>'mail_request_id' AS mail_request
        FROM turns WHERE conversation_id=%s ORDER BY created_at DESC, id DESC LIMIT 1
    """, (session["conversation_id"],)).fetchone()
    if not turn:
        return LastLog(steps=[])
    rows = conn.execute("""
        SELECT started_at, id::text, role, model, route AS provider, state AS status, trace_id,
               false AS local, input_tokens, output_tokens, cost AS cost_usd,
               extract(epoch FROM finished_at - started_at)::double precision * 1000 AS elapsed_ms
        FROM model_calls WHERE turn_id=%s
          AND (job_id IS NULL OR EXISTS (SELECT 1 FROM jobs WHERE id=model_calls.job_id AND kind='answer_turn'))
        UNION ALL
        SELECT started_at, id::text, role, NULL, NULL, state, NULL, true, NULL, NULL, 0, elapsed_ms
        FROM assembly_local_steps WHERE turn_id=%s ORDER BY started_at, id
    """, (turn["id"], turn["id"])).fetchall()
    steps = [Step(ordinal=i, **row) for i, row in enumerate(rows, 1)]
    paid = [step for step in steps if not step.local]
    mail = None
    if turn["mail_request"]:
        mail = conn.execute("""
            SELECT o.state, r.case_number AS number, r.sender, r.recipient, r.actor_email AS cc
            FROM mail_requests r JOIN mail_outbound o ON o.request_id=r.id AND o.business_key='initial'
            WHERE r.id=%s AND r.conversation_id=%s
        """, (turn["mail_request"], session["conversation_id"])).fetchone()
    def subtotal(field):
        values = [value for step in paid if (value := getattr(step, field)) is not None]
        return sum(values) if values or not paid else None

    return LastLog(
        turn_id=turn["id"], status=turn["status"], steps=steps, mail=mail,
        total_input_tokens=subtotal("input_tokens"),
        total_output_tokens=subtotal("output_tokens"),
        total_cost_usd=subtotal("cost_usd"),
        incomplete=any(None in (s.input_tokens, s.output_tokens, s.cost_usd) for s in paid),
    )


def browser_guidance(assembly):
    router = APIRouter(prefix="/v1/browser")

    def owner(conn, request, response):
        response.headers["Cache-Control"] = "private, no-store"
        session = assembly.session(conn, request)
        if session is None:
            raise HTTPException(401, "Откройте личный чат")
        return session

    @router.get("/last-log", response_model=LastLog)
    def last_log(request: Request, response: Response):
        with connect(assembly.dsn) as conn:
            return latest_log(conn, owner(conn, request, response))

    @router.get("/hint", response_model=Hint)
    def hint(request: Request, response: Response):
        with connect(assembly.dsn) as conn:
            return next_hint(conn, owner(conn, request, response))

    @router.post("/hint/open", status_code=204)
    def open_citation(body: OpenCitation, request: Request, response: Response):
        assembly.check_origin(request)
        with connect(assembly.dsn) as conn:
            session = owner(conn, request, response)
            turn = conn.execute("""
                SELECT t.id, f.revision_id FROM turns t
                JOIN source_fragments f ON f.id=%s
                WHERE t.conversation_id=%s AND t.result->'sources' @>
                    jsonb_build_array(jsonb_build_object('fragment_id', f.id::text, 'kind', 'document'))
                ORDER BY t.created_at DESC, t.id DESC LIMIT 1
            """, (body.fragment_id, session["conversation_id"])).fetchone()
            if not turn or get_fragment(conn, turn["revision_id"], body.fragment_id, f"guest:{session['id']}") is None:
                raise HTTPException(404, "Цитата недоступна")
            conn.execute("""
                INSERT INTO assembly_opened_citations(turn_id, fragment_id) VALUES (%s,%s) ON CONFLICT DO NOTHING
            """, (turn["id"], body.fragment_id))
        return Response(status_code=204, headers={"Cache-Control": "private, no-store"})

    return router
