import pytest

from extensions import db
from models import Tavern, TavernMembership, TavernAccessPolicy
from services.game_pack_registry import GamePackRegistry
from services.runtime_supervisor_service import RuntimeSupervisorService


def test_application_catalog_contains_only_abyss_tavern(client, make_user):
    _, headers = make_user('catalog_admin', is_admin=True)
    assert list(GamePackRegistry.all()) == ['s1_fantasy']
    response = client.get('/api/admin/game-packs', headers=headers)
    assert [pack['pack_id'] for pack in response.json['data']] == ['s1_fantasy']
    for pack in ('original_anomaly', 'framework_smoke'):
        response = client.post('/api/admin/taverns', headers=headers, json={
            'slug': 'excluded-game', 'display_name': 'Excluded', 'game_pack': pack,
        })
        assert response.status_code == 400


@pytest.mark.parametrize('is_admin', [False, True])
def test_old_game_is_hidden_and_denied_before_runtime_start(app, client, make_user, monkeypatch, is_admin):
    _, admin = make_user('owner', is_admin=True)
    player_id, headers = make_user('visitor', is_admin=is_admin)
    visible = client.post('/api/admin/taverns', headers=admin, json={
        'slug': 'abyss-tavern', 'display_name': '深渊酒馆',
        'is_public': True, 'allow_self_join': True,
    }).json['data']
    old = client.post('/api/admin/taverns', headers=admin, json={
        'slug': 'old-world', 'display_name': 'Old world',
        'is_public': True, 'allow_self_join': True,
    }).json['data']
    with app.app_context():
        row = db.session.get(Tavern, old['id'])
        row.game_pack = 'original_anomaly'
        row.game_pack_release = '0.1.0-mvp'
        db.session.add(TavernMembership(tavern_id=row.id, user_id=player_id, role='member', status='active'))
        db.session.commit()

    def unexpected(*args, **kwargs):
        pytest.fail('Excluded game must not start or receive a proxy request')
    monkeypatch.setattr(RuntimeSupervisorService, 'ensure_ready', unexpected)
    response = client.get('/api/taverns', headers=headers)
    assert [t['id'] for t in response.json['data']] == [visible['id']]
    assert client.post('/api/taverns/old-world/join', headers=headers).status_code == 404
    assert client.get('/api/taverns/old-world/context', headers=headers).status_code == 404
    assert client.get('/api/taverns/old-world/runtime/info', headers=headers).status_code == 404
    assert client.get(f"/api/admin/taverns/{old['id']}/narrative/descriptor", headers=admin).status_code == 404
    for action in ('start', 'restart'):
        assert client.post(f"/api/admin/taverns/{old['id']}/runtime/{action}", headers=admin).status_code == 409
    # Existing rows and policies are preserved; reads do not migrate player data.
    with app.app_context():
        assert db.session.get(Tavern, old['id']).game_pack == 'original_anomaly'
        assert db.session.query(TavernAccessPolicy).filter_by(tavern_id=old['id']).one().is_public
    assert client.post('/api/taverns/abyss-tavern/join', headers=headers).status_code == 200
    assert client.get('/api/taverns/abyss-tavern/context', headers=headers).json['data']['runtime']['game_pack'] == 's1_fantasy'


def test_export_excludes_other_games_and_keeps_runtime_test_fixture():
    from test_release_operations import module
    exporter = module('export_public')
    for path in (
        'game-packs/original-anomaly/manifest.json',
        'game-packs/original-anomaly/original_anomaly/adapter.py',
        'game-packs/framework-smoke/manifest.json',
        'frontend-v2/src/plugins/original_anomaly/runtime.ts',
        'frontend-v2/src/plugins/framework_smoke/runtime.ts',
        'tools/sync-s0-source-snapshot.ps1',
        'runtime_host/tests/test_original_anomaly_runtime.py',
    ):
        assert not exporter.allowed(path)
    assert exporter.allowed('game-packs/s1-fantasy/manifest.json')
    assert exporter.allowed('frontend-v2/src/plugins/adventurer_s1/runtime.ts')
    assert exporter.allowed('runtime_host/tests/fixtures/framework-smoke/runtime_adapter/smoke_adapter.py')
