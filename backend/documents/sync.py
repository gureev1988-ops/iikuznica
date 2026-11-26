"""Durable document indexing, activation and inventory reconciliation."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from backend.db import connect, lock_source_catalog

from .catalog import SnapshotError, activate_revision, deactivate_page, restrict_current_access, stage_revision
from .confluence import ConfluenceError
from .ports import DocumentSource, PageProbe, PageSnapshot, PageStatus, RevisionIndexer


SYNC_JOB_KIND = 'documents.sync'


class SyncBusy(RuntimeError):
    """Another worker holds the source's synchronization session."""


@dataclass(frozen=True)
class SyncResult:
    id: UUID
    source_key: str
    status: Literal['queued', 'running', 'incomplete', 'completed']
    cursor: str | None
    batch_offset: int
    listing_done: bool
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    counts: dict[str, int]


def _reconcile_faq(conn, new_revision_id: UUID | None, previous_revision_id: UUID | None) -> None:
    from backend.mailfaq import reconcile_document_revision

    reconcile_document_revision(conn, new_revision_id, previous_revision_id)


def _session_lock(conn, source_key: str) -> None:
    locked = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(current_schema()), hashtext(%s)) AS locked",
        (f'documents:scan:{source_key}',),
    ).fetchone()['locked']
    if not locked:
        raise SyncBusy('A synchronization is already running for this source')


def _check_source_key(source_key: str) -> None:
    if not source_key or len(source_key) > 100 or any(char not in '._-' and not char.isalnum() for char in source_key):
        raise ValueError('Invalid document source key')


class DocumentSync:
    def __init__(
        self, dsn: str | None, sources: Mapping[str, DocumentSource], *, indexer: RevisionIndexer | None = None,
        on_revision_change: Callable | None = None,
    ):
        for source_key in sources:
            _check_source_key(source_key)
        self.dsn = dsn
        self.sources = dict(sources)
        self.indexer = indexer
        self.on_revision_change = on_revision_change or _reconcile_faq

    def request_sync(self, source_key: str) -> UUID:
        """Create or resume an inventory run and queue it in one transaction."""
        from backend.runtime import enqueue

        if source_key not in self.sources:
            raise KeyError(source_key)
        with connect(self.dsn) as conn:
            conn.execute(
                'SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(%s))',
                (f'documents:request:{source_key}',),
            )
            run = conn.execute(
                "SELECT id FROM document_sync_runs WHERE source_key = %s AND status <> 'completed'",
                (source_key,),
            ).fetchone()
            if run is None:
                run_id = uuid4()
                conn.execute(
                    "INSERT INTO document_sync_runs (id, source_key, status) VALUES (%s, %s, 'queued')",
                    (run_id, source_key),
                )
            else:
                run_id = run['id']
                job = conn.execute(
                    """
                    SELECT id FROM jobs WHERE queue = 'background' AND kind = %s AND payload ->> 'run_id' = %s
                        AND state IN ('queued', 'running') LIMIT 1
                    """,
                    (SYNC_JOB_KIND, str(run_id)),
                ).fetchone()
                if job is not None:
                    return run_id
                conn.execute(
                    """
                    UPDATE document_sync_runs SET status = 'queued', error = NULL, finished_at = NULL
                    WHERE id = %s AND status <> 'completed'
                    """,
                    (run_id,),
                )
            enqueue(conn, queue='background', kind=SYNC_JOB_KIND, key=str(uuid4()), payload={'run_id': str(run_id)})
        return run_id

    def result(self, run_id: UUID) -> SyncResult | None:
        with connect(self.dsn) as conn:
            run = conn.execute(
                """
                SELECT r.*, (
                    SELECT jsonb_object_agg(c.outcome, c.total) FROM (
                        SELECT outcome, count(*) AS total FROM document_sync_seen
                        WHERE run_id = r.id GROUP BY outcome
                    ) AS c
                ) AS counts FROM document_sync_runs AS r WHERE r.id = %s
                """,
                (run_id,),
            ).fetchone()
        if run is None:
            return None
        run['counts'] = {status.value: (run['counts'] or {}).get(status.value, 0) for status in PageStatus}
        return SyncResult(**run)

    def handle_job(self, job) -> None:
        from backend.runtime import heartbeat, require_lease

        self.run(UUID(job.payload['run_id']), check_lease=lambda conn: require_lease(conn, job),
                 renew=lambda: heartbeat(self.dsn, job, lease_seconds=60))

    @staticmethod
    def terminal_job(conn, job, reason: str) -> None:
        conn.execute(
            """
            UPDATE document_sync_runs SET status = 'incomplete', finished_at = clock_timestamp(), error = coalesce(error, %s)
            WHERE id = %s AND status <> 'completed'
            """,
            (reason, UUID(job.payload['run_id'])),
        )

    def run(
        self, run_id: UUID, *, check_lease: Callable | None = None, renew: Callable | None = None,
    ) -> SyncResult:
        """Resume a run using its last durable list and page checkpoints."""
        with connect(self.dsn) as conn:
            conn.autocommit = True
            run = conn.execute('SELECT * FROM document_sync_runs WHERE id = %s', (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run['status'] != 'completed':
                source_key = run['source_key']
                _session_lock(conn, source_key)
                try:
                    with conn.transaction():
                        if check_lease:
                            check_lease(conn)
                        run = conn.execute(
                            """
                            UPDATE document_sync_runs SET status = 'running', error = NULL, finished_at = NULL,
                                started_at = coalesce(started_at, clock_timestamp())
                            WHERE id = %s AND status <> 'completed' RETURNING *
                            """,
                            (run_id,),
                        ).fetchone()
                    if run is not None:
                        self._scan(conn, run, self.sources[source_key], check_lease, renew)
                except Exception as exc:
                    error = str(exc) if isinstance(exc, (ConfluenceError, SnapshotError)) else type(exc).__name__
                    with conn.transaction():
                        if check_lease:
                            check_lease(conn)
                        conn.execute(
                            """
                            UPDATE document_sync_runs SET status = 'incomplete', error = %s,
                                finished_at = clock_timestamp() WHERE id = %s AND status <> 'completed'
                            """,
                            (error, run_id),
                        )
                    raise
        result = self.result(run_id)
        if result is None:
            raise KeyError(run_id)
        return result

    def _scan(self, conn, run: dict, source: DocumentSource, check_lease, renew) -> None:
        run_id = run['id']
        source_key = run['source_key']
        cursor = run['cursor']
        visited_cursors = set()
        while not run['listing_done']:
            if cursor in visited_cursors:
                raise SnapshotError('Document listing repeated a pagination cursor')
            visited_cursors.add(cursor)
            if renew:
                renew()
            batch = source.list_pages(cursor)
            # Re-read the interrupted batch. Remote offset lists can shift, so use
            # durable external IDs as checkpoints rather than skipping a fixed prefix.
            for offset, entry in enumerate(batch.pages, start=1):
                seen = conn.execute(
                    'SELECT 1 FROM document_sync_seen WHERE run_id = %s AND external_id = %s',
                    (run_id, entry.external_id),
                ).fetchone()
                if seen is not None:
                    continue
                if isinstance(entry, PageSnapshot):
                    probe = PageProbe(PageStatus.ACTIVE, entry)
                else:
                    if renew:
                        renew()
                    probe = source.probe(entry.external_id)
                self._record_page(conn, source_key, run_id, entry.external_id, probe, offset, check_lease, renew)
            cursor = batch.next_cursor
            with conn.transaction():
                if check_lease:
                    check_lease(conn)
                run = conn.execute(
                    """
                    UPDATE document_sync_runs SET cursor = %s, batch_offset = 0, listing_done = %s
                    WHERE id = %s RETURNING *
                    """,
                    (cursor, cursor is None, run_id),
                ).fetchone()

        missing = conn.execute(
            """
            SELECT s.external_id FROM sources AS s
            WHERE s.source_system = 'document' AND s.source_key = %s AND NOT EXISTS (
                SELECT 1 FROM document_sync_seen AS seen WHERE seen.run_id = %s AND seen.external_id = s.external_id
            ) ORDER BY s.external_id
            """,
            (source_key, run_id),
        ).fetchall()
        for row in missing:
            external_id = row['external_id']
            if renew:
                renew()
            self._record_page(conn, source_key, run_id, external_id, source.probe(external_id), None, check_lease, renew)

        with conn.transaction():
            if check_lease:
                check_lease(conn)
            conn.execute(
                "UPDATE document_sync_runs SET status = 'completed', finished_at = clock_timestamp() WHERE id = %s",
                (run_id,),
            )

    def _record_page(self, conn, source_key, run_id, external_id, probe, offset, check_lease, renew) -> None:
        snapshot = probe.snapshot
        if snapshot is not None:
            if snapshot.external_id != external_id:
                raise SnapshotError('Document lookup returned a different external ID')
            revision = self._index(conn, source_key, snapshot, check_lease, renew)
        with conn.transaction():
            if check_lease:
                check_lease(conn)
            lock_source_catalog(conn, write=True)
            if snapshot is not None:
                self._publish(conn, revision, snapshot, activate=True)
            else:
                deactivate_page(conn, source_key, external_id, probe.status, self.on_revision_change)
            conn.execute(
                'INSERT INTO document_sync_seen (run_id, external_id, outcome) VALUES (%s, %s, %s)',
                (run_id, external_id, probe.status.value),
            )
            if offset is not None:
                conn.execute(
                    'UPDATE document_sync_runs SET batch_offset = greatest(%s, batch_offset) WHERE id = %s',
                    (offset, run_id),
                )

    def _index(self, conn, source_key, snapshot, check_lease=None, renew=None, *, activate=True):
        if activate and snapshot.acl_actor_ids is not None:
            # A known permission loss applies even while a newer body is still indexing.
            with conn.transaction():
                if check_lease:
                    check_lease(conn)
                lock_source_catalog(conn, write=True)
                restrict_current_access(conn, source_key, snapshot, self.on_revision_change)
        with conn.transaction():
            if check_lease:
                check_lease(conn)
            revision, indexed = stage_revision(conn, source_key, snapshot)
        if not indexed and self.indexer is not None:
            if renew:
                renew()
            self.indexer.index(revision)
        return revision

    def _publish(self, conn, revision, snapshot, *, activate):
        # Staging already persists the built-in fragment index. An external index
        # must also have acknowledged this stable revision before publication.
        conn.execute(
            'UPDATE source_revisions SET indexed_at = clock_timestamp() WHERE id = %s AND indexed_at IS NULL',
            (revision.revision_id,),
        )
        if activate:
            activate_revision(conn, revision, snapshot, self.on_revision_change)

    def import_batch(
        self, source_key: str, snapshots: Iterable[PageSnapshot], *, activate: bool = True,
    ) -> tuple[UUID, ...]:
        """Import versions incrementally without treating absent pages as removals."""
        _check_source_key(source_key)
        imported = []
        with connect(self.dsn) as conn:
            conn.autocommit = True
            _session_lock(conn, source_key)
            for snapshot in snapshots:
                indexed = self._index(conn, source_key, snapshot, activate=activate)
                with conn.transaction():
                    lock_source_catalog(conn, write=True)
                    self._publish(conn, indexed, snapshot, activate=activate)
                imported.append(indexed.revision_id)
        return tuple(imported)
