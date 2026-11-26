from dataclasses import replace
from hashlib import sha256
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from backend.db import connect
from backend.documents import (
    BatchSource,
    ConfluenceConfig,
    ConfluenceError,
    ConfluenceSource,
    DocumentSync,
    PageBatch,
    PageProbe,
    PageSnapshot,
    PageStatus,
    SnapshotError,
    get_fragment,
    list_fragments,
    make_router,
)
from backend.runtime import run_once


def _page_payload(external_id: str, version: int, **updates) -> dict[str, Any]:
    return {
        'id': external_id,
        'type': 'page',
        'title': 'Approved process',
        'status': 'current',
        'version': {'number': version},
        'space': {'key': 'HR'},
        'ancestors': [],
        'body': {'storage': {'value': f'<h2>Due date</h2><p>{version} days</p>'}},
        '_links': {'webui': f'/display/HR/Approved+process?pageId={external_id}'},
        **updates,
    }


def _restrictions(*, restricted: bool = False) -> dict:
    return {'restrictions': {
        'user': {'results': [{'username': 'synthetic'}] if restricted else []},
        'group': {'results': []},
    }}


def test_confluence_native_listing_and_two_historical_versions():
    called = []

    def mock(request: httpx.Request) -> httpx.Response:
        assert request.method == 'GET'
        assert request.url.host == 'docs.example.test'
        assert request.headers['Authorization'] == 'Bearer synthetic-token'
        called.append((request.url.path, dict(request.url.params)))
        path = request.url.path
        params = request.url.params
        if path == '/wiki/rest/api/content':
            assert params['type'] == 'page' and params['status'] == 'current'
            assert params['limit'] == '1'
            if params['spaceKey'] == 'HR' and params['start'] == '0':
                return httpx.Response(200, json={
                    'results': [{'id': '11'}],
                    '_links': {'next': '/rest/api/content?start=1&limit=1'},
                })
            if params['spaceKey'] == 'HR':
                assert params['start'] == '1'
                return httpx.Response(200, json={'results': [{'id': '12'}], '_links': {}})
            assert params['spaceKey'] == 'OPS'
            return httpx.Response(200, json={'results': [], '_links': {}})
        if path.endswith('/restriction/byOperation/read'):
            return httpx.Response(200, json=_restrictions(restricted='/99/' in path))
        assert path == '/wiki/rest/api/content/11'
        assert set(params['expand'].split(',')) == {'ancestors', 'body.storage', 'version', 'space'}
        if params['status'] == 'historical':
            version = int(params['version'])
            return httpx.Response(200, json=_page_payload('11', version, status='historical'))
        assert params['status'] == 'current'
        return httpx.Response(200, json=_page_payload('11', 3, ancestors=[{'id': '99'}]))

    config = ConfluenceConfig('https://docs.example.test/wiki', ('HR', 'OPS'), token='synthetic-token', page_size=1)
    with httpx.Client(transport=httpx.MockTransport(mock)) as client:
        source = ConfluenceSource(config, client=client)
        first = source.list_pages(None)
        second = source.list_pages(first.next_cursor)
        last = source.list_pages(second.next_cursor)
        assert [page.external_id for page in first.pages + second.pages] == ['11', '12']
        assert last.pages == () and last.next_cursor is None
        for version in (1, 2):
            historical = source.read_revision('11', version)
            assert historical.version == version
            assert historical.storage_html.endswith(f'<p>{version} days</p>')
            assert historical.acl_actor_ids == frozenset()
            assert historical.upstream_url == 'https://docs.example.test/wiki/display/HR/Approved+process?pageId=11'
        latest = source.probe('11').snapshot
        assert latest is not None and latest.acl_actor_ids == frozenset()
    assert '/wiki/rest/api/content/99/restriction/byOperation/read' in {item[0] for item in called}


def test_confluence_probe_distinguishes_movement_archive_trash_and_access_loss():
    def mock(request: httpx.Request) -> httpx.Response:
        external_id = request.url.path.rsplit('/', 1)[-1]
        status = request.url.params['status']
        if external_id == '1':
            return httpx.Response(200, json=_page_payload('1', 1, space={'key': 'OTHER'}))
        if external_id == '2' and status == 'archived':
            return httpx.Response(200, json=_page_payload('2', 1, status='archived'))
        if external_id == '3' and status == 'trashed':
            return httpx.Response(200, json=_page_payload('3', 1, status='trashed'))
        if external_id == '4':
            return httpx.Response(403)
        if external_id == '6':
            return httpx.Response(401)
        return httpx.Response(404)

    config = ConfluenceConfig('https://docs.example.test', ('HR',), username='synthetic', password='test-only')
    with httpx.Client(transport=httpx.MockTransport(mock)) as client:
        source = ConfluenceSource(config, client=client)
        assert [source.probe(str(page)).status for page in range(1, 6)] == [
            PageStatus.MOVED, PageStatus.ARCHIVED, PageStatus.REMOVED, PageStatus.ACCESS_LOST, PageStatus.ACCESS_LOST,
        ]
        with pytest.raises(ConfluenceError, match='HTTP 401'):
            source.probe('6')


@pytest.mark.parametrize('remote', [
    'https://outside.example.test/wiki/rest/api/content?start=1',
    '/wiki/rest/api/%2e%2e/content?start=1',
    '//outside.example.test/rest/api/content?start=1',
])
def test_confluence_rejects_untrusted_pagination_and_refuses_redirects(remote):
    calls = []

    def mock(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.path.endswith('/11'):
            return httpx.Response(302, headers={'Location': 'https://outside.example.test/'})
        return httpx.Response(200, json={'results': [], '_links': {'next': remote}})

    config = ConfluenceConfig('https://docs.example.test/wiki', ('HR',), token='synthetic-token')
    with httpx.Client(transport=httpx.MockTransport(mock), follow_redirects=True) as client:
        source = ConfluenceSource(config, client=client)
        with pytest.raises(ConfluenceError, match='Unsafe Confluence link'):
            source.list_pages(None)
        with pytest.raises(ConfluenceError, match='HTTP 302'):
            source.probe('11')
    assert calls == ['docs.example.test', 'docs.example.test']
    with pytest.raises(ValueError, match='HTTPS'):
        ConfluenceConfig('http://docs.example.test', ('HR',))
    with pytest.raises(ValueError, match='HTTPS'):
        ConfluenceConfig('https://username:token@docs.example.test', ('HR',))


def _snapshot(external_id: str, version: int, *, acl: frozenset[str] | None = None) -> PageSnapshot:
    return PageSnapshot(external_id, version, f'Page {external_id}',
                        f'<h2 id="due-date">Due <em>date</em></h2><p>{version} days &amp; hours</p>'
                        '<h3>Escalation</h3><ul><li>Talk to the team.</li></ul>'
                        '<h3>Escalation</h3><p>Alternate path.</p><script>untrusted()</script>',
                        'SYNTHETIC', acl_actor_ids=acl)


def _current(dsn, actor_id: str = 'alice'):
    with connect(dsn) as conn:
        return list_fragments(conn, actor_id)


def test_batch_import_preserves_source_versions_fragments_and_access(dsn):
    changes = []
    service = DocumentSync(dsn, {}, on_revision_change=lambda conn, new, old: changes.append((new, old)))
    private = _snapshot('fixed', 1, acl=frozenset({'alice'}))
    public = _snapshot('fixed', 2)
    revision1, revision2 = service.import_batch('fixture', (private, public))
    assert service.import_batch('fixture', (public,)) == (revision2,)
    assert changes == [(revision2, None)]
    assert {part.revision_id for part in _current(dsn, 'bob')} == {revision2}

    with connect(dsn) as conn:
        records = conn.execute('SELECT version, storage_body, checksum FROM source_revisions ORDER BY version').fetchall()
        assert records[0] == {'version': 1, 'storage_body': private.storage_html,
                              'checksum': sha256(private.storage_html.encode()).hexdigest()}
        parts = conn.execute('SELECT * FROM source_fragments WHERE revision_id = %s ORDER BY ordinal',
                             (revision1,)).fetchall()
        assert [row['anchor'] for row in parts] == ['due-date', 'escalation', 'escalation-2']
        assert [row['heading_path'] for row in parts] == [
            ['Due date'], ['Due date', 'Escalation'], ['Due date', 'Escalation'],
        ]
        assert parts[0]['text'] == 'Due date\n1 days & hours'
        assert parts[2]['text'] == 'Escalation\nAlternate path.'
        assert get_fragment(conn, revision1, parts[0]['id'], 'alice').text == parts[0]['text']
        assert get_fragment(conn, revision1, parts[0]['id'], 'alice', current=True) is None
        assert get_fragment(conn, revision1, parts[0]['id'], 'bob') is None

    service.import_batch('fixture', (replace(public, acl_actor_ids=frozenset({'alice'})),))
    assert _current(dsn, 'bob') == ()
    assert changes[-1] == (None, revision2)
    with pytest.raises(SnapshotError, match='conflicting content'):
        service.import_batch('fixture', (replace(public, storage_html='<p>changed</p>'),))
    assert {part.revision_id for part in _current(dsn)} == {revision2}


class _FailingIndexer:
    def __init__(self, dsn):
        self.dsn = dsn
        self.fail = True
        self.calls = []

    def index(self, revision):
        self.calls.append(revision.revision_id)
        assert revision.revision_id not in {part.revision_id for part in _current(self.dsn)}
        if revision.snapshot.external_id == 'b' and self.fail:
            raise OSError('synthetic index transport failure')


class _InterruptedInventory:
    def __init__(self):
        self.resume = False
        self.cursor_calls = []
        self.outcomes = {'c': PageStatus.MOVED, 'd': PageStatus.REMOVED, 'f': PageStatus.ARCHIVED,
                         'g': PageStatus.ACCESS_LOST}
        self.fail_probe = True

    def list_pages(self, cursor):
        self.cursor_calls.append(cursor)
        if cursor == 'rest':
            return PageBatch((_snapshot('e', 2),))
        first = (_snapshot('a', 2), _snapshot('b', 2))
        if self.resume:
            first = (_snapshot('new', 1), *first)
        return PageBatch(first, 'rest')

    def probe(self, external_id):
        if external_id == 'd' and self.fail_probe:
            raise OSError('synthetic probe transport failure')
        return PageProbe(self.outcomes[external_id])


def test_sync_checkpoints_verified_pages_without_deactivating_unvisited_pages(dsn):
    inventory = _InterruptedInventory()
    seed = [_snapshot(page_id, 1) for page_id in 'abcdefg']
    changes = []
    observer = lambda conn, new, old: changes.append((new, old))
    initial = DocumentSync(dsn, {'feed': BatchSource(seed, batch_size=2)}, on_revision_change=observer)
    initial.run(initial.request_sync('feed'))
    indexer = _FailingIndexer(dsn)
    service = DocumentSync(dsn, {'feed': inventory}, indexer=indexer, on_revision_change=observer)

    run_id = service.request_sync('feed')
    assert service.request_sync('feed') == run_id
    with pytest.raises(OSError):
        service.run(run_id)
    assert service.result(run_id).batch_offset == 1
    assert service.result(run_id).status == 'incomplete'
    assert service.result(run_id).counts['active'] == 1
    assert len({part.document_id for part in _current(dsn)}) == 7
    assert any(part.title == 'Page b' and part.text.endswith('1 days & hours') for part in _current(dsn))
    old_c = next(part for part in _current(dsn) if part.title == 'Page c')
    first_a, first_b = indexer.calls

    inventory.resume = True
    indexer.fail = False
    with pytest.raises(OSError):
        service.run(run_id)
    assert service.result(run_id).listing_done
    assert service.result(run_id).counts['moved'] == 1
    assert {part.title for part in _current(dsn)} == {
        'Page a', 'Page b', 'Page d', 'Page e', 'Page f', 'Page g', 'Page new',
    }
    with connect(dsn) as conn:
        assert get_fragment(conn, old_c.revision_id, old_c.fragment_id, 'alice') is None
    assert indexer.calls.count(first_a) == 1
    assert indexer.calls.count(first_b) == 2

    inventory.fail_probe = False
    result = service.run(run_id)
    assert result.status == 'completed'
    assert result.counts == {'active': 4, 'removed': 1, 'archived': 1, 'access_lost': 1, 'moved': 1}
    assert inventory.cursor_calls == [None, None, 'rest']
    assert {part.title for part in _current(dsn)} == {'Page a', 'Page b', 'Page e', 'Page new'}
    assert len([change for change in changes if change[0] is None]) == 4
    assert service.request_sync('feed') != run_id


def test_known_acl_loss_applies_before_new_revision_is_indexed(dsn):
    changes = []
    service = DocumentSync(dsn, {}, indexer=_FailingIndexer(dsn),
                           on_revision_change=lambda conn, new, old: changes.append((new, old)))
    service.indexer.fail = False
    service.import_batch('fixture', (_snapshot('b', 1),))
    old = _current(dsn, 'bob')[0]
    service.indexer.fail = True
    with pytest.raises(OSError):
        service.import_batch('fixture', (_snapshot('b', 2, acl=frozenset({'alice'})),))
    assert _current(dsn, 'bob') == ()
    assert {part.revision_id for part in _current(dsn, 'alice')} == {old.revision_id}
    with connect(dsn) as conn:
        assert get_fragment(conn, old.revision_id, old.fragment_id, 'bob') is None
    assert changes[-1] == (None, old.revision_id)


def test_document_sync_http_admin_and_worker_hooks(dsn):
    service = DocumentSync(dsn, {'demo': BatchSource((_snapshot('seed', 1),))},
                           on_revision_change=lambda conn, new, old: None)
    app = FastAPI()

    def require_admin(role: str | None = Header(default=None)) -> None:
        if role != 'ops':
            raise HTTPException(403, 'Administrator required')

    app.include_router(make_router(service, require_admin))
    client = TestClient(app)
    assert client.post('/v1/ops/documents/demo/sync').status_code == 403
    headers = {'Role': 'ops'}
    assert client.post('/v1/ops/documents/unknown/sync', headers=headers).status_code == 404
    response = client.post('/v1/ops/documents/demo/sync', headers=headers)
    assert response.status_code == 202
    run_id = response.json()['run_id']
    result_url = f'/v1/ops/documents/sync/{run_id}'
    assert client.get(result_url, headers=headers).json()['status'] == 'queued'
    job = run_once(dsn, 'background', {'documents.sync': service.handle_job},
                   terminal_handlers={'documents.sync': service.terminal_job})
    assert job.payload == {'run_id': run_id}
    assert client.get(result_url, headers=headers).json()['status'] == 'completed'
    assert _current(dsn)
