"""The portable Pack must not expose a second fixed-realm control plane."""
from datetime import datetime, timedelta, timezone

import jwt


def _admin_headers(app, client):
    response = client.post(
        '/api/runtime/identity/sync',
        headers={'X-Runtime-Bootstrap-Secret': 'test-bootstrap-secret-private-32-bytes'},
        json={'id': 71, 'username': 'portable-admin', 'is_admin': True, 'is_active': True},
    )
    assert response.status_code == 200
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {'user_id': 71, 'iat': now, 'exp': now + timedelta(minutes=5)},
        app.config['JWT_SECRET_KEY'], algorithm='HS256',
    )
    return {'Authorization': f'Bearer {token}'}


def test_fixed_realm_control_routes_are_absent(runtime_app, runtime_client):
    headers = _admin_headers(runtime_app, runtime_client)
    for method, path in (
        ('GET', '/realm-admission'), ('PUT', '/realm-admission'),
        ('POST', '/realm-runtime/restart'),
        ('GET', '/gateway/init'), ('OPTIONS', '/gateway/init'),
        ('GET', '/cross-realm-mirror'), ('POST', '/cross-realm-mirror/arm'),
        ('POST', '/admin/ai/balance'),
    ):
        response = runtime_client.open(
            '/api/adventurer-s1' + path, method=method, headers=headers, json={},
        )
        # GET-only shared paths legitimately return method-not-allowed.
        assert response.status_code in {404, 405}, (method, path, response.status_code)
    assert not any(
        rule.endpoint.endswith((
            'receive_region_2_runtime_socket_relay',
            'admin_preview_ai_config_region_1_sync',
            'admin_apply_ai_config_region_1_sync',
        )) for rule in runtime_app.url_map.iter_rules()
    )


def test_local_game_and_model_reads_still_work(runtime_app, runtime_client):
    headers = _admin_headers(runtime_app, runtime_client)
    for path in ('/init', '/logs', '/admin/ai/config'):
        response = runtime_client.get('/api/adventurer-s1' + path, headers=headers)
        assert response.status_code == 200, (path, response.get_json())
        assert response.json['code'] == 200
    assert runtime_client.get('/api/adventurer-s1/init').status_code == 401
    with runtime_app.app_context():
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import SETTING_KEYS
        assert 'region_2_enabled' not in SETTING_KEYS


def test_public_projection_omits_fixed_server_modules():
    from pathlib import Path
    import runpy

    root = Path(__file__).resolve().parents[2]
    projection = runpy.run_path(str(root / 'tools/s1_platform_projection.py'))
    for relative in projection['OMIT']:
        assert not (root / 'game-packs/s1-fantasy/source-snapshot' / relative).exists()
        if relative.startswith('frontend-v2/'):
            assert not (root / relative).exists()


def test_setting_reschedule_uses_this_runtime_scheduler_even_for_legacy_realm(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    from services.scheduler_service import SchedulerService
    calls = []
    monkeypatch.setattr(SchedulerService, 'reschedule_interval_job', lambda job_id, **kw: (calls.append(job_id) or True, None))
    with runtime_app.app_context():
        runtime_app.config['ADVENTURER_S1_FIXED_REALM_CODE'] = 'region_2'
        updated = S1SettingsService.reschedule_expedition_scheduler_jobs()
    assert updated and updated == calls
