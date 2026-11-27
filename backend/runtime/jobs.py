"""PostgreSQL queue and fenced worker lifecycle."""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection, Error
from psycopg.types.json import Jsonb

from backend.db import connect


class QueueFull(Exception):
    """Admission was refused before the caller's transaction committed."""

    def __init__(self, queue: str, limit: int):
        self.queue = queue
        self.limit = limit
        super().__init__(f"{queue} queue reached its limit of {limit} active jobs")


class LeaseLost(Exception):
    """The caller can no longer commit effects under this lease."""


class PermanentJobFailure(Exception):
    """The handler determined that another attempt cannot resolve its failure."""


@dataclass(frozen=True)
class Job:
    id: UUID
    queue: str
    kind: str
    business_key: str
    payload: dict[str, Any]
    state: str
    attempt: int
    max_attempts: int
    available_at: datetime
    deadline_at: datetime | None
    lease_token: UUID | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    first_started_at: datetime | None
    finished_at: datetime | None


Handler = Callable[[Job], None]
TerminalHandler = Callable[[Connection, Job, str], None]

DOCUMENT_JOB_SCOPE = """
    AND (%(document_source_keys)s::text[] IS NULL OR jobs.kind <> 'documents.sync' OR EXISTS (
        SELECT 1 FROM document_sync_runs r WHERE r.id = (jobs.payload ->> 'run_id')::uuid
            AND r.source_key = ANY(%(document_source_keys)s::text[])
    ))
"""


def _lock_queue(conn: Connection, queue: str) -> None:
    # The lock serializes admission counting and claim capacity checks in this queue.
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || %s, 0))",
        (f":job-queue:{queue}",),
    )


def enqueue(
    conn: Connection,
    *,
    queue: str,
    kind: str,
    key: str,
    payload: dict[str, Any],
    deadline_at: datetime | None = None,
    max_attempts: int = 3,
    max_pending: int | None = None,
    available_at: datetime | None = None,
) -> UUID:
    """Set up durable work as part of the caller's transaction.

    The admission limit includes running jobs. Repeating a business key returns
    the original job even at capacity.
    """
    _lock_queue(conn, queue)
    existing = conn.execute(
        "SELECT id FROM jobs WHERE queue = %s AND kind = %s AND business_key = %s",
        (queue, kind, key),
    ).fetchone()
    if existing:
        return existing["id"]
    if max_pending is not None:
        count = conn.execute(
            "SELECT count(*) AS count FROM jobs WHERE queue = %s AND state IN ('queued', 'running')",
            (queue,),
        ).fetchone()["count"]
        if count >= max_pending:
            raise QueueFull(queue, max_pending)
    job_id = uuid4()
    conn.execute(
        """
        INSERT INTO jobs (id, queue, kind, business_key, payload, deadline_at, max_attempts, available_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()))
        """,
        (job_id, queue, kind, key, Jsonb(payload), deadline_at, max_attempts, available_at),
    )
    return job_id


def _retry_delay(attempt: int, base_retry_seconds: float) -> timedelta:
    return timedelta(seconds=min(base_retry_seconds * 2 ** min(max(attempt - 1, 0), 20), 300))


def _transition_after_failure(
    conn: Connection,
    job: Job,
    reason: str,
    *,
    terminal: TerminalHandler | None,
    force_terminal: bool = False,
    retry_from: datetime,
    base_retry_seconds: float,
) -> None:
    if force_terminal or job.attempt >= job.max_attempts:
        conn.execute(
            """
            UPDATE jobs SET state = 'failed', lease_token = NULL, lease_expires_at = NULL,
                finished_at = clock_timestamp(), last_error = %s
            WHERE id = %s
            """,
            (reason, job.id),
        )
        if terminal:
            terminal(conn, job, reason)
        return
    available_at = retry_from + _retry_delay(job.attempt, base_retry_seconds)
    if job.deadline_at:
        available_at = min(available_at, job.deadline_at)
    conn.execute(
        """
        UPDATE jobs SET state = 'queued', lease_token = NULL, lease_expires_at = NULL,
            available_at = %s, last_error = %s
        WHERE id = %s
        """,
        (available_at, reason, job.id),
    )


def recover_jobs(
    dsn: str | None,
    queue: str,
    *,
    base_retry_seconds: float = 1,
    terminal_handlers: Mapping[str, TerminalHandler] | None = None,
    batch_size: int = 100,
    document_source_keys: Collection[str] | None = None,
) -> int:
    """Retry expired leases and publish terminal outcomes for overdue jobs."""
    with connect(dsn) as conn:
        rows = conn.execute(
            f"""
            SELECT jobs.*, clock_timestamp() AS observed_at FROM jobs
            WHERE queue = %(queue)s AND (
                (state = 'running' AND lease_expires_at <= clock_timestamp()) OR
                (state = 'queued' AND deadline_at <= clock_timestamp())
            )
            {DOCUMENT_JOB_SCOPE}
            ORDER BY coalesce(lease_expires_at, deadline_at), id LIMIT %(limit)s
            FOR UPDATE SKIP LOCKED
            """,
            {'queue': queue, 'limit': batch_size, 'document_source_keys': document_source_keys},
        ).fetchall()
        for row in rows:
            now = row.pop("observed_at")
            job = Job(**row)
            overdue = job.deadline_at is not None and job.deadline_at <= now
            _transition_after_failure(
                conn,
                job,
                "deadline_exceeded" if overdue else "lease_expired",
                terminal=(terminal_handlers or {}).get(job.kind),
                force_terminal=overdue,
                retry_from=job.lease_expires_at or now,
                base_retry_seconds=base_retry_seconds,
            )
    return len(rows)


def claim(
    dsn: str | None,
    queue: str,
    *,
    max_running: int | None = None,
    lease_seconds: float = 30,
    active_kinds: Collection[str] | None = None,
    document_source_keys: Collection[str] | None = None,
) -> Job | None:
    """Claim ready work with a fresh token, optionally restricted to active kinds."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    with connect(dsn) as conn:
        _lock_queue(conn, queue)
        if max_running is not None:
            count = conn.execute(
                """
                SELECT count(*) AS count FROM jobs WHERE queue = %s AND state = 'running'
                    AND lease_expires_at > clock_timestamp()
                """,
                (queue,),
            ).fetchone()["count"]
            if count >= max_running:
                return None
        row = conn.execute(
            f"""
            WITH next_job AS (
                SELECT id FROM jobs WHERE queue = %(queue)s AND state = 'queued'
                    AND available_at <= clock_timestamp()
                    AND (deadline_at IS NULL OR deadline_at > clock_timestamp())
                    AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s))
                    {DOCUMENT_JOB_SCOPE}
                ORDER BY available_at, created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs SET state = 'running', attempt = attempt + 1, lease_token = %(token)s,
                lease_expires_at = least(deadline_at, clock_timestamp() + %(duration)s),
                first_started_at = coalesce(first_started_at, clock_timestamp())
            FROM next_job WHERE jobs.id = next_job.id RETURNING jobs.*
            """,
            {
                "queue": queue,
                "kinds": list(active_kinds) if active_kinds is not None else None,
                "token": uuid4(),
                "duration": timedelta(seconds=lease_seconds),
                "document_source_keys": document_source_keys,
            },
        ).fetchone()
        return Job(**row) if row else None


def require_lease(conn: Connection, job: Job) -> Job:
    """Hold the current lease row until the caller commits a job effect."""
    row = conn.execute(
        """
        SELECT * FROM jobs WHERE id = %s AND state = 'running' AND lease_token = %s
        FOR UPDATE
        """,
        (job.id, job.lease_token),
    ).fetchone()
    if row is None:
        raise LeaseLost(job.id)
    if row["lease_expires_at"] <= conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]:
        raise LeaseLost(job.id)
    return Job(**row)


def heartbeat(dsn: str | None, job: Job, *, lease_seconds: float = 30) -> Job:
    """Extend an active lease without exceeding the job deadline."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    with connect(dsn) as conn:
        require_lease(conn, job)
        row = conn.execute(
            """
            UPDATE jobs SET lease_expires_at = least(
                deadline_at, greatest(lease_expires_at, clock_timestamp() + %s)
            ) WHERE id = %s RETURNING *
            """,
            (timedelta(seconds=lease_seconds), job.id),
        ).fetchone()
        return Job(**row)


def _keep_lease(dsn: str | None, job: Job, lease_seconds: float, stop: Event) -> None:
    interval = lease_seconds / 3
    while not stop.wait(interval):
        try:
            heartbeat(dsn, job, lease_seconds=lease_seconds)
        except LeaseLost:
            return
        except Error:
            # On a temporary DB outage the lease still expires independently.
            # A later renewal succeeds only if the original token remains live.
            continue


def finish(conn: Connection, job: Job) -> None:
    """Finish successfully, optionally in the same transaction as the result."""
    require_lease(conn, job)
    conn.execute(
        """
        UPDATE jobs SET state = 'succeeded', lease_token = NULL, lease_expires_at = NULL,
            finished_at = clock_timestamp(), last_error = NULL
        WHERE id = %s
        """,
        (job.id,),
    )


def run_once(
    dsn: str | None,
    queue: str,
    handlers: Mapping[str, Handler],
    *,
    max_running: int | None = None,
    lease_seconds: float = 30,
    base_retry_seconds: float = 1,
    terminal_handlers: Mapping[str, TerminalHandler] | None = None,
    active_kinds: Collection[str] | None = None,
    document_source_keys: Collection[str] | None = None,
) -> Job | None:
    """Recover expired work in the queue, then claim and run one eligible job.

    Pass active_kinds to park jobs for disabled integrations until a worker
    enables their kind. Deadline recovery applies to the whole queue.
    """
    from .journal import recover_journal

    recover_jobs(dsn, queue, base_retry_seconds=base_retry_seconds, terminal_handlers=terminal_handlers,
                 document_source_keys=document_source_keys)
    recover_journal(dsn)
    job = claim(
        dsn, queue, max_running=max_running, lease_seconds=lease_seconds, active_kinds=active_kinds,
        document_source_keys=document_source_keys,
    )
    if job is None:
        return None

    handler = handlers.get(job.kind)
    reason = None
    permanent = False
    if handler is None:
        reason, permanent = "unknown_job_kind", True
    else:
        stop = Event()
        renewer = Thread(target=_keep_lease, args=(dsn, job, lease_seconds, stop), daemon=True)
        renewer.start()
        try:
            handler(job)
        except LeaseLost:
            return job
        except Exception as error:
            reason = type(error).__name__
            permanent = isinstance(error, PermanentJobFailure)
        finally:
            stop.set()
            renewer.join(timeout=min(lease_seconds / 3, 1))

    try:
        with connect(dsn) as conn:
            if reason is None:
                finish(conn, job)
            else:
                current = require_lease(conn, job)
                now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                _transition_after_failure(
                    conn,
                    current,
                    reason,
                    terminal=(terminal_handlers or {}).get(current.kind),
                    force_terminal=permanent,
                    retry_from=now,
                    base_retry_seconds=base_retry_seconds,
                )
    except LeaseLost:
        # A handler may have finished atomically, or another worker now owns the job.
        pass
    return job
