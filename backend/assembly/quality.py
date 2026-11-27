"""Browser projection and expert commands for the isolated training quality mailbox."""

from collections import defaultdict
from email import policy
from email.parser import BytesParser
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.db import connect, lock_source_catalog
from backend.mailfaq.faq import MailError, _key, publish_faq, withdraw_faq
from backend.mailfaq.message import TOKEN, clean_public_text
from backend.mailfaq.transport import Outbound, SmtpTransport
from backend.runtime import enqueue, reconcile_delivery, require_lease, send_once

from .demo_project import display_example, display_text
from .quality_checks import evidence, judge
from .quality_views import MailThread, QualityCard, QualityReport, ThreadHeader
from .smtp_gateway import BOT, DOMAIN, QUALITY

REPORT_JOB = "assembly.quality.report"
# Browser access is granted for this isolated mail channel only. Operator and DION records are separate.
DEMO_SCOPE = """r.channel='guest' AND r.actor_id LIKE 'guest:%%' AND r.sender=%s AND r.recipient=%s
                 AND r.actor_email = 'guest-' || replace(substring(r.actor_id from 7),'-','') || '@assembly.test'"""
SCOPE = (BOT, QUALITY)
CARD_SELECT = f"""
    SELECT f.id,f.request_id,f.version,f.status AS stored_status,
        coalesce(f.display_question,r.question) AS question,r.conditions,
        f.current_revision_id,f.current_fragment_id,f.document_id,fr.text AS answer,
        f.updated_at,(SELECT min(created_at) FROM source_revisions WHERE source_id=f.document_id) AS created_at,
        (SELECT count(*) FROM assembly_faq_uses u WHERE u.faq_id=f.id) +
          CASE WHEN seed.faq_id IS NULL THEN 0 ELSE 5 END AS uses,
        ck.outcome,ck.reason,ck.quote,ck.catalog,ck.retry_after,ck.pending_message_id,
        proof.id AS proof_id,proof.anchor,reg.id AS regulation_id,reg.title,reg.source_id
    FROM faq_entries f JOIN mail_requests r ON r.id=f.request_id
    JOIN source_fragments fr ON fr.id=f.current_fragment_id
    LEFT JOIN assembly_quality_seed seed ON seed.faq_id=f.id
    LEFT JOIN assembly_quality_checks ck ON ck.faq_id=f.id AND ck.faq_revision_id=f.current_revision_id
    LEFT JOIN source_fragments proof ON proof.id=ck.fragment_id
    LEFT JOIN source_revisions reg ON reg.id=proof.revision_id
    WHERE {DEMO_SCOPE}"""


class Version(BaseModel):
    version: int = Field(ge=1)


class Edit(Version):
    question: str = Field(min_length=1, max_length=8000)
    answer: str = Field(min_length=1, max_length=8000)
    confirm_conflict: bool = False


class Check(BaseModel):
    command_id: UUID


def card_status(row):
    if row["stored_status"] == "withdrawn":
        return "withdrawn"
    return "review" if row["outcome"] in ("conflict", "unconfirmed") else "active"


def get_card(conn, faq_id, *, version=None):
    row = conn.execute(CARD_SELECT + " AND f.id=%s", (*SCOPE, faq_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Разъяснение не найдено")
    if version is not None and row["version"] != version:
        raise HTTPException(409, "Карточка уже изменена. Обновите список.")
    return row


def saved_conflicts(conn, row):
    records = conn.execute(
        """
        SELECT x.ordinal,x.quote,x.claim,x.reason,f.id,f.anchor,r.source_id,r.id AS revision_id,r.title
        FROM assembly_quality_conflict_fragments x JOIN source_fragments f ON f.id=x.fragment_id
        JOIN source_revisions r ON r.id=f.revision_id JOIN sources s ON s.id=r.source_id
        WHERE x.faq_id=%s AND x.faq_revision_id=%s AND s.status='active'
            AND s.acl_actor_ids IS NULL AND r.acl_actor_ids IS NULL ORDER BY x.ordinal
        """,
        (row["id"], row["current_revision_id"]),
    ).fetchall()
    return [
        {**evidence("conflict", r["reason"], r, r["quote"]), "claim": r["claim"], "ordinal": r["ordinal"]}
        for r in records
    ]


def serialize(conn, row):
    value = {
        key: row[key]
        for key in ("id", "request_id", "version", "question", "answer", "created_at", "updated_at", "uses")
    }
    value["status"] = card_status(row)
    source = (
        None
        if not row["proof_id"]
        else dict(
            id=row["proof_id"],
            revision_id=row["regulation_id"],
            source_id=row["source_id"],
            title=row["title"],
            anchor=row["anchor"],
        )
    )
    value["evidence"] = (
        evidence(row["outcome"], row["reason"], source, row["quote"]) if row["outcome"] else None
    )
    facts = saved_conflicts(conn, row) if row["outcome"] == "conflict" else []
    value["additional_conflicts"] = [fact for fact in facts if fact["ordinal"] > 0]
    if facts and facts[0]["ordinal"] == 0 and value["evidence"]:
        value["evidence"]["claim"] = facts[0]["claim"]
    # Before the browser audit log existed, each published answer already had an
    # immutable source revision and an original mail request. Keep those editions
    # visible alongside later expert actions, including after the first new edit.
    value["history"] = conn.execute(
        """
        WITH audit AS (
            SELECT version,question,answer,at,action,actor FROM assembly_quality_history WHERE faq_id=%s
        )
        SELECT * FROM audit
        UNION ALL
        SELECT rev.version,mail.question,fr.text,rev.created_at,'archived_revision','Архив Отдела качества'
        FROM source_revisions rev JOIN source_fragments fr ON fr.revision_id=rev.id AND fr.ordinal=0
        CROSS JOIN mail_requests mail
        WHERE rev.source_id=%s AND mail.id=%s AND NOT EXISTS (
            SELECT 1 FROM audit WHERE version=rev.version
        )
        ORDER BY version""",
        (row["id"], row["document_id"], row["request_id"]),
    ).fetchall()
    if value["history"] and value["history"][-1]["version"] < row["version"]:
        value["history"].append(
            dict(
                version=row["version"],
                question=row["question"],
                answer=row["answer"],
                at=row["updated_at"],
                action="archived_state",
                actor="Архив Отдела качества",
            )
        )
    return display_example(value)


def remember(conn, row, actor, action):
    conn.execute(
        """INSERT INTO assembly_quality_history(faq_id,version,question,answer,at,actor,action)
        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (row["id"], row["version"], row["question"], row["answer"], row["updated_at"], actor, action),
    )


def catalog(conn):
    return [
        r["current_revision_id"]
        for r in conn.execute("""
        SELECT s.current_revision_id FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id
        WHERE s.source_key='assembly-corpus' AND s.source_system='document' AND s.status='active'
          AND s.acl_actor_ids IS NULL AND r.acl_actor_ids IS NULL ORDER BY s.id""")
    ]


def prepare_review(conn, question, answer, conditions):
    # Network calls run before acquiring any source visibility or conversation locks.
    return dict(
        catalog=catalog(conn),
        question=question,
        answer=answer,
        conditions=conditions,
        result=judge(conn, question, answer, conditions),
    )


def ensure_catalog(conn, pinned):
    if catalog(conn) != pinned:
        raise HTTPException(409, "Редакция регламента изменилась. Повторите проверку.")


def save_check(conn, row, result, pinned, *, bump=True):
    # Transport errors carry no semantic evidence and cannot invalidate a prior verdict.
    if result.get("unavailable") and row["outcome"] in ("conflict", "consistent"):
        conn.execute(
            "UPDATE assembly_quality_checks SET retry_after=now()+interval '5 minutes' WHERE faq_id=%s",
            (row["id"],),
        )
        return
    conn.execute(
        """
        INSERT INTO assembly_quality_checks(faq_id,faq_revision_id,outcome,fragment_id,quote,reason,catalog,retry_after)
        VALUES (%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now()+interval '5 minutes' WHEN %s THEN now() END)
        ON CONFLICT(faq_id) DO UPDATE SET
            faq_revision_id=excluded.faq_revision_id,outcome=excluded.outcome,fragment_id=excluded.fragment_id,
            quote=excluded.quote,reason=excluded.reason,catalog=excluded.catalog,retry_after=excluded.retry_after,
            pending_message_id=CASE WHEN assembly_quality_checks.faq_revision_id=excluded.faq_revision_id
                AND excluded.outcome IN ('unconfirmed','no_source') THEN assembly_quality_checks.pending_message_id END""",
        (
            row["id"],
            row["current_revision_id"],
            result["outcome"],
            result["fragment_id"],
            result["quote"],
            result["reason"],
            pinned,
            bool(result.get("unavailable")),
            bool(result.get("pending")),
        ),
    )
    if row["pending_message_id"] and result["outcome"] in ("consistent", "conflict"):
        from backend.mailfaq.service import complete_mail_review
        complete_mail_review(conn, row, result)
    conn.execute("DELETE FROM assembly_quality_conflict_fragments WHERE faq_id=%s", (row["id"],))
    if result["outcome"] == "conflict":
        facts = [result, *result.get("additional_conflicts", [])]
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO assembly_quality_conflict_fragments
                    (faq_id,ordinal,faq_revision_id,fragment_id,quote,claim,reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        row["id"],
                        i,
                        row["current_revision_id"],
                        fact["fragment_id"],
                        fact["quote"],
                        fact.get("claim", row["answer"]),
                        fact["reason"],
                    )
                    for i, fact in enumerate(facts)
                ],
            )
    after = card_status({**row, "outcome": result["outcome"]})
    changed = bump and after != card_status(row)
    conn.execute(
        """UPDATE faq_entries SET conflict_revision_id=%s, version=version+%s,
        updated_at=CASE WHEN %s THEN now() ELSE updated_at END WHERE id=%s""",
        (
            result["ref"]["revision_id"] if result["outcome"] == "conflict" else None,
            int(changed),
            changed,
            row["id"],
        ),
    )


def review_after_mail(conn, faq_id, *, prepared=None):
    row = get_card(conn, faq_id)
    pinned = catalog(conn)
    if prepared is not None:
        if (
            any(row[k] != prepared[k] for k in ("question", "answer", "conditions"))
            or pinned != prepared["catalog"]
        ):
            raise RuntimeError("Quality review inputs changed during mail interpretation")
        result = prepared["result"]
    else:
        result = judge(conn, row["question"], row["answer"], row["conditions"], use_model=False)
    save_check(conn, row, result, pinned, bump=False)
    remember(conn, row, "Отдел качества", "published")
    return result


def change_card(conn, faq_id, body, action, actor):
    row = get_card(conn, faq_id, version=body.version)
    if (action == "resume") != (row["stored_status"] == "withdrawn"):
        raise HTTPException(409, "Действие недоступно для текущего статуса")
    if action == "withdraw":
        lock_source_catalog(conn, write=True)
        row = get_card(conn, faq_id, version=body.version)
        remember(conn, row, "Отдел качества", "published")
        withdraw_faq(conn, faq_id, body.version)
    else:
        question = clean_public_text(body.question) if action == "edit" else row["question"]
        answer = clean_public_text(body.answer) if action == "edit" else row["answer"]
        if not question or not answer:
            raise HTTPException(422, "Нужны вопрос и содержательный ответ")
        prepared = prepare_review(conn, question, answer, row["conditions"])
        result = prepared["result"]
        if result.get("unavailable"):
            raise HTTPException(503, result["reason"])
        lock_source_catalog(conn, write=True)
        ensure_catalog(conn, prepared["catalog"])
        row = get_card(conn, faq_id, version=body.version)
        if action == "edit" and result["outcome"] == "conflict" and not body.confirm_conflict:
            from fastapi.encoders import jsonable_encoder

            raise HTTPException(409, jsonable_encoder(dict(code="confirm_conflict", evidence=result)))
        remember(conn, row, "Отдел качества", "published")
        request = conn.execute("SELECT * FROM mail_requests WHERE id=%s", (row["request_id"],)).fetchone()
        # Each edit owns an immutable source revision, including a question-only correction.
        publish_faq(conn, {**request, "question": question}, answer, replace=True, force_revision=True)
        conn.execute(
            "UPDATE faq_entries SET display_question=%s,question_key=%s WHERE id=%s",
            (question, _key(question), faq_id),
        )
        save_check(conn, get_card(conn, faq_id), result, prepared["catalog"], bump=False)
    changed = get_card(conn, faq_id)
    remember(conn, changed, actor, action)
    return serialize(conn, changed)


def thread_rows(conn, id_=None):
    return conn.execute(
        f"""
        SELECT r.id,r.case_number,r.question,r.actor_email,r.sender,r.recipient,r.state,r.updated_at
        FROM mail_requests r WHERE {DEMO_SCOPE} AND (%s::uuid IS NULL OR r.id=%s)
        AND EXISTS (SELECT 1 FROM mail_outbound o WHERE o.request_id=r.id AND o.state='sent')
        ORDER BY r.updated_at DESC,r.id""",
        (*SCOPE, id_, id_),
    ).fetchall()


def read_threads(conn, rows):
    by_id = {r["id"]: r for r in rows}
    messages = defaultdict(list)
    reply_ids = {}
    ids = list(by_id)
    # Fetch each side of the exchange once, even when searching the entire training inbox.
    for item in conn.execute(
        """
        SELECT request_id,message_id,body,created_at FROM mail_outbound
        WHERE request_id=ANY(%s::uuid[]) AND state='sent' ORDER BY created_at,id""",
        (ids,),
    ):
        row = by_id[item["request_id"]]
        messages[row["id"]].append(
            dict(
                message_id=item["message_id"],
                sender=BOT,
                to=QUALITY,
                cc=row["actor_email"],
                subject=f"Обращение ОК{row['case_number']}",
                text=display_text(TOKEN.sub("", item["body"]).strip()),
                at=item["created_at"],
            )
        )
        reply_ids[row["id"]] = item["message_id"]
    for item in conn.execute(
        """
        SELECT request_id,message_id,raw,created_at,state FROM mail_inbound
        WHERE request_id=ANY(%s::uuid[]) AND state IN ('processed','interpreting','review') AND trusted ORDER BY created_at,uid""",
        (ids,),
    ):
        msg = BytesParser(policy=policy.default).parsebytes(item["raw"])
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            continue
        content = part.get_content()
        if not isinstance(content, str):
            continue
        messages[item["request_id"]].append(
            dict(
                message_id=item["message_id"],
                sender=str(msg.get("From", "")),
                to=str(msg.get("To", "")),
                cc=str(msg.get("Cc", "")),
                subject=TOKEN.sub("", str(msg.get("Subject", ""))).strip(),
                text=display_text(TOKEN.sub("", content).strip()),
                at=item["created_at"],
                processing=item["state"] if item["state"] in ("interpreting", "review") else None,
            )
        )
    threads = []
    for row in rows:
        letters = sorted(messages[row["id"]], key=lambda m: m["at"])
        if not letters:
            continue
        latest = letters[-1]
        is_open = row["state"] not in ("resolved", "withdrawn")
        threads.append(
            dict(
                id=row["id"],
                subject=f"Обращение ОК{row['case_number']}",
                updated_at=latest["at"],
                sender=latest["sender"],
                preview=latest["text"][:150],
                open=is_open,
                service=False,
                messages=letters,
                reply_message_id=reply_ids.get(row["id"]) if is_open else None,
            )
        )
    return threads


def report_thread(row):
    row = display_example(row)
    msg = dict(
        message_id=f"<quality-report-{row['id']}@{DOMAIN}>",
        sender=BOT,
        to=QUALITY,
        cc="",
        subject=row["subject"],
        text=row["text"],
        at=row["created_at"],
    )
    return dict(
        id=row["id"],
        subject=row["subject"],
        updated_at=row["created_at"],
        sender=BOT,
        preview=row["text"][:150],
        open=False,
        service=True,
        messages=[msg],
        reply_message_id=None,
    )


def run_checks(conn, command_id):
    # Concurrent retries of the same command share one physical report.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 18))", (str(command_id),))
    existing = conn.execute("SELECT * FROM assembly_quality_reports WHERE id=%s", (command_id,)).fetchone()
    if existing:
        return existing
    pinned = catalog(conn)
    rows = conn.execute(CARD_SELECT + " AND f.status='active' ORDER BY f.id", SCOPE).fetchall()
    reviews = []
    for row in rows:
        if (row["outcome"] in ("conflict", "consistent", "no_source")
                and row["catalog"] == pinned and row["retry_after"] is None):
            cached = serialize(conn, row)
            result = {**cached["evidence"], "additional_conflicts": cached["additional_conflicts"]}
        else:
            result = judge(conn, row["question"], row["answer"], row["conditions"])
        reviews.append((row, result))
    lock_source_catalog(conn, write=True)
    ensure_catalog(conn, pinned)
    lines = []
    for snapshot, result in reviews:
        row = get_card(conn, snapshot["id"], version=snapshot["version"])
        previous = card_status(row)
        remember(conn, row, "Отдел качества", "published")
        save_check(conn, row, result, pinned)
        current = get_card(conn, row["id"])
        state = card_status(current)
        if previous != state:
            remember(conn, current, "Судья", "review")
        labels = {"active": "Действует", "review": "Требует пересмотра"}
        status = labels[state] if state == previous else f"{labels[previous]} → {labels[state]}"
        line = f"{row['question']}\n{status}. {result['reason']}\nРазъяснение: {row['answer']}"
        if result["ref"]:
            line += f"\n{result['title']}, п. {result['anchor']}: {result['quote']}"
        for fact in result.get("additional_conflicts", []):
            line += (
                f"\n{fact['claim']} — {fact['title']}, п. {fact['anchor']}: {fact['quote']}. {fact['reason']}"
            )
        lines.append(line)
    subject = "Служебный отчёт о проверке конфликтов"
    text = "\n\n".join(lines) if lines else "Нет действующих разъяснений для проверки."
    report = conn.execute(
        """INSERT INTO assembly_quality_reports(id,subject,text)
        VALUES (%s,%s,%s) RETURNING *""",
        (command_id, subject, text),
    ).fetchone()
    enqueue(
        conn,
        queue="background",
        kind=REPORT_JOB,
        key=str(command_id),
        payload={"report_id": str(command_id)},
        max_attempts=5,
    )
    return report


def deliver_report(assembly, job):
    id_ = job.payload["report_id"]
    with connect(assembly.dsn) as conn:
        row = conn.execute("SELECT * FROM assembly_quality_reports WHERE id=%s", (id_,)).fetchone()
    mid = f"<quality-report-{id_}@{DOMAIN}>"
    msg = Outbound(mid, BOT, QUALITY, QUALITY, BOT, row["subject"], row["text"])
    outcome = send_once(
        assembly.dsn,
        channel="smtp",
        effect_key=f"quality-report:{id_}",
        job=job,
        send=lambda: SmtpTransport(assembly.mail).send(msg),
    )
    if outcome.state in ("sending", "failed"):
        raise RuntimeError("Quality report delivery pending")
    with connect(assembly.dsn) as conn:
        require_lease(conn, job)
        conn.execute("UPDATE assembly_quality_reports SET delivery=%s WHERE id=%s", (outcome.state, id_))


def report_terminal(conn, job, reason):
    conn.execute(
        """UPDATE assembly_quality_reports SET delivery='failed'
        WHERE id=%s AND delivery='pending'""",
        (job.payload["report_id"],),
    )


def reconcile_reports(assembly):
    with connect(assembly.dsn) as conn:
        rows = conn.execute("""
            SELECT r.id,d.id AS attempt FROM assembly_quality_reports r
            JOIN delivery_attempts d ON d.effect_key='quality-report:' || r.id::text AND d.channel='smtp'
            WHERE r.delivery IN ('pending','delivery_unknown') AND d.state='delivery_unknown'
            ORDER BY r.created_at LIMIT 20""").fetchall()
    for row in rows:
        mid = f"<quality-report-{row['id']}@{DOMAIN}>"
        if assembly.inbox.contains(mid):
            with connect(assembly.dsn) as conn:
                reconcile_delivery(
                    assembly.dsn,
                    conn=conn,
                    attempt_id=row["attempt"],
                    state="sent",
                    provider_id=mid,
                    detail="IMAP Message-ID receipt",
                )
                conn.execute("UPDATE assembly_quality_reports SET delivery='sent' WHERE id=%s", (row["id"],))


def recheck_revision(assembly):
    """Review a changed catalog or retry a temporary failure without blocking chat."""
    with connect(assembly.dsn) as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(31018) AS acquired").fetchone()["acquired"]:
            return False
        pinned = catalog(conn)
        row = conn.execute(
            CARD_SELECT
            + """
            AND f.status='active' AND (ck.retry_after <= now()
                OR (ck.retry_after IS NULL AND ck.catalog IS DISTINCT FROM %s::uuid[]))
            AND (ck.faq_id IS NOT NULL OR seed.faq_id IS NULL)
            ORDER BY f.updated_at LIMIT 1""",
            (*SCOPE, pinned),
        ).fetchone()
        if row is None:
            return False
        result = judge(conn, row["question"], row["answer"], row["conditions"])
        lock_source_catalog(conn, write=True)
        current = get_card(conn, row["id"])
        if (current["version"] != row["version"] or current["current_revision_id"] != row["current_revision_id"]
                or current["retry_after"] != row["retry_after"] or current["catalog"] != row["catalog"]
                or catalog(conn) != pinned):
            return False
        remember(conn, current, "Отдел качества", "published")
        save_check(conn, current, result, pinned)
        changed = get_card(conn, row["id"])
        if changed["version"] != current["version"]:
            remember(conn, changed, "Судья", "review")
        return True


def build_router(assembly):
    router = APIRouter(prefix="/v1/browser/quality")

    def browser(request: Request):
        with connect(assembly.dsn) as conn:
            row = assembly.session(conn, request)
        if row is None:
            raise HTTPException(401, "Откройте Демо DION")
        if request.method != "GET":
            assembly.check_origin(request)
        return str(row["id"])

    def expert(request: Request, guest=Depends(browser)):
        assembly.authorized_inbox(request)
        return "Эксперт " + guest

    @router.get("/cards", response_model=list[QualityCard])
    def cards(_=Depends(browser)):
        with connect(assembly.dsn) as conn:
            rows = conn.execute(CARD_SELECT + " ORDER BY f.updated_at DESC,f.id", SCOPE).fetchall()
            rows.sort(key=lambda r: {"review": 0, "active": 1, "withdrawn": 2}[card_status(r)])
            return [serialize(conn, r) for r in rows]

    @router.post("/checks", response_model=QualityReport)
    def check(body: Check, _=Depends(browser)):
        with connect(assembly.dsn) as conn:
            return display_example(run_checks(conn, body.command_id))

    def change(id_, body, action, actor):
        try:
            with connect(assembly.dsn) as conn:
                return change_card(conn, id_, body, action, actor)
        except MailError as exc:
            raise HTTPException(exc.status, exc.code) from None

    @router.post("/cards/{id_}/edit", response_model=QualityCard)
    def edit(id_: UUID, body: Edit, actor=Depends(expert)):
        return change(id_, body, "edit", actor)

    @router.post("/cards/{id_}/withdraw", response_model=QualityCard)
    def withdraw(id_: UUID, body: Version, actor=Depends(expert)):
        return change(id_, body, "withdraw", actor)

    @router.post("/cards/{id_}/resume", response_model=QualityCard)
    def resume(id_: UUID, body: Version, actor=Depends(expert)):
        return change(id_, body, "resume", actor)

    @router.get("/threads", response_model=list[ThreadHeader])
    def threads(q: str = Query("", max_length=300), _=Depends(browser)):
        needle = q.strip().casefold()
        with connect(assembly.dsn) as conn:
            results = read_threads(conn, thread_rows(conn))
            results.extend(report_thread(r) for r in conn.execute("SELECT * FROM assembly_quality_reports"))

        def matches(t):
            return needle in t["subject"].casefold() or any(
                needle in str(m[field]).casefold()
                for m in t["messages"]
                for field in ("subject", "sender", "to", "cc", "text")
            )

        return [
            {k: v for k, v in t.items() if k not in ("messages", "reply_message_id")}
            for t in sorted(results, key=lambda t: t["updated_at"], reverse=True)
            if not needle or matches(t)
        ]

    @router.get("/threads/{id_}", response_model=MailThread)
    def fetch_thread(id_: UUID, _=Depends(browser)):
        with connect(assembly.dsn) as conn:
            rows = thread_rows(conn, id_)
            if rows:
                return read_threads(conn, rows)[0]
            report = conn.execute("SELECT * FROM assembly_quality_reports WHERE id=%s", (id_,)).fetchone()
            if report:
                return report_thread(report)
        raise HTTPException(404, "Учебная цепочка не найдена")

    return router
