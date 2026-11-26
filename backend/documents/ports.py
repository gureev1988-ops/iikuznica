"""Document connector and revision indexing boundaries."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class PageStatus(StrEnum):
    ACTIVE = 'active'
    REMOVED = 'removed'
    ARCHIVED = 'archived'
    ACCESS_LOST = 'access_lost'
    MOVED = 'moved'


@dataclass(frozen=True)
class PageSnapshot:
    external_id: str
    version: int
    title: str
    storage_html: str
    space_key: str | None = None
    upstream_url: str | None = None
    acl_actor_ids: frozenset[str] | None = None  # None denotes the employee corpus.

    def __post_init__(self) -> None:
        if not self.external_id or not self.title or self.version < 1:
            raise ValueError('A document needs an external ID, title and positive version')


@dataclass(frozen=True)
class PageReference:
    external_id: str


@dataclass(frozen=True)
class PageBatch:
    pages: tuple[PageSnapshot | PageReference, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class PageProbe:
    status: PageStatus
    snapshot: PageSnapshot | None = None

    def __post_init__(self) -> None:
        if (self.status == PageStatus.ACTIVE) != (self.snapshot is not None):
            raise ValueError('An active page probe must include its snapshot')


class DocumentSource(Protocol):
    def list_pages(self, cursor: str | None) -> PageBatch:
        """Read a page of currently applicable documents in the configured scope."""
        ...

    def probe(self, external_id: str) -> PageProbe:
        """Determine whether a previously imported page is still applicable."""
        ...


@dataclass(frozen=True)
class Fragment:
    anchor: str
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class IndexFragment:
    fragment_id: UUID
    anchor: str
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class IndexRevision:
    document_id: UUID
    revision_id: UUID
    snapshot: PageSnapshot
    fragments: tuple[IndexFragment, ...]
    checksum: str


class RevisionIndexer(Protocol):
    def index(self, revision: IndexRevision) -> None:
        """Persist this revision's index, idempotently by revision_id."""
        ...


class BatchSource:
    """A complete, synthetic inventory for local runs and batch imports."""

    def __init__(self, pages: Iterable[PageSnapshot], *, batch_size: int = 100):
        if batch_size < 1:
            raise ValueError('Batch size must be positive')
        self._pages = tuple(pages)
        self._by_id = {page.external_id: page for page in self._pages}
        if len(self._by_id) != len(self._pages):
            raise ValueError('A batch source requires one current version per page')
        self._batch_size = batch_size

    def list_pages(self, cursor: str | None) -> PageBatch:
        start = 0 if cursor is None else int(cursor)
        if start < 0 or start > len(self._pages):
            raise ValueError('Invalid batch cursor')
        end = start + self._batch_size
        return PageBatch(self._pages[start:end], str(end) if end < len(self._pages) else None)

    def probe(self, external_id: str) -> PageProbe:
        page = self._by_id.get(external_id)
        if page is None:
            return PageProbe(PageStatus.REMOVED)
        return PageProbe(PageStatus.ACTIVE, page)
