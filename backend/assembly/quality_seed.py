"""Install the approved DB 1.6 training revision and one historical mail exchange."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4, uuid5, NAMESPACE_URL
from zipfile import ZipFile, ZIP_STORED

from psycopg.types.json import Jsonb

from backend.db import connect, lock_source_catalog
from backend.documents.catalog import activate_revision, stage_revision
from backend.documents.ports import PageSnapshot
from backend.mailfaq.faq import publish_faq, reconcile_document_revision
from backend.mailfaq.transport import Outbound
from eval.retrieval.extract import CORPUS, TITLES, clauses

from .corpus import retain
from .search import index_passages
from .smtp_gateway import BOT, DOMAIN, QUALITY

QUESTION = "Каков рекомендуемый срок выдачи точной оценки в ДБРЦТ?"
ANSWER = "Проектная деятельность ДБРЦТ, п. 1.6. Рекомендуемые сроки выдачи: для точных оценок – 6 рабочих дней."
SEED_ACTOR = uuid5(NAMESPACE_URL, "assembly.test/demo/quality/ok1")
FIRST_ID = f"<demo-ok1-request@{DOMAIN}>"
REPLY_ID = f"<demo-ok1-reply@{DOMAIN}>"


def training_original():
    """Create a deterministic, downloadable DOCX using the supplied original."""
    old = "для точных оценок – 5 рабочих дней;".encode()
    new = "для точных оценок – 7 рабочих дней;".encode()
    stream = BytesIO()
    with ZipFile(CORPUS / (TITLES["DB"] + ".docx")) as archive, ZipFile(stream, "w") as output:
        for entry in archive.infolist():
            data = archive.read(entry)
            if entry.filename == "word/document.xml":
                if data.count(old) != 1:
                    raise ValueError("Expected one approved DB clause about the exact estimate")
                data = data.replace(old, new)
            output.writestr(entry, data, compress_type=ZIP_STORED)
    return stream.getvalue()


def seed_revision(conn, directory):
    title = TITLES["DB"]
    digest, path = retain(directory, title + " — учебная редакция 2.docx", training_original())
    page = PageSnapshot("DB", 2, title, digest)
    revision, indexed = stage_revision(conn, "assembly-corpus", page)
    if indexed:
        return
    parts = [(uuid5(revision.revision_id, p["ref"]), p) for p in clauses("DB", title, path)]
    conn.execute("DELETE FROM source_fragments WHERE revision_id = %s", (revision.revision_id,))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO source_fragments(id,revision_id,ordinal,anchor,heading_path,text)
            VALUES (%s,%s,%s,%s,%s,%s)
        """,
            [
                (f, revision.revision_id, i, p["ref"], [p["section"]], p["text"])
                for i, (f, p) in enumerate(parts)
            ],
        )
    index_passages(conn, parts)
    conn.execute(
        "INSERT INTO assembly_originals VALUES (%s,%s,%s,%s)",
        (revision.revision_id, digest, path.name, str(path.relative_to(directory))),
    )
    conn.execute("UPDATE source_revisions SET indexed_at=now() WHERE id=%s", (revision.revision_id,))
    activate_revision(conn, revision, page, reconcile_document_revision)


def seed_quality(dsn, directory):
    with connect(dsn) as conn:
        lock_source_catalog(conn, write=True)
        if conn.execute("SELECT 1 FROM assembly_quality_seed").fetchone():
            return
        # An upgraded installation can own an immutable revision 2 already.
        existing = conn.execute("""SELECT 1 FROM source_revisions r JOIN sources s ON s.id=r.source_id
            WHERE s.source_key='assembly-corpus' AND s.external_id='DB' AND r.version=2 AND r.indexed_at IS NOT NULL""").fetchone()
        if not existing:
            seed_revision(conn, Path(directory))
        who = f"guest:{SEED_ACTOR}"
        cc = f"guest-{SEED_ACTOR.hex}@{DOMAIN}"
        conversation = conn.execute(
            "INSERT INTO conversations(owner_id) VALUES (%s) RETURNING id", (who,)
        ).fetchone()["id"]
        case = conn.execute(
            "INSERT INTO cases(conversation_id) VALUES (%s) RETURNING id", (conversation,)
        ).fetchone()["id"]
        turn = conn.execute(
            """INSERT INTO turns(conversation_id,case_id,request_id,question,status)
            VALUES (%s,%s,'ok1',%s,'completed') RETURNING id""",
            (conversation, case, QUESTION),
        ).fetchone()["id"]
        offer = conn.execute(
            """INSERT INTO offers(turn_id,case_id,status,text)
            VALUES (%s,%s,'confirmed','Передать вопрос в Отдел качества') RETURNING id""",
            (turn, case),
        ).fetchone()["id"]
        request = conn.execute(
            """
            INSERT INTO mail_requests(id,offer_id,conversation_id,case_id,turn_id,actor_id,actor_email,
                channel,question,conditions,recipient,sender,reply_to,token,state,case_number)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'guest',%s,'{}',%s,%s,%s,%s,'resolved',1) RETURNING *
        """,
            (uuid4(), offer, conversation, case, turn, who, cc, QUESTION, QUALITY, BOT, BOT, uuid4().hex),
        ).fetchone()
        body = (
            f"Вопрос от сотрудника\n{QUESTION}\n\nПочему направлено обращение\n"
            "Просим подтвердить срок для точной оценки. В исторической редакции 1, п. 1.6 указан срок 5 рабочих дней."
            f"\n\nДиалог до отправки\nСотрудник: {QUESTION}\nБот: Передать вопрос в Отдел качества?\nСотрудник: Да, передай."
        )
        conn.execute(
            """INSERT INTO mail_outbound(id,request_id,business_key,message_id,body,state)
            VALUES (%s,%s,'initial',%s,%s,'sent')""",
            (uuid4(), request["id"], FIRST_ID, body),
        )
        raw = Outbound(
            REPLY_ID, QUALITY, BOT, cc, QUALITY, "Re: Обращение ОК1", ANSWER, FIRST_ID, (FIRST_ID,)
        ).bytes()
        inbound_id = uuid4()
        conn.execute(
            """INSERT INTO mail_inbound(id,mailbox,uidvalidity,uid,fingerprint,message_id,trusted,raw,parsed,
            request_id,state) VALUES (%s,'assembly-demo-history','1',1,%s,%s,true,%s,%s,%s,'processed')""",
            (
                inbound_id,
                sha256(raw).hexdigest(),
                REPLY_ID,
                raw,
                Jsonb({"kind": "substantive", "public_text": ANSWER}),
                request["id"],
            ),
        )
        conn.execute("UPDATE mail_requests SET last_inbound_id=%s WHERE id=%s", (inbound_id, request["id"]))
        faq = publish_faq(conn, request, ANSWER)
        conn.execute("INSERT INTO assembly_quality_seed VALUES (%s)", (faq.faq_id,))
