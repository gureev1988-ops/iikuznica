"""Document connection configuration, operator checks and periodic dispatch."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from backend.config import HACKATHON_DOCUMENT_SOURCES, Settings
from backend.db import connect

from .catalog import SnapshotError
from .confluence import ConfluenceConfig, ConfluenceError, ConfluenceSource
from .http_json import HttpJsonConfig, HttpJsonError, HttpJsonSource
from .ports import BatchSource, DocumentSource, PageSnapshot


SourceType = Literal['http_json', 'confluence', 'batch']
SCHEDULE_BATCH_SIZE = 16
_NEXT_DUE = """least(clock_timestamp() + make_interval(
    secs => least(interval_seconds, 253402214400)::double precision
), timestamptz '9999-12-31 00:00:00+00')"""


class ConnectionPaused(RuntimeError):
    """The connection is configured with dispatch and checks disabled."""


@dataclass(frozen=True)
class ConnectionConfig:
    kind: SourceType
    enabled: bool = True
    interval_seconds: int | None = None


def check_source_key(source_key: str) -> None:
    if (not isinstance(source_key, str) or not source_key or len(source_key) > 100
            or any(char not in '._-' and not char.isalnum() for char in source_key)):
        raise ValueError('Invalid document source key')


def _batch(pages) -> BatchSource:
    return BatchSource(TypeAdapter(list[PageSnapshot]).validate_python(pages))


_FACTORIES = {
    'http_json': lambda settings: HttpJsonSource(HttpJsonConfig(**settings)),
    'confluence': lambda settings: ConfluenceSource(ConfluenceConfig(**settings)),
    'batch': lambda settings: _batch(settings['pages']),
}


def configure_sources(settings: Settings) -> tuple[dict[str, DocumentSource], dict[str, ConnectionConfig]]:
    sources = {}
    config = {}
    legacy_batch = settings.integrations.get('batch')
    if legacy_batch is not None:
        sources['batch'] = _batch(legacy_batch)
        config['batch'] = ConnectionConfig('batch')
    if settings.hackathon:
        return sources, config
    if legacy_confluence := settings.integrations.get('confluence'):
        sources['confluence'] = _FACTORIES['confluence'](legacy_confluence)
        config['confluence'] = ConnectionConfig('confluence')

    entries = settings.integrations.get('document_sources', {})
    if not isinstance(entries, dict):
        raise ValueError('Document connections must be an object')
    for source_key, entry in entries.items():
        check_source_key(source_key)
        if source_key in HACKATHON_DOCUMENT_SOURCES or source_key in sources:
            raise ValueError('Document connection key is reserved or already configured')
        if not isinstance(entry, dict) or entry.keys() - {'type', 'settings', 'enabled', 'interval_seconds'}:
            raise ValueError('Invalid document connection configuration')
        kind = entry.get('type')
        if not isinstance(kind, str) or kind not in _FACTORIES:
            raise ValueError('Unknown document connection type')
        enabled = entry.get('enabled', True)
        interval = entry.get('interval_seconds')
        adapter_settings = entry.get('settings')
        if (type(enabled) is not bool or (interval is not None and (type(interval) is not int or interval < 60))
                or not isinstance(adapter_settings, dict) or (kind == 'batch' and adapter_settings.keys() != {'pages'})):
            raise ValueError('Invalid document connection configuration')
        try:
            sources[source_key] = _FACTORIES[kind](adapter_settings)
        except (TypeError, ValueError):
            raise ValueError('Invalid document adapter settings') from None
        config[source_key] = ConnectionConfig(kind, enabled, interval)
    return sources, config


def source_type(source: DocumentSource) -> SourceType:
    if isinstance(source, HttpJsonSource):
        return 'http_json'
    if isinstance(source, ConfluenceSource):
        return 'confluence'
    return 'batch'


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    checked_at: datetime
    error_code: str | None


@dataclass(frozen=True)
class ConnectionRun:
    id: UUID
    status: str
    error_code: str | None


@dataclass(frozen=True)
class ConnectionStatus:
    source_key: str
    type: SourceType
    enabled: bool
    interval_seconds: int | None
    next_due_at: datetime | None
    last_check_at: datetime | None
    last_check_ok: bool | None
    last_check_error_code: str | None
    last_success_at: datetime | None
    last_run: ConnectionRun | None
    active_documents: int


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value is not None else None


_HTTP_ERRORS = frozenset({
    'catalog_configuration', 'http_timeout', 'http_transport', 'http_unauthorized', 'http_redirect', 'http_status',
    'invalid_encoding', 'response_too_large', 'invalid_json', 'invalid_catalog', 'too_many_documents',
    'invalid_document', 'duplicate_document', 'snapshot_regression', 'snapshot_conflict',
    'document_version_regression', 'document_version_conflict',
})


def _confluence_code(error: str) -> str:
    if error in {'Confluence HTTP 401', 'Confluence HTTP 403'}:
        return 'http_unauthorized'
    if error.startswith('Confluence HTTP 3'):
        return 'http_redirect'
    if error.startswith('Confluence HTTP '):
        return 'http_status'
    if error.startswith('Confluence transport: '):
        return 'http_timeout' if error.endswith('Timeout') else 'http_transport'
    return 'invalid_confluence_response'


def _run_code(error: str | None, kind: SourceType) -> str | None:
    if error is None or error in _HTTP_ERRORS or error in {'lease_expired', 'deadline_exceeded'}:
        return error
    if error == 'ConnectionPaused':
        return 'connection_paused'
    if kind == 'confluence' and error.startswith(('Confluence ', 'Invalid Confluence ', 'Unsafe Confluence ')):
        return _confluence_code(error)
    return 'sync_failed'


class DocumentConnections:
    def __init__(self, dsn, sources: Mapping[str, DocumentSource], config: Mapping[str, ConnectionConfig], *, profile):
        self.dsn = dsn
        self.sources = sources
        self.config = dict(config)
        self.profile = profile

    def ensure_enabled(self, source_key: str) -> None:
        if self.profile == 'hackathon' and source_key not in HACKATHON_DOCUMENT_SOURCES:
            raise KeyError(source_key)
        if not self.config[source_key].enabled:
            raise ConnectionPaused('Document connection is paused')

    def visible(self, source_key: str) -> bool:
        return source_key in self.config and (
            self.profile != 'hackathon' or source_key in HACKATHON_DOCUMENT_SOURCES
        )

    def check(self, source_key: str) -> ConnectionCheck:
        self.ensure_enabled(source_key)
        source = self.sources[source_key]
        error = None
        try:
            if hasattr(source, 'check_connection'):
                source.check_connection()
            else:
                source.list_pages(None)
        except HttpJsonError as exc:
            error = str(exc)
        except ConfluenceError as exc:
            error = _confluence_code(str(exc))
        except (ValueError, SnapshotError):
            error = 'invalid_batch' if isinstance(source, BatchSource) else 'check_failed'
        except Exception:
            error = 'check_failed'
        with connect(self.dsn) as conn:
            checked_at = conn.execute(
                """
                INSERT INTO document_connections (profile, source_key, last_check_at, last_check_error)
                VALUES (%s, %s, clock_timestamp(), %s)
                ON CONFLICT (profile, source_key) DO UPDATE SET last_check_at = clock_timestamp(),
                    last_check_error = excluded.last_check_error
                RETURNING last_check_at
                """,
                (self.profile, source_key, error),
            ).fetchone()['last_check_at']
        return ConnectionCheck(error is None, _utc(checked_at), error)

    def statuses(self) -> list[ConnectionStatus]:
        keys = sorted(key for key in self.config if self.visible(key))
        if not keys:
            return []
        with connect(self.dsn) as conn:
            rows = conn.execute(
                """
                SELECT key AS source_key, state.interval_seconds, state.next_due_at, state.last_check_at,
                    state.last_check_error, last_run.id, last_run.status, last_run.error,
                    success.finished_at AS last_success_at, active.total AS active_documents
                FROM unnest(%(keys)s::text[]) AS config(key)
                LEFT JOIN document_connections state ON state.profile = %(profile)s AND state.source_key = key
                LEFT JOIN LATERAL (
                    SELECT id, status, error FROM document_sync_runs WHERE source_key = key
                    ORDER BY created_at DESC, id DESC LIMIT 1
                ) last_run ON true
                LEFT JOIN LATERAL (
                    SELECT finished_at FROM document_sync_runs WHERE source_key = key AND status = 'completed'
                    ORDER BY finished_at DESC LIMIT 1
                ) success ON true
                CROSS JOIN LATERAL (
                    SELECT count(*) AS total FROM sources s JOIN source_revisions r ON r.id = s.current_revision_id
                    WHERE s.source_system = 'document' AND s.source_key = key AND s.status = 'active'
                        AND (NOT %(local)s OR (s.acl_actor_ids IS NULL AND r.acl_actor_ids IS NULL AND r.upstream_url IS NULL))
                ) active
                ORDER BY key
                """,
                {'keys': keys, 'profile': self.profile, 'local': self.profile == 'hackathon'},
            ).fetchall()
        statuses = []
        for row in rows:
            key = row['source_key']
            config = self.config[key]
            last_run = (ConnectionRun(row['id'], row['status'], _run_code(row['error'], config.kind))
                        if row['id'] else None)
            due = (row['next_due_at'] if config.enabled and config.interval_seconds == row['interval_seconds'] else None)
            statuses.append(ConnectionStatus(
                key, config.kind, config.enabled, config.interval_seconds, _utc(due), _utc(row['last_check_at']),
                row['last_check_error'] is None if row['last_check_at'] else None, row['last_check_error'],
                _utc(row['last_success_at']), last_run, row['active_documents'],
            ))
        return statuses

    def schedule_due(self, request_sync: Callable) -> int:
        if self.profile == 'hackathon':
            return 0
        scheduled = {key: config.interval_seconds for key, config in self.config.items()
                     if config.enabled and config.interval_seconds is not None}
        keys = list(scheduled)
        intervals = [str(scheduled[key]) for key in keys]
        considered = []
        dispatched = 0
        with connect(self.dsn) as conn:
            conn.autocommit = True
            with conn.transaction():
                self._reconcile_schedule(conn, keys, intervals)
            for _ in range(SCHEDULE_BATCH_SIZE):
                with conn.transaction():
                    row = conn.execute(
                        """
                        SELECT state.source_key FROM document_connections state
                        JOIN unnest(%s::text[], %s::numeric[]) AS config(source_key, seconds)
                            ON state.source_key = config.source_key AND state.interval_seconds = config.seconds
                        WHERE profile = %s AND next_due_at <= statement_timestamp() AND NOT (state.source_key = ANY(%s))
                        ORDER BY next_due_at, state.source_key LIMIT 1 FOR UPDATE OF state SKIP LOCKED
                        """,
                        (keys, intervals, self.profile, considered),
                    ).fetchone()
                    if row is None:
                        break
                    key = row['source_key']
                    considered.append(key)
                    if request_sync(conn, key, wait=False) is None:
                        continue
                    conn.execute(
                        f'UPDATE document_connections SET next_due_at = {_NEXT_DUE} WHERE profile = %s AND source_key = %s',
                        (self.profile, key),
                    )
                    dispatched += 1
        return dispatched

    def _reconcile_schedule(self, conn, keys: list[str], intervals: list[str]) -> None:
        conn.execute(
            """
            UPDATE document_connections SET next_due_at = NULL, interval_seconds = NULL
            WHERE profile = %s AND next_due_at IS NOT NULL AND NOT (source_key = ANY(%s))
            """,
            (self.profile, keys),
        )
        conn.execute(
            f"""
            WITH config AS (
                SELECT * FROM unnest(%(keys)s::text[], %(intervals)s::numeric[]) AS source(source_key, interval_seconds)
            )
            INSERT INTO document_connections (profile, source_key, interval_seconds, next_due_at)
            SELECT %(profile)s, source_key, interval_seconds, {_NEXT_DUE} FROM config
            WHERE NOT EXISTS (
                SELECT 1 FROM document_connections state WHERE state.profile = %(profile)s
                    AND state.source_key = config.source_key AND state.interval_seconds = config.interval_seconds
            )
            ORDER BY source_key
            ON CONFLICT (profile, source_key) DO UPDATE SET interval_seconds = excluded.interval_seconds,
                next_due_at = excluded.next_due_at
            WHERE document_connections.interval_seconds IS DISTINCT FROM excluded.interval_seconds
            """,
            {'profile': self.profile, 'keys': keys, 'intervals': intervals},
        )
