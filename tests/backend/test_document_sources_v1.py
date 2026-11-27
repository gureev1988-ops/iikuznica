"""Document sources v1 acceptance on PostgreSQL and synthetic HTTP responses."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import gzip
from hashlib import sha256
import json
import logging
import os
import socket
import subprocess
import sys
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app import Backend, create_app
from backend.config import INTEGRATED_QUALITY_EMAIL, Settings
from backend.contracts.engine import AnswerResult, Citation
from backend.db import connect
from backend.documents import list_fragments
from backend.documents import connections
from backend.documents import http_json
from backend.mailfaq import MailConfig, find_faq
from backend.mailfaq.service import POLL_KIND
from backend.mailfaq.transport import MailSimulator
from backend.runtime import enqueue


OPS = {'Authorization': 'Bearer test-ops'}
EMPLOYEE = {'Authorization': 'Bearer test-employee'}
QUALITY = {'Authorization': 'Bearer test-quality'}
OPS_PATH = '/v1/ops/documents'
QUESTION = 'Какой срок согласования?'
ANSWER = 'Срок согласования составляет 2 дня.'
MAIL = MailConfig(mode='disabled')


def document(id, revision=1, *, text='Original instructions.', title='Instructions', source_url=None):
    return dict(id=id, revision=revision, title=title, text=text,
                source_url=source_url or f'https://kb.example.test/{id}')


def export(revision, *documents, **updates):
    return dict(schema_version=1, snapshot_revision=revision, complete=True,
                count=len(documents), documents=list(documents)) | updates


def feed(key, *, interval=None, enabled=True, token=None):
    settings = dict(url=f'https://feeds.example.test/{key}?contact=example-employee', audience='all_employees')
    if token:
        settings['bearer_token_env'] = token
    return dict(type='http_json', interval_seconds=interval, enabled=enabled, settings=settings)


def settings(dsn, named=None, *, profile='integrated', **legacy):
    identities = [
        dict(id=name, roles=roles, bearer_sha256=sha256(f'test-{name}'.encode()).hexdigest(), email=email)
        for name, roles, email in (
            ('ops', ['ops'], 'ops@example.test'), ('employee', ['employee'], 'employee@example.test'),
            ('quality', ['quality', 'employee'], INTEGRATED_QUALITY_EMAIL),
        )
    ]
    integrations = legacy | ({} if named is None else {'document_sources': named})
    return Settings(database_url=dsn, profile=profile, integrations=integrations, identities=identities,
                    background_concurrency=2)


@pytest.fixture
def upstream(monkeypatch):
    responses, calls = {}, []

    async def handler(request):
        assert request.method == 'GET'
        assert request.url.host == 'feeds.example.test'
        key = request.url.path.removeprefix('/')
        calls.append((key, request.headers.get('Authorization')))
        response = responses[key]
        if callable(response):
            return await response(request)
        if isinstance(response, bytes):
            return httpx.Response(200, content=response)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    monkeypatch.setitem(connections._FACTORIES, 'http_json', lambda cfg: http_json.HttpJsonSource(
        http_json.HttpJsonConfig(**cfg), transport=httpx.MockTransport(handler),
    ))
    return responses, calls


def start(client, key):
    response = client.post(f'{OPS_PATH}/{key}/sync', headers=OPS)
    assert response.status_code == 202, response.text
    return response.json()['run_id']


def result(client, run_id):
    response = client.get(f'{OPS_PATH}/sync/{run_id}', headers=OPS)
    assert response.status_code == 200, response.text
    return response.json()


def statuses(client):
    response = client.get(f'{OPS_PATH}/connections', headers=OPS)
    assert response.status_code == 200, response.text
    return {row['source_key']: row for row in response.json()['connections']}


def sync(backend, client, key):
    run_id = start(client, key)
    job = backend.run_job('background')
    assert job and job.payload['run_id'] == run_id
    return result(client, run_id)


def current(dsn):
    with connect(dsn) as conn:
        return {(row['source_key'], row['external_id']): row for row in conn.execute("""
            SELECT s.source_key, s.external_id, s.id AS document_id, r.id AS revision_id,
                f.id AS fragment_id, r.version, r.upstream_url, r.storage_body, f.text
            FROM sources s JOIN source_revisions r ON r.id = s.current_revision_id
            JOIN source_fragments f ON f.revision_id = r.id
            WHERE s.source_system = 'document' AND s.status = 'active' AND f.ordinal = 0
        """)}


def make_due(dsn, *keys):
    with connect(dsn) as conn:
        conn.execute("UPDATE document_connections SET next_due_at = now() - interval '5 days' WHERE source_key = ANY(%s)",
                     (list(keys),))


def ready_retries(dsn):
    with connect(dsn) as conn:
        conn.execute("UPDATE jobs SET available_at = clock_timestamp() WHERE state = 'queued'")


def ask(client, backend, text):
    admitted = client.post(f'/v1/conversations/{uuid4()}/messages', headers=EMPLOYEE,
                           json={'text': text, 'client_message_id': 'v1-acceptance'})
    assert admitted.status_code == 202, admitted.text
    assert backend.run_job('interactive')
    return client.get(f"/v1/turns/{admitted.json()['turn_id']}", headers=EMPLOYEE).json()


def test_two_named_sources_and_existing_citation_path(dsn, upstream, monkeypatch):
    class CitingEngine:
        def answer(self, request):
            fragment = next(part for part in request.sources.fragments if part.text.startswith('A <tag>'))
            citation = Citation(fragment.document_id, fragment.revision_id, fragment.fragment_id, fragment.text)
            return AnswerResult('answer', fragment.text, citations=(citation,))

    responses, calls = upstream
    a_first = document('same', text='A <tag>\n\nNext paragraph & punctuation.')
    a_second = document('excluded', text='Another document.')
    responses.update(a=export(1, a_first, a_second), b=export(1, document('same', text='B content.')))
    monkeypatch.setenv('II_TOKEN_A', 'only-a')
    monkeypatch.setenv('II_TOKEN_B', 'only-b')
    cfg = settings(dsn, {'a': feed('a', interval=60, token='II_TOKEN_A'), 'b': feed('b', token='II_TOKEN_B')})
    backend = Backend(cfg, engine=CitingEngine(), mail_config=MAIL)
    with TestClient(create_app(services=backend)) as client:
        assert statuses(client)['a']['last_run'] is None
        assert sync(backend, client, 'a')['status'] == 'completed'
        assert sync(backend, client, 'b')['status'] == 'completed'
        assert calls == [('a', 'Bearer only-a'), ('b', 'Bearer only-b')]
        assert backend.polling()['document-schedule']() == 0
        first_due = datetime.fromisoformat(statuses(client)['a']['next_due_at'])
        assert datetime.now(UTC) + timedelta(seconds=55) < first_due <= datetime.now(UTC) + timedelta(seconds=60)
        assert statuses(client)['b']['next_due_at'] is None
        initial = current(dsn)
        assert initial['a', 'same']['document_id'] != initial['b', 'same']['document_id']
        assert '&lt;tag&gt;\n\n</p><p>' in initial['a', 'same']['storage_body']
        assert initial['a', 'same']['upstream_url'] == a_first['source_url']
        assert initial['a', 'same']['text'] == 'A <tag>\nNext paragraph & punctuation.'
        cited = ask(client, backend, 'Цитата из нового источника')['result']['sources'][0]
        assert cited['kind'] == 'document' and cited['quote'] == initial['a', 'same']['text']
        assert client.get(cited['url'], headers=EMPLOYEE).json()['text'] == cited['quote']

        reordered = export(1, dict(reversed(list(a_second.items()))), a_first)
        responses['a'] = httpx.Response(200, headers={'Content-Encoding': 'gzip'},
                                       stream=WireBody(gzip.compress(json.dumps(reordered, indent=4).encode())))
        make_due(dsn, 'a', 'b')
        assert backend.polling()['document-schedule']() == 1
        assert backend.run_job('background')
        assert backend.run_job('background') is None
        assert current(dsn) == initial
        assert calls[-1] == ('a', 'Bearer only-a')
        before_manual = statuses(client)['a']['next_due_at']
        responses['a'] = export(2, document('same', 2, text='A edited.'))
        assert sync(backend, client, 'a')['status'] == 'completed'
        updated = current(dsn)
        assert updated['a', 'same']['document_id'] == initial['a', 'same']['document_id']
        assert updated['a', 'same']['revision_id'] != initial['a', 'same']['revision_id']
        assert updated['a', 'same']['fragment_id'] != initial['a', 'same']['fragment_id']
        assert updated['a', 'same']['text'] == 'A edited.' and ('a', 'excluded') not in updated
        assert client.get(cited['url'], headers=EMPLOYEE).status_code == 200
        assert statuses(client)['a']['next_due_at'] == before_manual
        responses['a'] = export(3)
        assert sync(backend, client, 'a')['status'] == 'completed'
        assert current(dsn) == {('b', 'same'): initial['b', 'same']}
        assert client.get(cited['url'], headers=EMPLOYEE).status_code == 404
        with connect(dsn) as conn:
            assert len(list_fragments(conn, 'employee')) == 1
            assert conn.execute('SELECT count(*) AS n FROM document_run_snapshots').fetchone()['n'] == 0
            assert conn.execute("SELECT snapshot_revision FROM document_snapshot_versions WHERE source_key='a'").fetchone()[
                'snapshot_revision'] == 3


class WireBody(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


async def slow_response(_request):
    await asyncio.sleep(5)
    return httpx.Response(200, json=export(2))


@pytest.mark.parametrize(('name', 'expected'), [
    ('malformed', 'invalid_json'), ('duplicates', 'duplicate_document'), ('timeout', 'http_timeout'),
    ('401', 'http_unauthorized'), ('redirect', 'http_redirect'), ('large', 'response_too_large'),
    ('incomplete', 'invalid_catalog'), ('count', 'invalid_catalog'), ('text', 'document_version_conflict'),
    ('title', 'document_version_conflict'), ('url', 'document_version_conflict'), ('older', 'document_version_regression'),
    ('acl', 'invalid_document'), ('faq', 'invalid_document'), ('nul', 'invalid_document'),
    ('surrogate', 'invalid_document'), ('too_many', 'too_many_documents'), ('huge_version', 'invalid_document'),
    ('unindexable_id', 'invalid_document'),
    ('invalid_url_escape', 'invalid_document'), ('invalid_url_host', 'invalid_document'),
])
def test_bad_export_preserves_all_previously_available_data(dsn, upstream, monkeypatch, caplog, name, expected):
    responses, calls = upstream
    previous = document('z-unchanged', 2, text='Private marker from payload.')
    responses['a'] = export(1, previous, document('removed'))
    monkeypatch.setenv('II_TOKEN_A', 'private-marker-token')
    backend = Backend(settings(dsn, {'a': feed('a', token='II_TOKEN_A')}), mail_config=MAIL)
    fresh = document('a-first')
    unindexable_id = 'z-' + ''.join(sha256(str(i).encode()).hexdigest() for i in range(110))
    monkeypatch.setattr(http_json, 'HTTP_BUDGET_SECONDS', 0.12)
    poisoned = {
        'malformed': b'{"bad":"private marker"',
        'duplicates': export(2, fresh, fresh),
        'timeout': slow_response,
        '401': httpx.Response(401, text='private marker'),
        'redirect': httpx.Response(302, headers={'Location': 'https://redirect.example.test/private-marker'}),
        'large': httpx.Response(200, stream=WireBody(gzip.compress(b'A' * (http_json.MAX_BODY_BYTES + 1))),
                                headers={'Content-Encoding': 'gzip'}),
        'incomplete': export(2, fresh, complete=False),
        'count': export(2, fresh, count=0),
        'text': export(2, fresh, previous | {'text': 'Different.'}),
        'title': export(2, fresh, previous | {'title': 'Different.'}),
        'url': export(2, fresh, previous | {'source_url': 'https://kb.example.test/different'}),
        'older': export(2, fresh, previous | {'revision': 1}),
        'acl': export(2, fresh | {'acl_actor_ids': ['employee']}),
        'faq': export(2, fresh | {'kind': 'faq'}),
        'nul': export(2, fresh | {'text': 'null\x00in text'}),
        'surrogate': json.dumps(export(2, fresh | {'text': 'surrogate\ud800in text'})).encode(),
        'too_many': export(2, *(document(str(i)) for i in range(501))),
        'huge_version': export(2, fresh | {'revision': 2**63}),
        'unindexable_id': export(2, fresh, document(unindexable_id)),
        'invalid_url_escape': export(2, fresh, previous | {'source_url': 'https://kb.example.test/%QZ'}),
        'invalid_url_host': export(2, fresh, previous | {'source_url': 'https://kb|invalid.example.test/docs'}),
    }
    with TestClient(create_app(services=backend)) as client, caplog.at_level(logging.DEBUG):
        assert sync(backend, client, 'a')['status'] == 'completed'
        original = current(dsn)
        before = statuses(client)['a']['last_success_at']
        responses['a'] = poisoned[name]
        failure = sync(backend, client, 'a')
        assert failure['status'] == 'incomplete' and failure['error'] == expected
        assert current(dsn) == original
        status = statuses(client)['a']
        assert status['last_run']['error_code'] == expected and status['last_success_at'] == before
        assert 'private marker' not in caplog.text.casefold()
        assert 'private-marker' not in caplog.text and 'example-employee' not in caplog.text
        assert len(calls) == 2
        with connect(dsn) as conn:
            assert conn.execute('SELECT snapshot_revision FROM document_snapshot_versions').fetchone()[
                'snapshot_revision'] == 1
            assert conn.execute('SELECT count(*) AS n FROM document_run_snapshots').fetchone()['n'] == 0
        responses['a'] = export(2, fresh, previous)
        ready_retries(dsn)
        assert backend.run_job('background')
        assert result(client, failure['id'])['status'] == 'completed'
        assert set(current(dsn)) == {('a', 'a-first'), ('a', 'z-unchanged')}


def test_older_or_forked_editions_never_republish_removed_documents(dsn, upstream):
    responses, calls = upstream
    keep = document('keep')
    retired = document('retired', 2)
    responses['a'] = export(3, keep, retired)
    backend = Backend(settings(dsn, {'a': feed('a')}), mail_config=MAIL)
    with TestClient(create_app(services=backend)) as client:
        assert sync(backend, client, 'a')['status'] == 'completed'
        responses['a'] = export(4, keep)
        assert sync(backend, client, 'a')['status'] == 'completed'
        active = current(dsn)
        run = start(client, 'a')
        for payload, code in (
            (export(2), 'snapshot_regression'), (export(3, retired, keep), 'snapshot_regression'),
            (export(4), 'snapshot_conflict'),
        ):
            responses['a'] = payload
            ready_retries(dsn)
            assert backend.run_job('background')
            assert result(client, run)['error'] == code and current(dsn) == active
        responses['a'] = export(5, keep, retired | {'revision': 1})
        assert sync(backend, client, 'a')['error'] == 'document_version_regression'
        responses['a'] = export(5, keep, retired | {'text': 'Overwritten retired content.'})
        ready_retries(dsn)
        assert backend.run_job('background')
        assert result(client, run)['error'] == 'document_version_conflict' and current(dsn) == active
        responses['a'] = export(4, keep)
        ready_retries(dsn)
        assert backend.run_job('background')
        assert result(client, run)['status'] == 'completed' and current(dsn) == active
        responses['a'] = export(5)
        assert sync(backend, client, 'a')['status'] == 'completed' and current(dsn) == {}


@pytest.mark.parametrize('boundary', ['first_page', 'before_missing'])
def test_restart_uses_pinned_inventory_after_worker_loss(dsn, upstream, monkeypatch, boundary):
    class WorkerLost(BaseException):
        pass

    responses, calls = upstream
    responses['a'] = export(1, document('a'), document('b'), document('retired'))
    cfg = settings(dsn, {'a': feed('a')})
    first = Backend(cfg, mail_config=MAIL)
    with TestClient(create_app(services=first)) as client:
        assert sync(first, client, 'a')['status'] == 'completed'
        responses['a'] = export(2, document('a', 2, text='Pinned A.'), document('b', 2, text='Pinned B.'))
        original = first.documents._record_page

        def crash(conn, source_key, run_id, external_id, probe, offset, check_lease, renew):
            if boundary == 'before_missing' and offset is None:
                raise WorkerLost()
            original(conn, source_key, run_id, external_id, probe, offset, check_lease, renew)
            if boundary == 'first_page':
                raise WorkerLost()

        monkeypatch.setattr(first.documents, '_record_page', crash)
        run = start(client, 'a')
        with pytest.raises(WorkerLost):
            first.run_job('background')
        with connect(dsn) as conn:
            assert conn.execute('SELECT count(*) AS n FROM document_run_snapshots').fetchone()['n'] == 1
            conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE state = 'running'")
        after_crash = current(dsn)
        assert after_crash['a', 'a']['text'] == 'Pinned A.' and ('a', 'retired') in after_crash
    first.close()
    responses['a'] = export(3, document('a', 3, text='Next edition.'), document('retired', 2))
    restarted = Backend(cfg, mail_config=MAIL)
    with TestClient(create_app(services=restarted)) as client:
        assert restarted.run_job('background')
        assert result(client, run)['status'] == 'completed' and len(calls) == 2
        pinned = current(dsn)
        assert pinned['a', 'a'] == after_crash['a', 'a']
        assert pinned['a', 'b']['text'] == 'Pinned B.' and ('a', 'retired') not in pinned
        with connect(dsn) as conn:
            assert conn.execute('SELECT count(*) AS n FROM document_run_snapshots').fetchone()['n'] == 0
            assert conn.execute("SELECT count(*) AS n FROM source_revisions WHERE version = 2").fetchone()['n'] == 2
        assert sync(restarted, client, 'a')['status'] == 'completed' and len(calls) == 3
        assert current(dsn)['a', 'a']['text'] == 'Next edition.' and ('a', 'b') not in current(dsn)


def test_schedule_race_retries_and_other_work_continue(dsn, upstream, monkeypatch, tmp_path):
    responses, calls = upstream
    fetching = Event()

    async def unavailable(_request):
        fetching.set()
        await asyncio.sleep(5)
        return httpx.Response(200, json=export(1))

    responses.update(a=unavailable, b=export(1, document('works')))
    monkeypatch.setattr(http_json, 'HTTP_BUDGET_SECONDS', 1.2)
    cfg = settings(dsn, {'a': feed('a', interval=60), 'b': feed('b', interval=120)})
    mail_config = MailConfig(recipient=INTEGRATED_QUALITY_EMAIL, trusted_senders=(INTEGRATED_QUALITY_EMAIL,))
    backend = Backend(cfg, mail_config=mail_config, mailbox=MailSimulator(mail_config))
    assert backend.documents.schedule_due() == 0
    make_due(dsn, 'a', 'b')
    gate = tmp_path / 'start'
    ready = [tmp_path / f'ready-{index}' for index in range(2)]
    config_path = tmp_path / 'config.json'
    config_path.write_text(cfg.model_dump_json(exclude={'database_url'}))
    command = [sys.executable, '-c', """
import sys
from pathlib import Path
from time import monotonic, sleep
from backend.app import Backend
from backend.config import Settings

backend = Backend(Settings.load())
try:
    gate = Path(sys.argv[1])
    Path(sys.argv[2]).touch()
    deadline = monotonic() + 15
    while not gate.exists() and monotonic() < deadline:
        sleep(0.01)
    assert gate.exists()
    print(backend.polling()['document-schedule']())
finally:
    backend.close()
""", str(gate)]
    env = os.environ | {'II_CONFIG': str(config_path), 'DATABASE_URL': dsn, 'MAIL_MODE': 'disabled'}
    with TestClient(create_app(services=backend)) as client:
        children = [subprocess.Popen([*command, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=env) for marker in ready]
        try:
            deadline = monotonic() + 10
            while not all(marker.exists() for marker in ready) and monotonic() < deadline:
                assert all(child.poll() is None for child in children)
                sleep(0.01)
            assert all(marker.exists() for marker in ready)
            gate.touch()
            a_run = start(client, 'a')
            dispatches = 0
            for child in children:
                stdout, stderr = child.communicate(timeout=18)
                assert child.returncode == 0, stderr
                dispatches += int(stdout)
            assert dispatches + backend.documents.schedule_due() == 2
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        assert not calls
        status = statuses(client)
        assert all(datetime.fromisoformat(value['next_due_at']) > datetime.now(UTC) for value in status.values())
        assert status['a']['last_run']['id'] == a_run
        b_run = status['b']['last_run']['id']
        assert backend.documents.schedule_due() == 0 and statuses(client) == status
        assert start(client, 'a') == a_run
        assert start(client, 'b') == b_run
        with connect(dsn) as conn:
            assert conn.execute('SELECT count(*) AS n FROM document_sync_runs').fetchone()['n'] == 2
            assert conn.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'documents.sync'").fetchone()['n'] == 2
            conn.execute("UPDATE jobs SET available_at = now() - interval '1 minute' WHERE payload->>'run_id' = %s", (a_run,))
            mail_id = enqueue(conn, queue='background', kind=POLL_KIND, key='mail-while-feed-offline', payload={})
        with ThreadPoolExecutor(max_workers=1) as pool:
            stalled = pool.submit(backend.run_job, 'background')
            assert fetching.wait(2)
            assert client.get('/health/ready').json()['status'] == 'ready'
            assert ask(client, backend, 'Произвольный интерактивный вопрос')['status'] == 'completed'
            assert backend.run_job('background').payload['run_id'] == b_run
            assert backend.run_job('background').id == mail_id
            assert stalled.result(timeout=3).payload['run_id'] == a_run
        for _ in range(2):
            ready_retries(dsn)
            assert backend.run_job('background').payload['run_id'] == a_run
        assert result(client, a_run)['error'] == 'http_timeout'
        assert result(client, a_run)['status'] == 'incomplete'
        assert result(client, b_run)['status'] == 'completed'
        with connect(dsn) as conn:
            assert conn.execute("SELECT state, attempt FROM jobs WHERE payload->>'run_id' = %s", (a_run,)).fetchone() == {
                'state': 'failed', 'attempt': 3,
            }
            assert conn.execute('SELECT state FROM jobs WHERE id = %s', (mail_id,)).fetchone()['state'] == 'succeeded'
        assert backend.documents.schedule_due() == 0 and backend.run_job('background') is None
        responses['a'] = export(1, document('fixed'))
        make_due(dsn, 'a')
        assert backend.documents.schedule_due() == 1
        assert backend.run_job('background').payload['run_id'] == a_run
        assert result(client, a_run)['status'] == 'completed'


def test_ops_check_reports_only_check_result_and_pause(dsn, upstream, monkeypatch):
    responses, calls = upstream
    responses['a'] = export(1, document('present'))
    cfg = settings(dsn, {'a': feed('a', interval=60, token='CATALOG_TOKEN')})
    backend = Backend(cfg, mail_config=MAIL)
    with TestClient(create_app(services=backend)) as client:
        empty = statuses(client)['a']
        assert empty['last_run'] is None and empty['last_success_at'] is None and empty['last_check_at'] is None
        assert empty['active_documents'] == 0 and empty['last_check_ok'] is None
        for method, path in (
            (client.get, '/connections'), (client.post, '/a/check'), (client.post, '/a/sync'),
            (client.get, f'/sync/{uuid4()}'),
        ):
            assert method(OPS_PATH + path, headers=EMPLOYEE).status_code == 403
        response = client.post(f'{OPS_PATH}/a/check', headers=OPS)
        assert response.status_code == 200 and response.json()['error_code'] == 'catalog_configuration'
        assert not calls
        monkeypatch.setenv('CATALOG_TOKEN', 'sensitive-token')
        assert backend.documents.schedule_due() == 0
        success = sync(backend, client, 'a')
        assert success['status'] == 'completed'
        before = statuses(client)['a']
        records = current(dsn)
        responses['a'] = httpx.Response(503, text='private service fault')
        checked = client.post(f'{OPS_PATH}/a/check', headers=OPS)
        assert checked.status_code == 200 and checked.json()['error_code'] == 'http_status'
        after = statuses(client)['a']
        assert after == before | {'last_check_at': checked.json()['checked_at'], 'last_check_ok': False,
                                  'last_check_error_code': 'http_status'}
        assert current(dsn) == records
        responses['a'] = export(1, document('present'))
        checked = client.post(f'{OPS_PATH}/a/check', headers=OPS).json()
        assert checked['ok'] and checked['error_code'] is None
        assert statuses(client)['a']['last_run']['id'] == success['id']
        assert statuses(client)['a']['last_success_at'] == before['last_success_at']
        assert client.post(f'{OPS_PATH}/unknown/check', headers=OPS).status_code == 404
    first_due = datetime.fromisoformat(before['next_due_at'])
    changed = Backend(settings(dsn, {'a': feed('a', interval=120)}), mail_config=MAIL)
    assert changed.documents.schedule_due() == 0
    changed_due = changed.documents.connections.statuses()[0].next_due_at
    assert changed_due > first_due + timedelta(seconds=59)
    assert changed.documents.schedule_due() == 0 and changed.documents.connections.statuses()[0].next_due_at == changed_due
    restarted = Backend(settings(dsn, {'a': feed('a', interval=120)}), mail_config=MAIL)
    assert restarted.documents.schedule_due() == 0
    assert restarted.documents.connections.statuses()[0].next_due_at == changed_due
    paused = Backend(settings(dsn, {'a': feed('a', interval=120, enabled=False)}), mail_config=MAIL)
    make_due(dsn, 'a')
    assert paused.documents.schedule_due() == 0
    with TestClient(create_app(services=paused)) as client:
        status = statuses(client)['a']
        assert status['enabled'] is False and status['next_due_at'] is None and status['active_documents'] == 1
        assert client.post(f'{OPS_PATH}/a/check', headers=OPS).status_code == 409
        assert client.post(f'{OPS_PATH}/a/sync', headers=OPS).status_code == 409
        assert result(client, success['id'])['status'] == 'completed'
        assert current(dsn) == records
    with connect(dsn) as conn:
        assert conn.execute('SELECT interval_seconds, next_due_at FROM document_connections').fetchone() == {
            'interval_seconds': None, 'next_due_at': None,
        }


def test_legacy_and_named_adapters_hackathon_isolation(dsn, upstream, monkeypatch, tmp_path):
    from backend.documents.confluence import ConfluenceConfig, ConfluenceSource

    responses, calls = upstream
    conf_calls = []

    def confluence(request):
        conf_calls.append((request.url.params['spaceKey'], request.url.params['limit']))
        return httpx.Response(200, json={'results': [], '_links': {}})

    with httpx.Client(transport=httpx.MockTransport(confluence)) as conf_client:
        monkeypatch.setitem(connections._FACTORIES, 'confluence', lambda cfg: ConfluenceSource(
            ConfluenceConfig(**cfg), client=conf_client,
        ))
        monkeypatch.setenv('II_LOCAL_MAILBOX', str(tmp_path / 'demo-mail.sqlite'))
        responses['a'] = export(1, document('corporate'))
        conf = {'base_url': 'https://wiki.example.test', 'spaces': ['A', 'B']}
        batch = [dict(external_id='local', version=1, title='Local', storage_html='<p>Local.</p>')]
        named = {'a': feed('a', interval=60), 'wiki-v1': {'type': 'confluence', 'settings': conf},
                 'second-batch': {'type': 'batch', 'settings': {'pages': batch}}}
        legacy = Backend(settings(dsn, named, confluence=conf, batch=batch), mail_config=MAIL)
        with TestClient(create_app(services=legacy)) as client:
            assert {key: value['type'] for key, value in statuses(client).items()} == {
                'a': 'http_json', 'batch': 'batch', 'confluence': 'confluence',
                'second-batch': 'batch', 'wiki-v1': 'confluence',
            }
            for key in ('confluence', 'wiki-v1', 'batch', 'second-batch'):
                assert client.post(f'{OPS_PATH}/{key}/check', headers=OPS).json()['ok']
                assert sync(legacy, client, key)['status'] == 'completed'
            assert {item for item in conf_calls if item[1] == '1'} == {('A', '1'), ('B', '1')}
            run = sync(legacy, client, 'a')['id']
            queued_run = start(client, 'a')
            assert legacy.documents.schedule_due() == 0
        named_only = Backend(settings(dsn, {'wiki-v1': named['wiki-v1']}), mail_config=MAIL)
        with TestClient(create_app(services=named_only)) as client:
            assert client.get('/v1/ops/integrations', headers=OPS).json()['integrations']['documents']['mode'] == 'network'
        old_calls, old_conf_calls = list(calls), list(conf_calls)
        made_clients = []

        def forbidden(_):
            made_clients.append(True)
            raise AssertionError('Hackathon constructed a network connector')

        for adapter in ('http_json', 'confluence'):
            monkeypatch.setitem(connections._FACTORIES, adapter, forbidden)
        demo_settings = settings(dsn, {'a': feed('a', token='ABSENT_NETWORK_SECRET'), **named}, profile='hackathon',
                                 confluence={'would': 'fail validation'}, batch=batch)
        demo = Backend(demo_settings)
        with TestClient(create_app(services=demo)) as client:
            assert set(statuses(client)) == {'batch'}
            assert not made_clients and 'document-schedule' not in demo.polling() and demo.documents.schedule_due() == 0
            for key in ('a', 'wiki-v1', 'confluence', 'second-batch'):
                assert client.post(f'{OPS_PATH}/{key}/check', headers=OPS).status_code == 404
                assert client.post(f'{OPS_PATH}/{key}/sync', headers=OPS).status_code == 404
            assert client.get(f'{OPS_PATH}/sync/{run}', headers=OPS).status_code == 404
            assert client.get(f'{OPS_PATH}/sync/{queued_run}', headers=OPS).status_code == 404
            remote = current(dsn)['a', 'corporate']
            path = f"/v1/sources/{remote['revision_id']}/fragments/{remote['fragment_id']}"
            assert client.get(path, headers=EMPLOYEE).status_code == 404
            assert sync(demo, client, 'batch')['status'] == 'completed'
            assert demo.run_job('background') is None
            jobs = client.get('/v1/ops/jobs?state=queued', headers=OPS).json()['jobs']
            assert jobs == []
            with connect(dsn) as conn:
                assert {part.text for part in list_fragments(conn, 'employee', hackathon=True)} == {'Local.'}
        assert calls == old_calls and conf_calls == old_conf_calls
        assert legacy.documents.result(queued_run).status == 'queued'


@pytest.mark.parametrize('entry,legacy,error', [
    ({'synthetic-v1': feed('bad')}, {}, 'reserved'),
    ({'batch': feed('bad')}, {}, 'reserved'),
    ({'confluence': feed('bad')}, {'confluence': {'base_url': 'https://wiki.example.test', 'spaces': ['A']}}, 'already'),
    ({'a': {'type': 'unknown-sensitive-setting', 'settings': {}}}, {}, 'Unknown'),
    ({'a': feed('a', interval=True)}, {}, 'Invalid'),
    ({'a': feed('a', interval=59)}, {}, 'Invalid'),
    ({'a': {'type': 'http_json', 'settings': {'url': 'https://user:secret@feed.example.test'}}}, {}, 'Invalid'),
])
def test_invalid_configuration_has_safe_errors(entry, legacy, error):
    with pytest.raises(ValueError, match=error) as exc:
        Backend(settings('postgresql://localhost/unused', entry, **legacy), mail_config=MAIL)
    assert 'secret' not in str(exc.value) and 'sensitive-setting' not in str(exc.value)


def test_http_deadline_covers_stream_after_response_headers(monkeypatch):
    stopped = Event()

    class DrippingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b' '
                await asyncio.sleep(0.015)

        async def aclose(self):
            stopped.set()

    monkeypatch.setattr(http_json, 'HTTP_BUDGET_SECONDS', 0.12)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=DrippingBody()))
    source = http_json.HttpJsonSource(http_json.HttpJsonConfig('https://example.test/catalog', 'all_employees'),
                                     transport=transport)
    with pytest.raises(http_json.HttpJsonError, match='^http_timeout$'):
        source.read_inventory()
    assert stopped.is_set()


def test_http_deadline_does_not_wait_for_a_stuck_dns_lookup(monkeypatch):
    querying, release = Event(), Event()

    def resolve(*_args, **_kwargs):
        querying.set()
        release.wait(3)
        raise socket.gaierror('synthetic resolver failure')

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(http_json, 'HTTP_BUDGET_SECONDS', 0.15)
    source = http_json.HttpJsonSource(http_json.HttpJsonConfig('https://dns.example.test/catalog', 'all_employees'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(source.read_inventory)
        try:
            assert querying.wait(1)
            with pytest.raises(http_json.HttpJsonError, match='^http_timeout$'):
                reading.result(timeout=0.8)
        finally:
            release.set()


def test_trusted_faq_keeps_its_lifecycle_alongside_json_document(dsn, upstream):
    class EscalatingEngine:
        def answer(self, _request):
            return AnswerResult('escalate', 'Передать вопрос в службу качества?')

    responses, _ = upstream
    responses['a'] = export(1, document('procedure', title=QUESTION, text='Срок согласования составляет 7 дней.'))
    mail_config = MailConfig(recipient=INTEGRATED_QUALITY_EMAIL, trusted_senders=(INTEGRATED_QUALITY_EMAIL,))
    mailbox = MailSimulator(mail_config)
    backend = Backend(settings(dsn, {'a': feed('a')}), engine=EscalatingEngine(),
                      mail_config=mail_config, mailbox=mailbox)
    with TestClient(create_app(services=backend)) as client:
        offers = ask(client, backend, QUESTION)['offers']
        assert len(offers) == 1
        offer = offers[0]
        assert client.post(f"/v1/offers/{offer['id']}/confirm", headers=EMPLOYEE,
                           json={'version': offer['version']}).status_code == 200
        assert backend.run_job('background').kind == 'mail.send'
        (sent,) = mailbox.sent
        mailbox.reply(sent, ANSWER, message_id='<approved@example.test>')
        assert backend.mail.poll_inbox() == 1
        with connect(dsn) as conn:
            faq = find_faq(conn, QUESTION, {})
        assert faq.text == ANSWER and faq.kind == 'faq'
        assert sync(backend, client, 'a')['status'] == 'completed'
        document_revision = current(dsn)['a', 'procedure']['revision_id']
        with connect(dsn) as conn:
            same_faq = find_faq(conn, QUESTION, {})
        assert same_faq.revision_id == faq.revision_id and same_faq.conflict_revision_id == document_revision
        responses['a'] = export(2)
        assert sync(backend, client, 'a')['status'] == 'completed'
        with connect(dsn) as conn:
            same_faq = find_faq(conn, QUESTION, {})
        assert same_faq.revision_id == faq.revision_id and same_faq.conflict_revision_id is None
        response = client.post(f'/v1/quality/faq/{faq.faq_id}/withdraw', headers=QUALITY,
                               json={'version': same_faq.version})
        assert response.status_code == 200 and response.json()['status'] == 'withdrawn'
        with connect(dsn) as conn:
            assert find_faq(conn, QUESTION, {}) is None
