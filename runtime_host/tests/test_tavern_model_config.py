import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import requests

from app import create_runtime_app
from conftest import S1_ADAPTER_PATH
from test_s1_platform_boundaries import _admin_headers

URL = '/api/adventurer-s1/admin/ai/config'


def make_app(path, name):
    return create_runtime_app(database_uri=f'sqlite:///{path.as_posix()}', runtime_id=name,
        bootstrap_secret='test-bootstrap-secret-private-32-bytes', seed_on_start=False,
        adapter_path=str(S1_ADAPTER_PATH))


def proposal(client, headers, name='a', secret='fixture-private-key'):
    data = client.get(URL, headers=headers).json['data']
    return {'revision': data['revision'], 'default_profile_id': 'primary',
        'profiles': [{'id': 'primary', 'name': '主剧情', 'provider': 'openai_compatible',
            'api_base': f'https://{name}.example.test/v1', 'model': f'{name}-story', 'api_key': secret,
            'backup_api_keys': [{'id': 'pool-key', 'api_key': 'fixture-pool-private', 'enabled': True}]}],
        'assignments': {'expedition_main': 'primary', 'expedition_outline': 'primary'},
        'reasoning_efforts': {'expedition_main': 'high'}}


def test_roundtrip_secrets_revision_and_consumer(runtime_app, runtime_client):
    headers = _admin_headers(runtime_app, runtime_client)
    before = runtime_client.get(URL, headers=headers)
    assert before.headers['Cache-Control'] == 'private, no-store'
    assert len(before.json['data']['features']) >= 40
    payload = proposal(runtime_client, headers)
    saved = runtime_client.put(URL, headers=headers, json=payload)
    assert saved.status_code == 200, saved.json
    assert b'fixture-private' not in saved.data and b'fixture-pool-private' not in saved.data
    data = saved.json['data']
    assert data['profiles'][0]['api_key_configured'] is True
    with runtime_app.app_context():
        from plugins.adventurer_s1.services.s1_ai_runtime_config_service import resolve_s1_ai_profile_snapshot
        actual = resolve_s1_ai_profile_snapshot('expedition_main')
        assert (actual.model, actual.api_key, actual.reasoning_effort) == ('a-story', 'fixture-private-key', 'high')
        assert actual.backup_api_keys[0]['api_key'] == 'fixture-pool-private'
    # A full public round-trip preserves both primary and pool credentials.
    data['profiles'][0]['name'] = '改名'
    second = runtime_client.put(URL, headers=headers, json=data)
    assert second.status_code == 200
    assert second.json['data']['revision'] != data['revision']
    assert runtime_client.put(URL, headers=headers, json=data).status_code == 409
    with runtime_app.app_context():
        assert resolve_s1_ai_profile_snapshot('expedition_main').api_key == 'fixture-private-key'
    data = second.json['data']
    data['profiles'][0]['clear_api_key'] = True
    cleared = runtime_client.put(URL, headers=headers, json=data)
    assert cleared.status_code == 200 and not cleared.json['data']['profiles'][0]['api_key_configured']


def test_storage_error_never_exposes_credentials(runtime_app, runtime_client, monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError
    from services import tavern_model_management
    h = _admin_headers(runtime_app, runtime_client)
    payload = proposal(runtime_client, h)
    def fail(*args):
        raise OperationalError('INSERT', {'api_key': 'fixture-private-key'}, RuntimeError('storage unavailable'))
    monkeypatch.setattr(tavern_model_management, 'save_catalog', fail)
    result = runtime_client.put(URL, headers=h, json=payload)
    assert result.status_code == result.json['code'] == 503
    assert 'fixture-private-key' not in result.get_data(as_text=True) + caplog.text
    assert result.headers['Cache-Control'] == 'private, no-store'


def test_two_taverns_restart_and_actual_transport(tmp_path, monkeypatch):
    apps = [make_app(tmp_path / f'{name}.db', name) for name in ('a', 'b')]
    clients = [a.test_client() for a in apps]
    headers = [_admin_headers(a, c) for a, c in zip(apps, clients)]
    for name, c, h in zip(('a', 'b'), clients, headers):
        result = c.put(URL, headers=h, json=proposal(c, h, name, f'{name}-only-key'))
        assert result.status_code == 200, result.json
    cross = clients[0].get(URL, headers=headers[0]).json['data']
    assert clients[1].put(URL, headers=headers[1], json=cross).status_code == 409
    restarted = make_app(tmp_path / 'a.db', 'a')
    from plugins.adventurer_s1.services import s1_ai_runtime_config_service as s1
    calls = []
    def post(url, **kw):
        body = json.loads(kw['data']) if 'data' in kw else kw['json']
        calls.append((url, kw['headers']['Authorization'], body['model']))
        response = requests.Response(); response.status_code = 200
        response._content = b'{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]}'
        response._content_consumed = True
        return response
    for app in (restarted, apps[1]):
        monkeypatch.setattr(app.extensions['model_gateway'], '_post', post)
        with app.app_context():
            s1.post_s1_ai_request('https://unused.example.test/v1/chat/completions', feature='expedition_main',
                json={'messages': [{'role': 'user', 'content': 'simulated request'}]})
    assert calls == [('https://a.example.test/v1/chat/completions', 'Bearer a-only-key', 'a-story'),
                     ('https://b.example.test/v1/chat/completions', 'Bearer b-only-key', 'b-story')]


@pytest.mark.parametrize('mutation', ['unknown_feature', 'missing_profile', 'duplicate_id', 'self_backup', 'bad_url', 'bad_bool', 'missing_revision'])
def test_invalid_config_is_atomic(runtime_app, runtime_client, mutation):
    h = _admin_headers(runtime_app, runtime_client)
    original = runtime_client.get(URL, headers=h).json['data']
    data = proposal(runtime_client, h)
    if mutation == 'unknown_feature': data['assignments']['not_registered'] = 'primary'
    if mutation == 'missing_profile': data['assignments']['expedition_main'] = 'missing'
    if mutation == 'duplicate_id': data['profiles'] *= 2
    if mutation == 'self_backup': data['profiles'][0]['backup_profile_id'] = 'primary'
    if mutation == 'bad_url': data['profiles'][0]['api_base'] = 'https://example.test/v1?api_key=secret'
    if mutation == 'bad_bool': data['profiles'][0]['stream_enabled'] = 'false'
    if mutation == 'missing_revision': del data['revision']
    assert runtime_client.put(URL, headers=h, json=data).status_code == 400
    assert runtime_client.get(URL, headers=h).json['data'] == original


@pytest.mark.parametrize('existing', [False, True])
def test_concurrent_editors_only_one_wins(tmp_path, monkeypatch, existing):
    app = make_app(tmp_path / 'race.db', 'race')
    client = app.test_client(); h = _admin_headers(app, client)
    data = proposal(client, h)
    if existing:
        assert client.put(URL, headers=h, json=data).status_code == 200
        data = client.get(URL, headers=h).json['data']
    from services import tavern_model_management as service
    original = service.save_catalog
    barrier = Barrier(2)
    def save(catalog, raw):
        barrier.wait(timeout=10)
        return original(catalog, raw)
    monkeypatch.setattr(service, 'save_catalog', save)
    def write(name):
        payload = copy.deepcopy(data); payload['profiles'][0]['api_key'] = name
        return app.test_client().put(URL, headers=h, json=payload).status_code
    with ThreadPoolExecutor(2) as executor:
        statuses = list(executor.map(write, ('first-secret', 'second-secret')))
    assert sorted(statuses) == [200, 409]


def test_permissions_and_resume_are_tavern_local(tmp_path):
    a, b = [make_app(tmp_path / f'{n}.db', n) for n in ('a', 'b')]
    client = a.test_client(); h = _admin_headers(a, client)
    from services.ai_runtime_config_service import mark_ai_credential_balance_blocked, get_ai_credential_balance_blocks
    for app in (a, b):
        with app.app_context(): mark_ai_credential_balance_blocked(api_key='shared-fixture-key')
    data = client.get(URL, headers=h).json['data']
    assert data['blocked_credential_count'] == 1
    resume = '/api/adventurer-s1/admin/ai/balance/resume'
    assert client.post(resume, headers=h, json=data).status_code == 200
    with a.app_context(): assert not get_ai_credential_balance_blocks()
    with b.app_context(): assert get_ai_credential_balance_blocks()
    assert client.put(URL, json=data).status_code == 401
    client.post('/api/runtime/identity/sync', headers={'X-Runtime-Bootstrap-Secret': 'test-bootstrap-secret-private-32-bytes'},
                json={'id': 71, 'username': 'portable-admin', 'is_admin': False, 'is_active': True})
    assert client.get(URL, headers=h).status_code == 403
    assert client.put(URL, headers=h, json=data).status_code == 403
    assert client.post(resume, headers=h, json=data).status_code == 403
