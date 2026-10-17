import json

from services.runtime_supervisor_service import RuntimeSupervisorService


def test_model_config_proxy_requires_admin_and_uses_url_tavern(client, make_user, monkeypatch):
    _, admin = make_user('models-admin', is_admin=True)
    member_id, member = make_user('models-member')
    taverns = [client.post('/api/admin/taverns', headers=admin,
        json={'display_name': name, 'slug': name}).json['data'] for name in ('models-a', 'models-b')]
    client.post(f"/api/admin/taverns/{taverns[0]['id']}/members", headers=admin, json={'user_id': member_id})
    ready, forwarded = [], []
    def ensure(tavern, user):
        ready.append(tavern.slug)
        return True, None
    def proxy(tavern, **kw):
        forwarded.append((tavern.slug, kw['runtime_path'], json.loads(kw['body'])))
        return b'{"code":200,"data":{}}', 200, {'Content-Type': 'application/json', 'Cache-Control': 'private, no-store'}
    monkeypatch.setattr(RuntimeSupervisorService, 'ensure_ready', staticmethod(ensure))
    monkeypatch.setattr(RuntimeSupervisorService, 'proxy_request', staticmethod(proxy))
    for slug in ('models-a', 'models-b'):
        path = f'/api/taverns/{slug}/runtime/admin/ai/config'
        assert client.put(path, headers=member, json={}).status_code == 403
        assert client.get(path, headers=member).status_code == 403
    assert ready == forwarded == []
    for slug in ('models-a', 'models-b'):
        response = client.put(f'/api/taverns/{slug}/runtime/admin/ai/config', headers=admin,
            json={'tavern_id': taverns[1]['id'], 'runtime_id': 'forged-other-tavern'})
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'private, no-store'
    assert [entry[0] for entry in forwarded] == ['models-a', 'models-b']
