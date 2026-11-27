"""Immutable revision staging and employee-visible source lookup."""

from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from backend.config import HACKATHON_DOCUMENT_SOURCES, HACKATHON_QUALITY_EMAIL
from backend.contracts.engine import SourceFragment, SourceKind

from .fragments import extract_fragments
from .ports import IndexFragment, IndexRevision, PageSnapshot, PageStatus


class SnapshotError(RuntimeError):
    """A document source returned inconsistent versioned data."""


_HACKATHON_SOURCE = """
    AND (NOT %s OR (s.source_system = 'document' AND s.source_key = ANY(%s)
        AND s.acl_actor_ids IS NULL AND r.acl_actor_ids IS NULL AND r.upstream_url IS NULL) OR EXISTS (
        SELECT 1 FROM faq_entries faq JOIN mail_requests mail ON mail.id = faq.request_id
        WHERE faq.document_id = s.id AND mail.recipient = %s
    ))
"""


def _hackathon_filter(hackathon: bool) -> tuple:
    return hackathon, list(HACKATHON_DOCUMENT_SOURCES), HACKATHON_QUALITY_EMAIL


def list_fragments(
    conn, actor_id: UUID | str, *, kind: SourceKind | None = None, hackathon: bool = False,
) -> tuple[SourceFragment, ...]:
    """Read the latest indexed revisions available to an authenticated employee."""
    rows = conn.execute(
        f"""
        SELECT s.id AS document_id, r.id AS revision_id, f.id AS fragment_id,
               f.text, r.title, f.anchor, s.source_system AS kind
        FROM sources AS s
        JOIN source_revisions AS r ON r.id = s.current_revision_id
        JOIN source_fragments AS f ON f.revision_id = r.id
        WHERE s.status = 'active' AND r.indexed_at IS NOT NULL
            AND (%s::text IS NULL OR s.source_system = %s)
            AND (s.acl_actor_ids IS NULL OR %s = ANY(s.acl_actor_ids))
            AND (s.source_system = 'document' OR r.acl_actor_ids IS NULL OR %s = ANY(r.acl_actor_ids))
            {_HACKATHON_SOURCE}
        ORDER BY s.id, f.ordinal
        """,
        (kind, kind, str(actor_id), str(actor_id), *_hackathon_filter(hackathon)),
    ).fetchall()
    return tuple(SourceFragment(**row) for row in rows)


def get_fragment(
    conn, revision_id: UUID, fragment_id: UUID, actor_id: UUID | str, *, current: bool = False,
    hackathon: bool = False,
) -> SourceFragment | None:
    """Resolve an indexed citation under current source access and historical ACLs."""
    row = conn.execute(
        f"""
        SELECT s.id AS document_id, r.id AS revision_id, f.id AS fragment_id,
               f.text, r.title, f.anchor, s.source_system AS kind
        FROM source_fragments AS f
        JOIN source_revisions AS r ON r.id = f.revision_id
        JOIN sources AS s ON s.id = r.source_id
        WHERE r.id = %s AND f.id = %s AND r.indexed_at IS NOT NULL
            AND (s.status = 'active' OR s.source_system = 'faq')
            AND (NOT %s OR (s.status = 'active' AND s.current_revision_id = r.id))
            AND (s.acl_actor_ids IS NULL OR %s = ANY(s.acl_actor_ids))
            AND (r.acl_actor_ids IS NULL OR %s = ANY(r.acl_actor_ids)
                 OR (s.source_system = 'document' AND r.id = s.current_revision_id))
            {_HACKATHON_SOURCE}
        """,
        (revision_id, fragment_id, current, str(actor_id), str(actor_id), *_hackathon_filter(hackathon)),
    ).fetchone()
    return SourceFragment(**row) if row else None


def actor_ids(snapshot: PageSnapshot) -> list[str] | None:
    return None if snapshot.acl_actor_ids is None else sorted(snapshot.acl_actor_ids)


def is_local_public_page(conn, source_key: str, external_id: str, snapshot: PageSnapshot | None) -> bool:
    """Check local provenance and the latest version at which access was denied."""
    if snapshot is not None and (snapshot.acl_actor_ids is not None or snapshot.upstream_url is not None):
        return False
    return conn.execute(
        """
        SELECT 1 FROM sources s WHERE s.source_system = 'document' AND s.source_key = %s
            AND s.external_id = %s AND (s.acl_actor_ids IS NOT NULL OR EXISTS (
                SELECT 1 FROM source_revisions r WHERE r.source_id = s.id
                    AND (r.acl_actor_ids IS NOT NULL OR r.upstream_url IS NOT NULL)
            ))
        UNION ALL
        SELECT 1 FROM document_demo_access_cutoffs
        WHERE source_key = %s AND external_id = %s AND blocked_through_version >= %s
        LIMIT 1
        """,
        (source_key, external_id, source_key, external_id, snapshot.version if snapshot else None),
    ).fetchone() is None


def record_demo_access_cutoff(conn, source_key: str, external_id: str, version: int) -> None:
    conn.execute(
        """
        INSERT INTO document_demo_access_cutoffs (source_key, external_id, blocked_through_version)
        VALUES (%s, %s, %s)
        ON CONFLICT (source_key, external_id) DO UPDATE SET blocked_through_version = greatest(
            document_demo_access_cutoffs.blocked_through_version, excluded.blocked_through_version
        )
        """,
        (source_key, external_id, version),
    )


def stage_revision(conn, source_key: str, snapshot: PageSnapshot) -> tuple[IndexRevision, bool]:
    checksum = sha256(snapshot.storage_html.encode('utf-8')).hexdigest()
    conn.execute(
        """
        INSERT INTO sources (id, source_system, source_key, external_id)
        VALUES (%s, 'document', %s, %s) ON CONFLICT (source_system, source_key, external_id) DO NOTHING
        """,
        (uuid4(), source_key, snapshot.external_id),
    )
    source_id = conn.execute(
        """
        SELECT id FROM sources WHERE source_system = 'document' AND source_key = %s AND external_id = %s
        """,
        (source_key, snapshot.external_id),
    ).fetchone()['id']
    row = conn.execute(
        'SELECT * FROM source_revisions WHERE source_id = %s AND version = %s',
        (source_id, snapshot.version),
    ).fetchone()
    if row is not None:
        if row['checksum'] != checksum or row['title'] != snapshot.title:
            raise SnapshotError('A source returned conflicting content for an existing version')
        stored_acl = row['acl_actor_ids']
        stored_snapshot = PageSnapshot(snapshot.external_id, row['version'], row['title'], row['storage_body'],
                                       row['space_key'], row['upstream_url'],
                                       None if stored_acl is None else frozenset(stored_acl))
        fragments = tuple(
            IndexFragment(fragment['id'], fragment['anchor'], tuple(fragment['heading_path']), fragment['text'])
            for fragment in conn.execute(
                'SELECT id, anchor, heading_path, text FROM source_fragments WHERE revision_id = %s ORDER BY ordinal',
                (row['id'],),
            )
        )
        return IndexRevision(source_id, row['id'], stored_snapshot, fragments, checksum), row['indexed_at'] is not None

    revision_id = uuid4()
    conn.execute(
        """
        INSERT INTO source_revisions
            (id, source_id, version, title, storage_body, checksum, upstream_url, space_key, acl_actor_ids)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (revision_id, source_id, snapshot.version, snapshot.title, snapshot.storage_html, checksum,
         snapshot.upstream_url, snapshot.space_key, actor_ids(snapshot)),
    )
    fragments = tuple(
        IndexFragment(uuid5(revision_id, fragment.anchor), fragment.anchor, fragment.heading_path, fragment.text)
        for fragment in extract_fragments(snapshot.storage_html)
    )
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO source_fragments (id, revision_id, ordinal, anchor, heading_path, text)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [(part.fragment_id, revision_id, ordinal, part.anchor, list(part.heading_path), part.text)
             for ordinal, part in enumerate(fragments)],
        )
    return IndexRevision(source_id, revision_id, snapshot, fragments, checksum), False


def activate_revision(conn, revision: IndexRevision, snapshot: PageSnapshot, on_revision_change) -> None:
    current = conn.execute(
        """
        SELECT s.status, r.id, r.version, s.acl_actor_ids, s.space_key
        FROM sources AS s LEFT JOIN source_revisions AS r ON r.id = s.current_revision_id
        WHERE s.id = %s
        """,
        (revision.document_id,),
    ).fetchone()
    acl = actor_ids(snapshot)
    if current['version'] is not None and current['version'] > snapshot.version:
        return
    if (current['status'] == 'active' and current['id'] == revision.revision_id
            and current['acl_actor_ids'] == acl and current['space_key'] == snapshot.space_key):
        return
    conn.execute(
        """
        UPDATE sources SET current_revision_id = %s, status = 'active', space_key = %s, acl_actor_ids = %s,
            updated_at = clock_timestamp() WHERE id = %s
        """,
        (revision.revision_id, snapshot.space_key, acl, revision.document_id),
    )
    previous_shared = current['id'] if current['status'] == 'active' and current['acl_actor_ids'] is None else None
    current_shared = revision.revision_id if acl is None else None
    if previous_shared != current_shared:
        on_revision_change(conn, current_shared, previous_shared)


def restrict_current_access(conn, source_key: str, snapshot: PageSnapshot, on_revision_change) -> None:
    acl = snapshot.acl_actor_ids
    if acl is None:
        return
    row = conn.execute(
        """
        SELECT s.id, s.current_revision_id, s.acl_actor_ids
        FROM sources AS s JOIN source_revisions AS r ON r.id = s.current_revision_id
        WHERE s.source_system = 'document' AND s.source_key = %s AND s.external_id = %s
            AND s.status = 'active' AND r.version <= %s
        """,
        (source_key, snapshot.external_id, snapshot.version),
    ).fetchone()
    if row is None:
        return
    previous = row['acl_actor_ids']
    allowed = sorted(acl if previous is None else acl.intersection(previous))
    if allowed == previous:
        return
    conn.execute(
        'UPDATE sources SET acl_actor_ids = %s, updated_at = clock_timestamp() WHERE id = %s',
        (allowed, row['id']),
    )
    if previous is None:
        on_revision_change(conn, None, row['current_revision_id'])


def deactivate_page(
    conn, source_key: str, external_id: str, outcome: PageStatus, on_revision_change,
    *, through_version: int | None = None,
) -> None:
    row = conn.execute(
        """
        SELECT s.id, s.current_revision_id, s.status, s.acl_actor_ids FROM sources s
        LEFT JOIN source_revisions r ON r.id = s.current_revision_id
        WHERE s.source_system = 'document' AND s.source_key = %s AND s.external_id = %s AND s.status <> %s
            AND (%s::bigint IS NULL OR r.version <= %s)
        """,
        (source_key, external_id, outcome.value, through_version, through_version),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        'UPDATE sources SET status = %s, updated_at = clock_timestamp() WHERE id = %s',
        (outcome.value, row['id']),
    )
    if row['status'] == 'active' and row['acl_actor_ids'] is None:
        on_revision_change(conn, None, row['current_revision_id'])
