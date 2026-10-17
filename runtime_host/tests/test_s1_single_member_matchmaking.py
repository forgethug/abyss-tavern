"""Exercise real tickets and dispatch; enqueue AI work without calling a provider."""
import pytest

from test_s1_platform_boundaries import _admin_headers


def prepare(app, client, minimum):
    headers = _admin_headers(app, client)
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import invalidate_matchmaking_read_cache
    with app.app_context():
        invalidate_matchmaking_read_cache()
        member = AdventurerS1(name='单人测试', owner_id=71, is_player_created=True,
            status='resting', hp=100, max_hp=100, stamina=100,
            strength=10, agility=10, intelligence=10, luck=10, party_preference='manual')
        db.session.add(member)
        db.session.commit()
        assert S1SettingsService.set('party_min_size', minimum)[0]
        assert S1SettingsService.set('daily_expedition_quota', 0)[0]
    return headers


def create_ticket(client, headers):
    response = client.post('/api/adventurer-s1/matchmaking/create-party', headers=headers,
        json={'preferred_dungeon': '哥布林矿坑', 'access_mode': 'public', 'allow_solo_fill': False})
    assert response.status_code == 200, response.json
    ticket = response.json['data']['ticket']
    response = client.post(f"/api/adventurer-s1/matchmaking/{ticket['id']}/start", headers=headers, json={})
    assert response.status_code == 200, response.json
    return ticket['id']


@pytest.mark.parametrize('minimum', [1, 4])
def test_group_matching_uses_saved_minimum(runtime_app, runtime_client, minimum):
    headers = prepare(runtime_app, runtime_client, minimum)
    ticket_id = create_ticket(runtime_client, headers)
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1MatchTicket, AdventurerS1Party
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import (
        AdventurerS1MatchmakingService as Matching, _select_min_ready_match_ids, _ticket_payload,
    )
    with runtime_app.app_context():
        ticket = db.session.get(AdventurerS1MatchTicket, ticket_id)
        assert _ticket_payload(ticket)['ready_member_threshold'] == minimum
        assert bool(_select_min_ready_match_ids([ticket], 0)) == (minimum == 1)
        result = Matching.tick_matchmaking(enqueue_automatic=False, dispatch_immediately=False)
        assert result['matched'] == int(minimum == 1)
        db.session.expire_all()
        ticket = db.session.get(AdventurerS1MatchTicket, ticket_id)
        assert ticket.status == ('matched' if minimum == 1 else 'queued')
        if minimum == 1:
            party = db.session.get(AdventurerS1Party, ticket.matched_party_id)
            assert party.target_size == len(party.members) == 1
            assert party.departure_time is not None
            assert Matching.tick_matchmaking(enqueue_automatic=False, dispatch_immediately=False)['matched'] == 0


def test_single_member_dispatch_preserves_ai_pause_and_is_idempotent(runtime_app, runtime_client, monkeypatch):
    headers = prepare(runtime_app, runtime_client, 1)
    create_ticket(runtime_client, headers)
    from services.async_task_service import AsyncTaskService
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import AdventurerS1MatchmakingService
    from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import dispatch_departed_expeditions
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    tasks = []
    def enqueue(kind, payload):
        tasks.append((kind, payload))
        return True
    monkeypatch.setattr(AsyncTaskService, 'submit_task', enqueue)
    with runtime_app.app_context():
        result = AdventurerS1MatchmakingService.tick_matchmaking(enqueue_automatic=False, dispatch_immediately=False)
        assert result['matched'] == 1
        assert S1SettingsService.set('ai_calls_enabled', False)[0]
        paused = dispatch_departed_expeditions()
        assert paused.get('ai_blocked') is True
        assert not tasks
        assert S1SettingsService.set('ai_calls_enabled', True)[0]
        result = dispatch_departed_expeditions()
        assert result['dispatched'] == 1, result
        assert [kind for kind, _ in tasks] == ['ai_s1_expedition']
        assert dispatch_departed_expeditions()['dispatched'] == 0
        assert len(tasks) == 1


def test_saved_minimum_update_unblocks_existing_ticket(runtime_app, runtime_client):
    headers = prepare(runtime_app, runtime_client, 4)
    ticket_id = create_ticket(runtime_client, headers)
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import AdventurerS1MatchmakingService
    before = runtime_client.get('/api/adventurer-s1/matchmaking/my', headers=headers)
    assert before.status_code == 200, before.json
    assert before.json['data']['items'][0]['ready_member_threshold'] == 4
    updated = runtime_client.post('/api/adventurer-s1/admin/expedition/config',
        headers=headers, json={'party_min_size': 1})
    assert updated.status_code == 200, updated.json
    after = runtime_client.get('/api/adventurer-s1/matchmaking/my', headers=headers)
    assert after.json['data']['items'][0]['id'] == ticket_id
    assert after.json['data']['items'][0]['ready_member_threshold'] == 1
    with runtime_app.app_context():
        assert AdventurerS1MatchmakingService.tick_matchmaking(
            enqueue_automatic=False, dispatch_immediately=False)['matched'] == 1


def test_minimum_is_read_from_each_tavern(tmp_path):
    from test_tavern_model_config import make_app
    from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import _match_min_size
    apps = [make_app(tmp_path / f'{n}.db', n) for n in ('solo', 'group')]
    for app, minimum in zip(apps, (1, 4)):
        prepare(app, app.test_client(), minimum)
    for app, minimum in zip(apps * 2, (1, 4, 1, 4)):
        with app.test_request_context():
            assert _match_min_size() == minimum
            assert _match_min_size() == minimum
