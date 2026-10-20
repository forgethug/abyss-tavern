from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from test_tavern_model_config import make_app
from test_s1_platform_boundaries import _admin_headers

URL = '/api/adventurer-s1/admin/ai/stage-settings'


def test_settings_auth_isolation_persistence_and_revision(tmp_path):
    from services.tavern_ai_stage_settings import FIELDS, enabled
    apps = [make_app(tmp_path / f'{name}.db', name) for name in ('stage-a', 'stage-b')]
    clients = [app.test_client() for app in apps]
    headers = [_admin_headers(app, client) for app, client in zip(apps, clients)]
    assert clients[0].get(URL).status_code == 401
    response = clients[0].get(URL, headers=headers[0])
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.json['data'] == {**dict.fromkeys(FIELDS, True), 'revision': 'default'}
    original = response.json['data']
    assert clients[0].put(URL, headers=headers[0], json={**original, FIELDS[0]: 'false'}).status_code == 400
    changed = {**original, FIELDS[0]: False, FIELDS[2]: False}
    assert clients[0].put(URL, headers=headers[0], json=changed).status_code == 200
    assert clients[0].put(URL, headers=headers[0], json=original).status_code == 409
    assert clients[1].get(URL, headers=headers[1]).json['data'] == original
    restarted = make_app(tmp_path / 'stage-a.db', 'stage-a')
    with restarted.app_context():
        assert [enabled(field) for field in FIELDS] == [False, True, False]
    with apps[1].app_context():
        assert all(enabled(field) for field in FIELDS)
    response = clients[0].post('/api/runtime/identity/sync',
        headers={'X-Runtime-Bootstrap-Secret': 'test-bootstrap-secret-private-32-bytes'},
        json={'id': 71, 'username': 'ordinary-player', 'is_admin': False, 'is_active': True})
    assert response.status_code == 200
    assert clients[0].get(URL, headers=headers[0]).status_code == 403
    assert clients[0].put(URL, headers=headers[0], json=original).status_code == 403


def test_concurrent_admin_saves_do_not_lose_changes(runtime_app):
    from services.tavern_ai_stage_settings import FIELDS, save_settings
    barrier = Barrier(2)
    def save(field):
        with runtime_app.app_context():
            barrier.wait(timeout=5)
            return save_settings({**dict.fromkeys(FIELDS, True), field: False, 'revision': 'default'})[2]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, field) for field in FIELDS[:2]]
        assert sorted(f.result(timeout=10) for f in futures) == [200, 409]
