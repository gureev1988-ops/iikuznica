"""Read complete, versioned employee document inventories over HTTPS."""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from html import escape
import json
import logging
import os
import re
from urllib.parse import urlsplit
import zlib

import httpx

from .ports import PageBatch, PageProbe, PageSnapshot, PageStatus


MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_DOCUMENTS = 500
HTTP_BUDGET_SECONDS = 10
MAX_DOCUMENT_VERSION = 2**63 - 1
# Leave room for source_key and tuple overhead in PostgreSQL's composite B-tree keys.
MAX_EXTERNAL_ID_BYTES = 2048
_HEADER_FIELDS = {'schema_version', 'snapshot_revision', 'complete', 'count', 'documents'}
_DOCUMENT_FIELDS = {'id', 'revision', 'title', 'text', 'source_url'}
_NEWLINE = r'(?:\r\n|\r(?!\n)|(?<!\r)\n)'
_PARAGRAPH_BREAK = re.compile(rf'({_NEWLINE}[ \t]*{_NEWLINE})')
_INVALID_URL_CHAR = re.compile(r'[\x00-\x20\x7f<>"{}|\\^`]|%(?![0-9a-fA-F]{2})')
_PRIVATE_HTTP = ContextVar('private_document_http', default=False)


class HttpJsonError(RuntimeError):
    """The message is a diagnostic code independent of upstream data."""


def _allow_http_log(_record):
    return not _PRIVATE_HTTP.get()


def valid_https_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return (
            parts.scheme == 'https' and bool(parts.hostname) and parts.username is None
            and '%' not in parts.netloc and (parts.port is None or parts.port > 0)
            and not _INVALID_URL_CHAR.search(value) and not any(char.isspace() for char in value)
            and bool(httpx.URL(value).host)
        )
    except (ValueError, httpx.InvalidURL):
        return False


@dataclass(frozen=True)
class HttpJsonConfig:
    url: str = field(repr=False)
    audience: str
    bearer_token_env: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.audience != 'all_employees' or not isinstance(self.url, str) or not valid_https_url(self.url)
                or '#' in self.url):
            raise ValueError('Invalid HTTPS catalog settings')
        if self.bearer_token_env is not None and (
            not isinstance(self.bearer_token_env, str)
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.bearer_token_env)
        ):
            raise ValueError('Invalid catalog token variable')


@dataclass(frozen=True)
class HttpInventory:
    revision: int
    digest: str
    documents: list[dict]

    def as_source(self):
        return _InventorySource(self.documents)


class _InventorySource:
    """One pinned inventory serves both the listing and the absence checks."""

    def __init__(self, documents: list[dict]):
        self._pages = {record['id']: _page(record) for record in documents}

    def list_pages(self, cursor: str | None) -> PageBatch:
        if cursor is not None:
            raise ValueError('A JSON catalog does not paginate')
        return PageBatch(tuple(self._pages.values()))

    def probe(self, external_id: str) -> PageProbe:
        snapshot = self._pages.get(external_id)
        return PageProbe(PageStatus.ACTIVE, snapshot) if snapshot else PageProbe(PageStatus.REMOVED)


def _page(record: dict) -> PageSnapshot:
    text = _PARAGRAPH_BREAK.sub(r'\1</p><p>', escape(record['text'], quote=False))
    return PageSnapshot(record['id'], record['revision'], record['title'], f'<p>{text}</p>',
                        upstream_url=record['source_url'])


def _unique_object(pairs: list[tuple]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate JSON member')
    return result


def _valid_text(value) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and '\x00' not in value
            and not any('\ud800' <= char <= '\udfff' for char in value))


def parse_inventory(body: bytes) -> HttpInventory:
    try:
        data = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise HttpJsonError('invalid_json') from None
    if (not isinstance(data, dict) or data.keys() != _HEADER_FIELDS
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or type(data['snapshot_revision']) is not int or data['snapshot_revision'] < 1
            or data['complete'] is not True or type(data['count']) is not int
            or not isinstance(data['documents'], list) or data['count'] != len(data['documents'])):
        raise HttpJsonError('invalid_catalog')
    documents = data['documents']
    if len(documents) > MAX_DOCUMENTS:
        raise HttpJsonError('too_many_documents')
    seen = set()
    for record in documents:
        if (not isinstance(record, dict) or record.keys() != _DOCUMENT_FIELDS
                or type(record['revision']) is not int or not 1 <= record['revision'] <= MAX_DOCUMENT_VERSION
                or not all(_valid_text(record[field]) for field in ('id', 'title', 'text', 'source_url'))
                or len(record['id'].encode('utf-8')) > MAX_EXTERNAL_ID_BYTES
                or not valid_https_url(record['source_url'])):
            raise HttpJsonError('invalid_document')
        if record['id'] in seen:
            raise HttpJsonError('duplicate_document')
        seen.add(record['id'])
    ordered = sorted(documents, key=lambda item: item['id'])
    canonical = json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return HttpInventory(data['snapshot_revision'], sha256(canonical).hexdigest(), ordered)


async def _read_body(response: httpx.Response) -> bytes:
    # Buffered responses are supported for synthetic transports. The standard
    # transport always streams, with decoding bounded before allocating output.
    if response.is_stream_consumed:
        if len(response.content) > MAX_BODY_BYTES:
            raise HttpJsonError('response_too_large')
        return response.content
    encoding = response.headers.get('Content-Encoding', 'identity').strip().lower()
    if encoding not in {'identity', 'gzip', 'deflate'}:
        raise HttpJsonError('invalid_encoding')
    decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15) if encoding != 'identity' else None
    body = bytearray()
    try:
        async for chunk in response.aiter_raw():
            remaining = MAX_BODY_BYTES - len(body)
            decoded = decoder.decompress(chunk, remaining + 1) if decoder else chunk
            if len(decoded) > remaining:
                raise HttpJsonError('response_too_large')
            body.extend(decoded)
        if decoder and (not decoder.eof or decoder.unused_data):
            raise HttpJsonError('invalid_encoding')
    except zlib.error:
        raise HttpJsonError('invalid_encoding') from None
    return bytes(body)


class HttpJsonSource:
    def __init__(self, config: HttpJsonConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport
        # HTTPX logs URLs at INFO and HTTPCore traces headers at DEBUG.
        # Suppress those records only in the context of this connector's requests.
        for name in ('httpx', 'httpcore.connection', 'httpcore.http11'):
            logging.getLogger(name).addFilter(_allow_http_log)

    def read_inventory(self) -> HttpInventory:
        headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip, deflate'}
        if (variable := self.config.bearer_token_env) is not None:
            token = os.getenv(variable)
            if not token or not re.fullmatch(r'[!-~]+', token):
                raise HttpJsonError('catalog_configuration')
            headers['Authorization'] = f'Bearer {token}'
        private = _PRIVATE_HTTP.set(True)
        try:
            loop = asyncio.new_event_loop()
            try:
                return parse_inventory(loop.run_until_complete(self._fetch(headers)))
            finally:
                # A timed-out DNS lookup can still be running in the loop's
                # executor. Closing the loop leaves that lookup to finish.
                loop.close()
        except (TimeoutError, httpx.TimeoutException):
            raise HttpJsonError('http_timeout') from None
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            raise HttpJsonError('http_transport') from None
        finally:
            _PRIVATE_HTTP.reset(private)

    async def _fetch(self, headers: dict[str, str]) -> bytes:
        async with asyncio.timeout(HTTP_BUDGET_SECONDS):
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=HTTP_BUDGET_SECONDS, transport=self._transport,
            ) as client:
                async with client.stream('GET', self.config.url, headers=headers) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise HttpJsonError('http_unauthorized')
                    if 300 <= status < 400:
                        raise HttpJsonError('http_redirect')
                    if status != 200:
                        raise HttpJsonError('http_status')
                    return await _read_body(response)

    def check_connection(self) -> None:
        self.read_inventory()

    def list_pages(self, cursor: str | None) -> PageBatch:
        return self.read_inventory().as_source().list_pages(cursor)

    def probe(self, external_id: str) -> PageProbe:
        return self.read_inventory().as_source().probe(external_id)


def prepare_inventory(conn, run_id, source_key: str, source: HttpJsonSource, check_lease: Callable | None):
    """Pin the validated export and its version before changing any catalog row."""
    from psycopg.types.json import Jsonb

    stored = conn.execute('SELECT documents FROM document_run_snapshots WHERE run_id = %s', (run_id,)).fetchone()
    if stored is not None:
        return _InventorySource(stored['documents'])
    inventory = source.read_inventory()
    pinned = inventory.as_source()
    with conn.transaction():
        if check_lease:
            check_lease(conn)
        accepted = conn.execute(
            'SELECT snapshot_revision, content_sha256 FROM document_snapshot_versions WHERE source_key = %s',
            (source_key,),
        ).fetchone()
        if accepted:
            if inventory.revision < accepted['snapshot_revision']:
                raise HttpJsonError('snapshot_regression')
            if inventory.revision == accepted['snapshot_revision'] and inventory.digest != accepted['content_sha256']:
                raise HttpJsonError('snapshot_conflict')
        saved = conn.execute(
            """
            SELECT s.external_id, r.version, r.title, r.storage_body, r.upstream_url
            FROM sources s JOIN LATERAL (
                SELECT version, title, storage_body, upstream_url FROM source_revisions
                WHERE source_id = s.id ORDER BY version DESC LIMIT 1
            ) r ON true
            WHERE s.source_system = 'document' AND s.source_key = %s AND s.external_id = ANY(%s)
            """,
            (source_key, [doc['id'] for doc in inventory.documents]),
        )
        for row in saved:
            page = pinned.probe(row['external_id']).snapshot
            if page.version < row['version']:
                raise HttpJsonError('document_version_regression')
            if page.version == row['version'] and (page.title, page.storage_html, page.upstream_url) != (
                row['title'], row['storage_body'], row['upstream_url'],
            ):
                raise HttpJsonError('document_version_conflict')
        conn.execute(
            """
            INSERT INTO document_snapshot_versions (source_key, snapshot_revision, content_sha256)
            VALUES (%s, %s, %s) ON CONFLICT (source_key) DO UPDATE SET
                snapshot_revision = excluded.snapshot_revision, content_sha256 = excluded.content_sha256
            """,
            (source_key, inventory.revision, inventory.digest),
        )
        conn.execute('INSERT INTO document_run_snapshots (run_id, documents) VALUES (%s, %s)',
                     (run_id, Jsonb(inventory.documents)))
    return pinned
