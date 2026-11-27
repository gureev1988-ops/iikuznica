"""Publish versioned official FAQ and maintain document coverage pins."""

from dataclasses import dataclass
from hashlib import sha256
import json
import unicodedata
from uuid import UUID, uuid4

from backend.config import HACKATHON_DOCUMENT_SOURCES, HACKATHON_QUALITY_EMAIL
from backend.db import lock_source_catalog

from .message import clean_public_text


class MailError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class FaqMatch:
    faq_id: UUID
    version: int
    document_id: UUID
    revision_id: UUID
    fragment_id: UUID
    text: str
    conflict_revision_id: UUID | None
    partial: bool = False
    kind: str = "faq"
    quote: str | None = None

    def source_ref(self) -> dict:
        return {
            "document_id": str(self.document_id),
            "revision_id": str(self.revision_id),
            "fragment_id": str(self.fragment_id),
            "quote": self.quote if self.quote is not None else self.text,
            "kind": self.kind,
            "url": f"/v1/sources/{self.revision_id}/fragments/{self.fragment_id}",
        }


def lock_sources(conn) -> None:
    lock_source_catalog(conn, write=True)


def _key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).strip(" .!?")


def _conditions_key(conditions: dict) -> str:
    # Preserve types and values, independent of the order in JSON objects.
    return json.dumps(conditions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _match(row: dict) -> FaqMatch:
    return FaqMatch(
        row["id"], row["version"], row["document_id"], row["current_revision_id"],
        row["current_fragment_id"], row["text"], row["conflict_revision_id"],
        bool(row["pending"]),
    )


def find_faq(conn, question: str, conditions: dict, *, hackathon: bool = False) -> FaqMatch | None:
    """Resolve an exact published explanation or its verified demo document coverage."""
    rows = conn.execute(
        """
        SELECT f.*, r.pending, fragment.text, s.status AS source_status
        FROM faq_entries f
        JOIN mail_requests r ON r.id = f.request_id
        JOIN sources s ON s.id = f.document_id AND s.current_revision_id = f.current_revision_id
        JOIN source_fragments fragment ON fragment.id = f.current_fragment_id
        WHERE f.status = 'active' AND f.question_key = %s AND f.conditions_key = %s
            AND (NOT %s OR r.recipient = %s)
        ORDER BY (f.conflict_revision_id IS NOT NULL) DESC, f.updated_at DESC, f.id
        """,
        (_key(question), _conditions_key(conditions), hackathon, HACKATHON_QUALITY_EMAIL),
    ).fetchall()
    if not rows or len({row["text"] for row in rows}) != 1:
        return None
    eligible = next((row for row in rows if row["source_status"] == "active"), None)
    if hackathon and eligible and not _public_document(conn, eligible["conflict_revision_id"], hackathon=True):
        eligible["conflict_revision_id"] = None
    if eligible:
        return _match(eligible)
    if hackathon:
        for row in rows:
            document = _public_document(conn, row["covered_revision_id"], hackathon=True)
            if document is None or row["pending"]:
                continue
            covering = _covering_fragment(conn, document["id"], row["question_key"], row["text"], row["conditions_key"])
            if covering is not None:
                fragment, text = covering
                return FaqMatch(
                    row["id"], row["version"], document["source_id"], document["id"], fragment["id"], text,
                    conflict_revision_id=None, kind="document", quote=fragment["text"],
                )
    return None


def current_faq_notification(conn, revision_id: UUID, fragment_id: UUID) -> bool:
    """Validate an original-recipient notification for a currently covered FAQ."""
    row = conn.execute(
        """
        SELECT f.covered_revision_id
        FROM faq_entries f JOIN sources s ON s.id = f.document_id
        WHERE f.status = 'active' AND s.status = 'archived' AND s.current_revision_id = %s
            AND f.current_revision_id = %s AND f.current_fragment_id = %s
        """,
        (revision_id, revision_id, fragment_id),
    ).fetchone()
    return row is not None and _public_document(conn, row["covered_revision_id"]) is not None


def _metadata(conn, faq_id: UUID, version: int, *, hackathon: bool = False) -> dict:
    row = conn.execute(
        """
        SELECT f.* FROM faq_entries f JOIN mail_requests r ON r.id = f.request_id
        WHERE f.id = %s AND (NOT %s OR r.recipient = %s) FOR UPDATE OF f
        """,
        (faq_id, hackathon, HACKATHON_QUALITY_EMAIL),
    ).fetchone()
    if row is None:
        raise MailError(404, "faq_not_found")
    if row["version"] != version:
        raise MailError(409, "stale_faq_version")
    return row


def _covering_fragment(conn, revision_id: UUID, question: str, answer: str, conditions_key: str):
    if conditions_key != "{}":
        return None
    title = conn.execute(
        "SELECT title FROM source_revisions WHERE id = %s", (revision_id,),
    ).fetchone()["title"]
    fragments = conn.execute(
        "SELECT id, text, heading_path FROM source_fragments WHERE revision_id = %s ORDER BY ordinal",
        (revision_id,),
    ).fetchall()
    section = [
        fragment for fragment in fragments
        if fragment["heading_path"] and _key(fragment["heading_path"][-1]) == question
    ]
    if not section and _key(title) == question:
        section = [fragment for fragment in fragments if not fragment["heading_path"]]
    if len(section) != 1:
        return None
    entry = section[0]
    path = entry["heading_path"]
    if path and any(
        len(fragment["heading_path"]) > len(path) and fragment["heading_path"][:len(path)] == path
        for fragment in fragments
    ):
        return None
    lines = entry["text"].strip().splitlines()
    if entry["heading_path"] and lines and _key(lines[0]) == _key(entry["heading_path"][-1]):
        lines = lines[1:]
    text = "\n".join(lines)
    return (entry, text) if _key(text) == _key(answer) else None


def _public_document(conn, revision_id: UUID | None, *, hackathon: bool = False) -> dict | None:
    if revision_id is None:
        return None
    return conn.execute(
        """
        SELECT r.id, r.source_id
        FROM source_revisions r JOIN sources s ON s.id = r.source_id
        WHERE r.id = %s AND s.source_system = 'document' AND s.status = 'active'
            AND s.current_revision_id = r.id AND s.acl_actor_ids IS NULL
            AND (NOT %s OR (s.source_key = ANY(%s) AND r.acl_actor_ids IS NULL AND r.upstream_url IS NULL))
        """,
        (revision_id, hackathon, list(HACKATHON_DOCUMENT_SOURCES)),
    ).fetchone()


def _set_coverage(
    conn, row: dict, source_id: UUID | None, covered: UUID | None, conflict: UUID | None,
    *, bump: bool = True,
) -> None:
    if (row["coverage_source_id"], row["covered_revision_id"], row["conflict_revision_id"]) == (
        source_id, covered, conflict
    ):
        return
    conn.execute(
        """
        UPDATE faq_entries
        SET coverage_source_id = %s, covered_revision_id = %s, conflict_revision_id = %s,
            version = version + %s, updated_at = now()
        WHERE id = %s
        """,
        (source_id, covered, conflict, int(bump), row["id"]),
    )
    if row["status"] == "active":
        conn.execute(
            "UPDATE sources SET status = %s, updated_at = now() WHERE id = %s",
            ("archived" if covered else "active", row["document_id"]),
        )


def _matching_document(conn, question: str, *, hackathon: bool = False) -> UUID | None:
    # A matching heading/title is an explicit section boundary for comparing an
    # otherwise unlinked public document. Ambiguous associations require review.
    rows = conn.execute(
        """
        SELECT r.id, r.title, ARRAY(
            SELECT unnest(f.heading_path) FROM source_fragments f WHERE f.revision_id = r.id
        ) AS headings
        FROM sources s JOIN source_revisions r ON r.id = s.current_revision_id
        WHERE s.source_system = 'document' AND s.status = 'active' AND s.acl_actor_ids IS NULL
            AND (NOT %s OR (s.source_key = ANY(%s) AND r.acl_actor_ids IS NULL AND r.upstream_url IS NULL))
        """,
        (hackathon, list(HACKATHON_DOCUMENT_SOURCES)),
    ).fetchall()
    matches = [
        row["id"] for row in rows if any(_key(name) == question for name in [row["title"], *row["headings"]])
    ]
    return matches[0] if len(matches) == 1 else None


def _document_questions(conn, revision_id: UUID) -> list[str]:
    row = conn.execute(
        """
        SELECT r.title, ARRAY(
            SELECT unnest(f.heading_path) FROM source_fragments f WHERE f.revision_id = r.id
        ) AS headings
        FROM source_revisions r WHERE r.id = %s
        """,
        (revision_id,),
    ).fetchone()
    return list({_key(name) for name in [row["title"], *row["headings"]]})


def publish_faq(conn, request: dict, text: str, *, replace: bool = False, hackathon: bool = False) -> FaqMatch | None:
    """Publish a new immutable source revision using the caller's locked transaction."""
    text = clean_public_text(text)
    if not text:
        raise MailError(422, "empty_public_excerpt")
    row = conn.execute(
        """
        SELECT f.*, fragment.text FROM faq_entries f
        JOIN source_fragments fragment ON fragment.id = f.current_fragment_id
        WHERE f.request_id = %s FOR UPDATE OF f
        """,
        (request["id"],),
    ).fetchone()
    if row and row["status"] == "withdrawn" and not replace:
        return None
    if row and row["status"] == "active" and row["text"] == text:
        if row["coverage_source_id"] is not None:
            current = conn.execute(
                "SELECT current_revision_id FROM sources WHERE id = %s",
                (row["coverage_source_id"],),
            ).fetchone()["current_revision_id"]
            reconcile_document_revision(conn, current, hackathon=hackathon)
            row = {**row, **conn.execute("SELECT * FROM faq_entries WHERE id = %s", (row["id"],)).fetchone()}
        return _match({**row, "pending": request["pending"]})
    faq_id = row["id"] if row else uuid4()
    document_id = row["document_id"] if row else uuid4()
    version = row["version"] + 1 if row else 1
    revision_id, fragment_id = uuid4(), uuid4()
    question = _key(request["question"])
    conditions = _conditions_key(request["conditions"])
    if row is None:
        conn.execute(
            "INSERT INTO sources(id, source_system, source_key) VALUES (%s, 'faq', %s)",
            (document_id, str(faq_id)),
        )
    conn.execute(
        """
        INSERT INTO source_revisions (id, source_id, version, title, checksum, indexed_at)
        VALUES (%s, %s, %s, %s, %s, now())
        """,
        (
            revision_id, document_id, version,
            "Официальное разъяснение",
            sha256(text.encode("utf-8")).hexdigest(),
        ),
    )
    conn.execute(
        """
        INSERT INTO source_fragments (id, revision_id, ordinal, anchor, text)
        VALUES (%s, %s, 0, 'official-answer', %s)
        """,
        (fragment_id, revision_id, text),
    )
    conn.execute(
        "UPDATE sources SET current_revision_id = %s, status = 'active', updated_at = now() WHERE id = %s",
        (revision_id, document_id),
    )
    current = conn.execute(
        """
        INSERT INTO faq_entries (id, request_id, question_key, conditions_key, version, status,
                                 document_id, current_revision_id, current_fragment_id)
        VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s)
        ON CONFLICT (request_id) DO UPDATE SET
            version = excluded.version, status = excluded.status, updated_at = now(),
            current_revision_id = excluded.current_revision_id,
            current_fragment_id = excluded.current_fragment_id
        RETURNING *
        """,
        (faq_id, request["id"], question, conditions, version, document_id, revision_id, fragment_id),
    ).fetchone()
    source_id = current["coverage_source_id"]
    if source_id:
        candidate = conn.execute(
            "SELECT current_revision_id FROM sources WHERE id = %s", (source_id,)
        ).fetchone()["current_revision_id"]
    else:
        candidate = _matching_document(conn, question, hackathon=hackathon) if not request["conditions"] else None
    document = _public_document(conn, candidate, hackathon=hackathon)
    covered = conflict = None
    if document:
        source_id = document["source_id"]
        if not request["pending"] and _covering_fragment(conn, document["id"], question, text, conditions):
            covered = document["id"]
        else:
            conflict = document["id"]
    if covered:
        conn.execute("UPDATE sources SET status = 'archived' WHERE id = %s", (document_id,))
    _set_coverage(conn, current, source_id, covered, conflict, bump=False)
    return FaqMatch(
        faq_id, version, document_id, revision_id, fragment_id, text, conflict, bool(request["pending"])
    )


def set_coverage(conn, faq_id: UUID, version: int, revision_id: UUID, *, hackathon: bool = False) -> dict:
    lock_sources(conn)
    row = _metadata(conn, faq_id, version, hackathon=hackathon)
    if row["status"] != "active":
        raise MailError(409, "faq_withdrawn")
    document = _public_document(conn, revision_id, hackathon=hackathon)
    if document is None:
        raise MailError(409, "covering_revision_not_current_public_document")
    match = conn.execute(
        """
        SELECT r.pending, f.text FROM mail_requests r CROSS JOIN source_fragments f
        WHERE r.id = %s AND f.id = %s
        """,
        (row["request_id"], row["current_fragment_id"]),
    ).fetchone()
    if match["pending"]:
        raise MailError(409, "partial_faq_cannot_be_covered")
    if not _covering_fragment(conn, revision_id, row["question_key"], match["text"], row["conditions_key"]):
        raise MailError(409, "covering_revision_not_equivalent")
    _set_coverage(conn, row, document["source_id"], revision_id, None)
    return conn.execute("SELECT * FROM faq_entries WHERE id = %s", (faq_id,)).fetchone()


def withdraw_faq(conn, faq_id: UUID, version: int, *, hackathon: bool = False) -> dict:
    lock_sources(conn)
    row = _metadata(conn, faq_id, version, hackathon=hackathon)
    if row["status"] == "withdrawn":
        return row
    conn.execute(
        "UPDATE sources SET status = 'archived', updated_at = now() WHERE id = %s",
        (row["document_id"],),
    )
    return conn.execute(
        """
        UPDATE faq_entries SET status = 'withdrawn', version = version + 1, updated_at = now(),
            covered_revision_id = NULL, conflict_revision_id = NULL
        WHERE id = %s RETURNING *
        """,
        (faq_id,),
    ).fetchone()


def replace_faq(conn, faq_id: UUID, version: int, text: str, *, hackathon: bool = False) -> FaqMatch:
    lock_sources(conn)
    row = _metadata(conn, faq_id, version, hackathon=hackathon)
    request = conn.execute(
        "SELECT * FROM mail_requests WHERE id = %s", (row["request_id"],)
    ).fetchone()
    if request["state"] == "withdrawn":
        conn.execute(
            "UPDATE mail_requests SET state = 'resolved', updated_at = now() WHERE id = %s",
            (request["id"],),
        )
    return publish_faq(conn, request, text, replace=True, hackathon=hackathon)


def reconcile_document_revision(
    conn, new_revision_id: UUID | None, previous_revision_id: UUID | None = None, *, hackathon: bool = False,
) -> None:
    """Keep a FAQ hidden only while its current public covering revision is equivalent."""
    lock_sources(conn)
    reference = new_revision_id or previous_revision_id
    if reference is None:
        return
    source = conn.execute(
        "SELECT source_id FROM source_revisions WHERE id = %s", (reference,),
    ).fetchone()
    if source is None:
        return
    source_id = source["source_id"]
    document = _public_document(conn, new_revision_id, hackathon=hackathon)
    questions = _document_questions(conn, document["id"]) if document else []
    rows = conn.execute(
        """
        SELECT f.*, fragment.text, request.pending
        FROM faq_entries f
        JOIN source_fragments fragment ON fragment.id = f.current_fragment_id
        JOIN mail_requests request ON request.id = f.request_id
        WHERE f.status = 'active' AND (NOT %s OR request.recipient = %s) AND (
            f.coverage_source_id = %s OR (
                f.coverage_source_id IS NULL AND f.conditions_key = '{}' AND f.question_key = ANY(%s)
            )
        )
        FOR UPDATE OF f
        """,
        (hackathon, HACKATHON_QUALITY_EMAIL, source_id, questions),
    ).fetchall()
    for row in rows:
        covered = conflict = None
        if document:
            if row["coverage_source_id"] is None and _matching_document(
                conn, row["question_key"], hackathon=hackathon,
            ) != document["id"]:
                continue
            if not row["pending"] and _covering_fragment(
                conn, document["id"], row["question_key"], row["text"], row["conditions_key"],
            ):
                covered = document["id"]
            else:
                conflict = document["id"]
        _set_coverage(conn, row, source_id, covered, conflict)
