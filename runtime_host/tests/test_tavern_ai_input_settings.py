from types import SimpleNamespace as Obj

from test_tavern_model_config import make_app
from test_s1_platform_boundaries import _admin_headers

URL = '/api/adventurer-s1/admin/ai/input-settings'


def test_default_auth_validation_cas_and_isolation(tmp_path):
    apps = [make_app(tmp_path / f'{name}.db', name) for name in ('input-a', 'input-b')]
    clients = [app.test_client() for app in apps]
    headers = [_admin_headers(app, client) for app, client in zip(apps, clients)]
    assert clients[0].get(URL).status_code == 401
    assert clients[0].post('/api/adventurer-s1/admin/traits/compress').status_code == 401
    assert clients[0].post('/api/adventurer-s1/admin/traits/compress', headers=headers[0], json={}).status_code == 400
    first = clients[0].get(URL, headers=headers[0])
    assert first.headers['Cache-Control'] == 'private, no-store'
    assert first.json['data'] == {'compression_enabled': False, 'revision': 'default'}
    payload = {**first.json['data'], 'compression_enabled': True}
    assert clients[0].put(URL, headers=headers[0], json={**payload, 'compression_enabled': 'false'}).status_code == 400
    assert clients[0].put(URL, headers=headers[0], json=payload).status_code == 200
    assert clients[0].put(URL, headers=headers[0], json=payload).status_code == 409
    assert clients[1].get(URL, headers=headers[1]).json['data']['compression_enabled'] is False
    restarted = make_app(tmp_path / 'input-a.db', 'input-a')
    from services.tavern_ai_input_settings import enabled
    with restarted.app_context():
        assert enabled() is True
    with apps[1].app_context():
        assert enabled() is False


def test_full_descriptions_and_frozen_receipts(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services import s1_description_ai_service as d
    from plugins.adventurer_s1.services import s1_trait_description_ai_service as t
    from plugins.adventurer_s1.services import s1_trait_context_projection_service as p
    from plugins.adventurer_s1.services import adventurer_s1_prompt_service as prompts
    from services.tavern_ai_input_settings import FLAG
    with runtime_app.app_context():
        full = '完整的机制条件与限制。' * 80
        def asset(kind, ident):
            field = 'custom_desc_ai' if kind == 'item' else 'description_ai'
            return Obj(id=ident, name=kind, description=full, custom_desc=full, equipped=True,
                is_archived=False, type='weapon', **{field: '压缩摘要',
                field + '_source_hash': d.description_source_hash(full, kind=kind)})
        trait, skill, item = asset('trait', 1), asset('skill', 2), asset('item', 3)
        member = Obj(id=1, name='测试冒险者', traits=[trait], skill_items=[skill], equipment_items=[item])
        assert not d.description_ai_summaries_enabled()
        assert t.trait_text_for_expedition_ai(trait) == full
        for obj, kind in ((trait, 'trait'), (skill, 'skill'), (item, 'item')):
            assert d.frozen_asset_description_for_ai(obj, kind=kind) == full
        assert prompts.frozen_asset_description_for_ai is d.frozen_asset_description_for_ai
        # Exercise actual outbound JSON, including the event-focused compact path.
        body = prompts.build_s1_member_block(member, {}, resources_only=True,
            resource_detail_names={'skill': set(), 'item': set(), 'trait': set()}, compact_unfocused_resources=True)
        assert body.count(full) == 3
        assert '压缩摘要' not in body
        assert d.description_text_for_ai(full, kind='skill', summaries_by_hash={
            d.description_source_hash(full, kind='skill'): '压缩摘要'}) == full
        refreshed = d.refresh_frozen_member_description_ai_context([member])
        assert refreshed[0].traits[0].description_ai is None
        assert trait.description_ai == '压缩摘要'  # No mutation of original state.
        monkeypatch.setattr(d, '_load_cache_rows', lambda *a: (_ for _ in ()).throw(AssertionError('unexpected cache query')))
        assert d.process_description_compression_task({'items': []}) == (0, 0)
        assert t.compress_traits([trait]) == (0, 0)
        runtime_app.config[FLAG] = True
        for obj, kind in ((trait, 'trait'), (skill, 'skill'), (item, 'item')):
            assert d.frozen_asset_description_for_ai(obj, kind=kind) == '压缩摘要'
        receipt = d.build_frozen_member_description_projection_receipt([member], item_dict={},
            target_dungeon='test', use_condition_by_name={})
        assert any(row['uses_summary'] for row in receipt['assets'])
        runtime_app.config[FLAG] = False
        restored = d.restore_frozen_member_description_projection([member], receipt)
        assert d.frozen_asset_description_for_ai(restored[0].skill_items[0], kind='skill') == '压缩摘要'
        # A fresh projection remains full text even after an old receipt is replayed.
        assert d.frozen_asset_description_for_ai(skill, kind='skill') == full
        selected = p.build_s1_trait_context_projection([member], objective_outline=None, target_dungeon='test')
        assert selected.receipt['mode'] == 'off'
        assert [x.id for x in selected.members[0].traits] == [1]
        invalid = p.build_s1_trait_context_projection([member], objective_outline=None,
            target_dungeon='test', frozen_receipt={'mode': 'active'})
        assert invalid.receipt['mode'] == 'off'
        # Valid full-input receipts stay full even when compression is enabled later.
        full_receipt = d.build_frozen_member_description_projection_receipt([member], item_dict={},
            target_dungeon='test', use_condition_by_name={})
        runtime_app.config[FLAG] = True
        restored_full = d.restore_frozen_member_description_projection([member], full_receipt)
        repeated_body = prompts.build_s1_member_block(restored_full[0], {}, resources_only=True,
            resource_detail_names={'skill': set(), 'item': set(), 'trait': set()}, compact_unfocused_resources=True)
        assert repeated_body.count(full) == 3
        repeated_selection = p.build_s1_trait_context_projection([member], objective_outline=None,
            target_dungeon='test', frozen_receipt=selected.receipt)
        assert repeated_selection.reused_frozen_receipt
        assert repeated_selection.receipt['mode'] == 'off'


def test_disabled_worker_releases_reservation_without_model_call(runtime_app, monkeypatch):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache
    from plugins.adventurer_s1.services import s1_description_ai_service as d
    from services.tavern_ai_input_settings import FLAG
    def forbidden(*args, **kwargs):
        raise AssertionError('compression AI called while disabled')
    monkeypatch.setattr(d, '_call_description_compress_ai', forbidden)
    with runtime_app.app_context():
        full = '完整的装备条件' * 90
        key = d.description_source_hash(full, kind='item')
        row = AdventurerS1DescriptionAiCache(source_hash=key, description_kind='item',
            description_ai='已有摘要', status='pending', audit_status='passed', processing_token='pending-token')
        db.session.add(row)
        db.session.commit()
        payload = {'items': [{'source_hash': key, 'kind': 'item', 'description': full, 'name': '装备'}]}
        assert d.enqueue_description_compression_items(payload['items']) == 0
        assert d.process_description_compression_task(payload) == (0, 0)
        db.session.expire_all()
        assert row.status == 'retryable' and row.processing_token is None
        assert row.description_ai == '已有摘要'
        assert d.description_text_for_ai(full, kind='item') == full


def test_ordinary_player_cannot_read_or_write_settings(runtime_app, runtime_client):
    headers = _admin_headers(runtime_app, runtime_client)
    response = runtime_client.post('/api/runtime/identity/sync',
        headers={'X-Runtime-Bootstrap-Secret': 'test-bootstrap-secret-private-32-bytes'},
        json={'id': 71, 'username': 'ordinary-player', 'is_admin': False, 'is_active': True})
    assert response.status_code == 200
    assert runtime_client.get(URL, headers=headers).status_code == 403
    assert runtime_client.put(URL, headers=headers,
        json={'compression_enabled': True, 'revision': 'default'}).status_code == 403


def test_concurrent_editors_only_one_saves(runtime_app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from services.tavern_ai_input_settings import save_settings
    barrier = Barrier(2)
    def save():
        with runtime_app.app_context():
            barrier.wait(timeout=5)
            return save_settings({'compression_enabled': True, 'revision': 'default'})[2]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save), pool.submit(save)]
        assert sorted(future.result(timeout=10) for future in futures) == [200, 409]


def test_disabled_feedback_task_releases_its_receipt(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services import s1_description_ai_service as d
    from plugins.adventurer_s1.services import s1_description_feedback_service as feedback
    calls = []
    monkeypatch.setattr(feedback, '_finish', lambda payload, status: calls.append((payload, status)))
    payload = {'generation_mode': 'description_feedback', 'token': 'test-token'}
    with runtime_app.app_context():
        assert d.process_description_compression_task(payload) == (0, 0)
    assert calls == [(payload, 'retryable')]


def test_admin_preview_does_not_present_cached_summaries_as_active(runtime_app, runtime_client):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Trait
    from plugins.adventurer_s1.services.s1_description_ai_service import description_source_hash
    headers = _admin_headers(runtime_app, runtime_client)
    with runtime_app.app_context():
        adventurer = AdventurerS1(name='测试角色', status='resting')
        db.session.add(adventurer)
        db.session.flush()
        full = '完整特质说明' * 80
        trait = AdventurerS1Trait(adventurer_id=adventurer.id, name='测试特质', description=full,
            description_ai='旧缓存摘要', description_ai_source_hash=description_source_hash(full, kind='trait'))
        db.session.add(trait)
        db.session.commit()
        trait_id = trait.id
    response = runtime_client.get(f'/api/adventurer-s1/admin/traits/compress/compare/{trait_id}', headers=headers)
    assert response.status_code == 200, response.json
    row = response.json['data']
    assert row['compression_enabled'] is False
    assert row['main_ai_uses_full_description'] and not row['compressed']
    assert row['description_ai'] is None and row['candidate_description_ai'] == '旧缓存摘要'
    assert not row['cache']['context_effective'] and not row['cache']['summary_effective']
    response = runtime_client.get('/api/adventurer-s1/admin/descriptions/compress/preview?kind=trait', headers=headers)
    assert response.status_code == 200, response.json
    rows = response.json['data']['items']
    assert len(rows) == 1
    assert rows[0]['compression_enabled'] is False and not rows[0]['compressed']
