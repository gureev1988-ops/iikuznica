"""Read-only Confluence Server/Data Center REST API v1 connector."""

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx

from .ports import PageBatch, PageProbe, PageReference, PageSnapshot, PageStatus


class ConfluenceError(RuntimeError):
    """A transport or API profile error; safe to record in a sync result."""


def _safe_path(path: str) -> bool:
    decoded = unquote(path)
    return (
        decoded.startswith('/')
        and not decoded.startswith('//')
        and not any(char in '%\\' or ord(char) < 32 or ord(char) == 127 for char in decoded)
        and not any(segment in {'.', '..'} for segment in decoded.split('/'))
    )


@dataclass(frozen=True)
class ConfluenceConfig:
    base_url: str
    spaces: tuple[str, ...]
    token: str | None = field(default=None, repr=False)
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    page_size: int = 30
    timeout_seconds: float = 10

    def __post_init__(self) -> None:
        base = urlsplit(self.base_url)
        if (
            base.scheme != 'https' or not base.hostname or base.username is not None
            or base.password is not None or base.query or base.fragment
            or not _safe_path(base.path or '/')
            or any(char.isspace() or ord(char) < 32 or char == '\\' for char in self.base_url)
        ):
            raise ValueError('Confluence base URL must be HTTPS with an optional context path')
        if base.port is not None and base.port < 1:
            raise ValueError('Invalid Confluence port')
        if (not self.spaces or len(set(self.spaces)) != len(self.spaces)
                or any(not space.strip() for space in self.spaces)):
            raise ValueError('Confluence requires a nonempty allowlist of distinct space keys')
        if bool(self.username) != bool(self.password) or self.token is not None and self.username:
            raise ValueError('Configure a personal access token or both Basic credentials')
        if self.token is not None and not re.fullmatch(r'[!-~]+', self.token):
            raise ValueError('Invalid Confluence token')
        if not 1 <= self.page_size <= 100 or self.timeout_seconds <= 0:
            raise ValueError('Confluence requires a page size from 1 to 100 and a positive timeout')


class ConfluenceSource:
    _CONTENT = '/rest/api/content'
    _EXPAND = 'body.storage,version,space,ancestors'

    def __init__(self, config: ConfluenceConfig, *, client: httpx.Client | None = None):
        self.config = config
        self._base = config.base_url.rstrip('/')
        self._context = urlsplit(self._base).path
        self._headers = {'Accept': 'application/json'}
        if config.token:
            self._headers['Authorization'] = f'Bearer {config.token}'
        self._auth = httpx.BasicAuth(config.username, config.password or '') if config.username else None
        self._client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, path: str, params: dict[str, str | int]) -> httpx.Response:
        try:
            return self._client.get(
                self._base + path, params=params, headers=self._headers, auth=self._auth,
                timeout=self.config.timeout_seconds, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ConfluenceError(f'Confluence transport: {type(exc).__name__}') from None

    @staticmethod
    def _data(response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200:
            raise ConfluenceError(f'Confluence HTTP {response.status_code}')
        try:
            result = response.json()
        except ValueError:
            raise ConfluenceError('Invalid Confluence JSON') from None
        if not isinstance(result, dict):
            raise ConfluenceError('Invalid Confluence response shape')
        return result

    @staticmethod
    def _page_id(external_id: str) -> str:
        if not re.fullmatch(r'[0-9]+', external_id):
            raise ConfluenceError('Invalid Confluence content ID')
        return external_id

    def _relative_link(self, value: str) -> tuple[str, str, str]:
        try:
            if any(char.isspace() or ord(char) < 32 or char == '\\' for char in value):
                raise ValueError
            link = urlsplit(value)
            base = urlsplit(self._base)
            if link.netloc or link.scheme:
                port = 443 if link.port is None else link.port
                if (link.scheme, link.hostname, port) != (base.scheme, base.hostname, base.port or 443):
                    raise ValueError
                if link.username is not None or link.password is not None:
                    raise ValueError
                if self._context and not link.path.startswith(self._context + '/'):
                    raise ValueError
            if link.path and not _safe_path(link.path):
                raise ValueError
            if self._context and link.path.startswith(self._context + '/'):
                path = link.path[len(self._context):]
            else:
                path = link.path
            return path, link.query, link.fragment
        except ValueError:
            raise ConfluenceError('Unsafe Confluence link') from None

    def _next_start(self, value: str, start: int) -> int:
        path, query, anchor = self._relative_link(value)
        params = parse_qs(query, max_num_fields=30)
        offsets = params.get('start', [])
        if ((path and path != self._CONTENT) or anchor or len(offsets) != 1
                or not re.fullmatch(r'[0-9]{1,12}', offsets[0]) or int(offsets[0]) <= start):
            raise ConfluenceError('Invalid Confluence pagination link')
        return int(offsets[0])

    def list_pages(self, cursor: str | None) -> PageBatch:
        try:
            space, start = (self.config.spaces[0], 0) if cursor is None else json.loads(cursor)
            if space not in self.config.spaces or type(start) is not int or start < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ConfluenceError('Invalid Confluence cursor') from None
        pages, next_link = self._listing(space, start, self.config.page_size)
        if next_link:
            return PageBatch(pages, json.dumps([space, self._next_start(next_link, start)]))
        index = self.config.spaces.index(space) + 1
        next_cursor = json.dumps([self.config.spaces[index], 0]) if index < len(self.config.spaces) else None
        return PageBatch(pages, next_cursor)

    def check_connection(self) -> None:
        for space in self.config.spaces:
            _, next_link = self._listing(space, 0, 1)
            if next_link:
                self._next_start(next_link, 0)

    def _listing(self, space: str, start: int, limit: int) -> tuple[tuple[PageReference, ...], str | None]:
        payload = self._data(self._request(self._CONTENT, {
            'spaceKey': space, 'type': 'page', 'status': 'current', 'start': start,
            'limit': limit,
        }))
        try:
            results = payload['results']
            links = payload['_links']
            if not isinstance(results, list) or not isinstance(links, dict):
                raise TypeError
            pages = tuple(PageReference(self._page_id(item['id'])) for item in results)
            next_link = links.get('next')
            if next_link is not None and not isinstance(next_link, str):
                raise TypeError
        except (KeyError, TypeError):
            raise ConfluenceError('Invalid Confluence listing response') from None
        return pages, next_link

    def _page(self, external_id: str, *, status: str, version: int | None = None) -> httpx.Response:
        params = {'status': status, 'expand': self._EXPAND, 'version': version} if version else {
            'status': status, 'expand': self._EXPAND,
        }
        return self._request(f'{self._CONTENT}/{self._page_id(external_id)}', params)

    def _page_data(self, response: httpx.Response, external_id: str) -> dict:
        payload = self._data(response)
        if payload.get('id') != external_id or payload.get('type') != 'page':
            raise ConfluenceError('Confluence returned a different content item')
        return payload

    def _restricted(self, external_id: str) -> bool:
        path = f'{self._CONTENT}/{self._page_id(external_id)}/restriction/byOperation/read'
        response = self._request(path, {'expand': 'restrictions.user,restrictions.group'})
        if response.status_code in {403, 404}:
            return True
        payload = self._data(response)
        try:
            restrictions = payload['restrictions']
            views = (restrictions['user'], restrictions['group'])
            for view in views:
                results = view['results']
                if not isinstance(results, list):
                    raise TypeError
                if results or view.get('size', 0) or view.get('_links', {}).get('next'):
                    return True
        except (AttributeError, KeyError, TypeError):
            raise ConfluenceError('Confluence read restrictions could not be verified') from None
        return False

    def _snapshot(self, payload: dict, external_id: str, expected_version: int | None = None) -> PageSnapshot:
        try:
            space = payload['space']['key']
            title = payload['title']
            version = payload['version']['number']
            html = payload['body']['storage']['value']
            ancestors = payload['ancestors']
            if (type(version) is not int or version < 1 or not isinstance(title, str) or not isinstance(html, str)
                    or not isinstance(ancestors, list) or not isinstance(space, str)):
                raise TypeError
            if space not in self.config.spaces:
                raise ConfluenceError('Confluence page is outside the configured spaces')
            if expected_version is not None and version != expected_version:
                raise ConfluenceError('Confluence returned a different page version')
            parent_ids = tuple(self._page_id(ancestor['id']) for ancestor in ancestors)
            page_link = payload.get('_links', {}).get('webui')
            if page_link:
                path, query, anchor = self._relative_link(page_link)
                upstream_url = self._base + path + (f'?{query}' if query else '') + (f'#{anchor}' if anchor else '')
            else:
                upstream_url = f'{self._base}/pages/viewpage.action?{urlencode({"pageId": external_id})}'
        except (AttributeError, KeyError, TypeError):
            raise ConfluenceError('Invalid Confluence storage page response') from None
        # Confluence restriction principals require an explicit identity mapping.
        # Keep their content outside the employee corpus, including inherited restrictions.
        restricted = any(self._restricted(content_id) for content_id in (external_id, *parent_ids))
        return PageSnapshot(external_id, version, title, html, space, upstream_url,
                            frozenset() if restricted else None)

    def read_revision(self, external_id: str, version: int) -> PageSnapshot:
        """Fetch a specific storage revision through Confluence's historical REST profile."""
        if type(version) is not int or version < 1:
            raise ValueError('Confluence version must be a positive integer')
        response = self._page(external_id, status='historical', version=version)
        payload = self._page_data(response, external_id)
        if payload.get('status') != 'historical':
            raise ConfluenceError('Confluence did not return a historical revision')
        # The read-restriction endpoint describes current permissions. Historical
        # backfills have no verified original audience and start private.
        snapshot = self._snapshot(payload, external_id, version)
        return replace(snapshot, acl_actor_ids=frozenset())

    def probe(self, external_id: str) -> PageProbe:
        response = self._page(external_id, status='current')
        if response.status_code == 403:
            return PageProbe(PageStatus.ACCESS_LOST)
        if response.status_code == 410:
            return PageProbe(PageStatus.REMOVED)
        if response.status_code == 404:
            # A 404 alone is ambiguous in Confluence. Verify archive/trash when visible.
            for status, outcome in (('archived', PageStatus.ARCHIVED), ('trashed', PageStatus.REMOVED)):
                check = self._page(external_id, status=status)
                if check.status_code == 200 and self._page_data(check, external_id).get('status') == status:
                    return PageProbe(outcome)
                if check.status_code == 410:
                    return PageProbe(PageStatus.REMOVED)
                if check.status_code not in {200, 403, 404}:
                    self._data(check)
            return PageProbe(PageStatus.ACCESS_LOST)
        payload = self._page_data(response, external_id)
        status = payload.get('status')
        if status in {'archived', 'trashed', 'deleted'}:
            return PageProbe(PageStatus.ARCHIVED if status == 'archived' else PageStatus.REMOVED)
        if status != 'current':
            raise ConfluenceError('Unexpected Confluence page status')
        try:
            space = payload['space']['key']
            if not isinstance(space, str):
                raise TypeError
        except (KeyError, TypeError):
            raise ConfluenceError('Confluence page space could not be verified') from None
        if space not in self.config.spaces:
            return PageProbe(PageStatus.MOVED)
        return PageProbe(PageStatus.ACTIVE, self._snapshot(payload, external_id))
