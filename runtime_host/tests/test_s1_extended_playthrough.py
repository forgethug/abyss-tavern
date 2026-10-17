"""Scripted provider envelopes through real routes, task handlers and consumers.

Transport, task scheduling and selected RNG branches are controlled. Tasks dispatch through the
pack registry after the HTTP transaction commits; no AI result validator or
settlement function is replaced. Database rows establish isolated test setup.
"""
import json
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace

import jwt
import pytest
import requests

from runtime_core.time import utc_now


@pytest.fixture
def scenario(runtime_app, monkeypatch):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1BuildingTemplate, AdventurerS1BuildingInstance
    from plugins.adventurer_s1.seed import seed_all
    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
    from services.async_task_service import AsyncTaskService
    from runtime_core.task_registry import runtime_task_registry

    app = runtime_app
    # Production uses one process per tavern; pytest reuses this interpreter.
    from plugins.adventurer_s1.services.adventurer_s1_service import invalidate_adventurer_detail_cache, invalidate_buildings_list_cache
    invalidate_adventurer_detail_cache()
    invalidate_buildings_list_cache()
    app.config.update(TESTING=False, DEBUG=False,
        ADVENTURER_S1_AI_PROFILES_JSON=json.dumps([{'id': 'default', 'api_base': 'https://fixture.invalid/v1',
            'model': 'fixture', 'provider': 'openai_compatible', 'api_key': 'test-only'}]),
        ADVENTURER_S1_AI_FEATURE_ASSIGNMENTS_JSON='{}')
    client = app.test_client()
    tokens = {}
    for uid in (1, 2, 3, 4, 99):
        response = client.post('/api/runtime/identity/sync',
            headers={'X-Runtime-Bootstrap-Secret': 'test-bootstrap-secret-private-32-bytes'},
            json={'id': uid, 'username': f'player{uid}', 'is_active': True, 'is_admin': uid == 99})
        assert response.status_code == 200
        tokens[uid] = jwt.encode({'user_id': uid, 'iat': utc_now(), 'exp': utc_now() + timedelta(hours=1)},
            app.config['JWT_SECRET_KEY'], algorithm='HS256')
    state = SimpleNamespace(app=app, tasks=[], calls=[], responses={}, members={}, buildings={}, requests=[])
    with app.app_context():
        seed_all()
        for uid in (1, 2, 3, 4):
            member = AdventurerS1(name=f'测试冒险者{uid}', owner_id=uid, is_player_created=True,
                status='resting', hp=100, max_hp=100, stamina=100, strength=20, agility=20, intelligence=20, luck=20)
            db.session.add(member)
            db.session.flush()
            state.members[uid] = member.id
        db.session.commit()
        for member_id in state.members.values():
            result, error = S1CurrencyService.add(member_id, 'gold', 10000, source_type='test_setup', description='测试初始资金')
            assert error is None, error
        for code in ('skill_school', 'item_assembly_workshop', 'ember_kitchen', 'hospital'):
            template = AdventurerS1BuildingTemplate.query.filter_by(code=code).one()
            instance = AdventurerS1BuildingInstance.query.filter_by(template_id=template.id).first()
            if not instance:
                instance = AdventurerS1BuildingInstance(template_id=template.id)
                db.session.add(instance)
            instance.status = 'built'
            instance.disabled = False
            db.session.flush()
            state.buildings[code] = instance.id
        db.session.commit()

    def enqueue(task_type, payload):
        assert runtime_task_registry.is_registered(task_type), task_type
        state.tasks.append((task_type, dict(payload)))
        return True
    monkeypatch.setattr(AsyncTaskService, 'submit_task', enqueue)
    monkeypatch.setattr(AsyncTaskService, 'is_task_enqueued_or_running', lambda *a, **kw: False)
    # This dispatcher models one occupied foreground worker. Background
    # description prewarming waits for later capacity, independently of
    # worker threads started by other tests in the shared pytest interpreter.
    app.config['ADVENTURER_S1_DESCRIPTION_BACKGROUND_WORKER_RESERVE'] = 1
    monkeypatch.setattr(AsyncTaskService, 'get_runtime_status', lambda: {
        'alive': True, 'max_workers': 1, 'inflight_tasks': 1,
        'reserved_tasks': 0, 'queue_depth': len(state.tasks)})

    def post(url, **kwargs):
        payload = json.loads(kwargs['data']) if 'data' in kwargs else kwargs['json']
        feature = payload['messages'][0]['content'].split('当前 feature：', 1)[1].split('；', 1)[0]
        state.calls.append(feature)
        state.requests.append(payload)
        assert feature in state.responses, f'Unexpected AI call: {feature}'
        value = state.responses[feature]
        value = value(payload) if callable(value) else value
        response = requests.Response()
        response.url = url
        response.status_code = 401 if value == 'unauthorized' else 403 if value == 'forbidden' else 200
        response.headers['Content-Type'] = 'application/json'
        response.encoding = 'utf-8'
        body = {'error': {'message': 'Rejected test credential'}} if value in ('unauthorized', 'forbidden') else {
            'choices': [{'message': {'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 100, 'total_tokens': 200}}
        response._content = json.dumps(body, ensure_ascii=False).encode()
        response._content_consumed = True
        return response
    monkeypatch.setattr(app.extensions['model_gateway'], '_post', post)
    monkeypatch.setattr(requests.Session, 'send', lambda *a, **kw: pytest.fail('External HTTP forbidden'))

    def api(method, path, body=None, uid=1, expected=200):
        response = client.open('/api/adventurer-s1/' + path, method=method, json=body,
            headers={'Authorization': 'Bearer ' + tokens[uid]})
        assert response.status_code == expected, (path, response.json)
        return response.json.get('data')

    def drain():
        count = 0
        while state.tasks:
            count += 1
            assert count < 20, state.tasks
            task, payload = state.tasks.pop(0)
            with app.app_context():
                runtime_task_registry.dispatch(task, payload)
                db.session.remove()

    def expire_usage(uid=1):
        with app.app_context():
            member = db.session.get(AdventurerS1, state.members[uid])
            member.building_assigned_at = utc_now() - timedelta(days=1)
            db.session.commit()
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        with app.app_context():
            AdventurerS1Service.auto_complete_expired_usage()
            db.session.commit()
        drain()

    state.api, state.drain, state.expire_usage = api, drain, expire_usage
    yield state
    with app.app_context():
        db.session.remove()


@pytest.mark.parametrize('outcome', ['success', 'unauthorized'])
def test_mentor_route_to_scheduled_completion(scenario, outcome):
    s = scenario
    s.responses['building_skill_training'] = 'unauthorized' if outcome == 'unauthorized' else {
        'direction_reasonable': True, 'reason': '完成练习。',
        'skill': {'name': '护卫术', 'description': '以盾为身旁同伴阻挡一次攻击，使用后需要休息十分钟。', 'recovery_minutes': 10,
            'owner_id': 999}, 'narrative': '导师指导冒险者掌握了护卫术。', 'grant_gold': 999}
    bid = s.buildings['skill_school']
    before = s.api('GET', str(s.members[1]))
    s.api('POST', f'buildings/{bid}/use-start', {'adventurer_id': s.members[1], 'operation': 'learn', 'direction': '学习护卫同伴'}, uid=2, expected=400)
    s.api('POST', f'buildings/{bid}/use-start', {'operation': 'learn', 'direction': '学习护卫同伴'})
    during = s.api('GET', str(s.members[1]))
    assert during['status'] == 'building' and during['gold'] < before['gold']
    s.api('POST', f'buildings/{bid}/use-complete', {}, expected=400)
    s.expire_usage()
    after = s.api('GET', str(s.members[1]))
    assert after['status'] == 'resting'
    assert s.calls == ['building_skill_training']
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1Skill, AdventurerS1BuildingUsageLog, AdventurerS1
    with s.app.app_context():
        skills = AdventurerS1Skill.query.filter_by(adventurer_id=s.members[1], name='护卫术').all()
        assert len(skills) == int(outcome == 'success')
        assert AdventurerS1BuildingUsageLog.query.filter_by(adventurer_id=s.members[1]).count() == 1
        member = db.session.get(AdventurerS1, s.members[1])
        if outcome == 'unauthorized':
            assert after['gold'] == before['gold'] and after['stamina'] == before['stamina']
            assert member.skill_training_retry
        else:
            assert after['gold'] == during['gold']
    s.api('POST', f'buildings/{bid}/use-complete', {}, expected=400)
    assert s.calls == ['building_skill_training']


@pytest.mark.parametrize('review_outcome', ['allow', 'wrong_operation', 'unauthorized'])
def test_appeal_submit_review_apply_and_revert(scenario, review_outcome):
    s = scenario
    s.responses['data_appeal'] = dict(summary='按要求新增技能', player_reject_reason='提出新增方案',
        core_request_satisfied=True, withdrawal_requested=False,
        request_checklist=[dict(label='新增技能', category='skill', is_core=True, status='done', resolution_basis='repair_supported', detail='按要求新增')],
        focus_refs={}, lookup_queries=[], warnings=[], patch={'create_skills': [dict(name='申诉技能', description='可发出柔和的照明光。', equipped=True, recovery_minutes=0, reason='玩家要求')]})
    def review(payload):
        if review_outcome == 'unauthorized':
            return 'unauthorized'
        text = payload['messages'][-1]['content']
        request = json.loads(text[text.index('{'):])
        manifest = request['operation_manifest']
        return dict(verdict_contract_version=2, decision='allow', summary='操作完整', player_reason='方案完整', issues=[],
            verdicts=[dict(operation_id=row['operation_id'] if review_outcome == 'allow' else 'foreign-operation', request_ids=['request_1'], decision='allow', reason='操作匹配') for row in manifest],
            request_verdicts=[dict(request_id='request_1', decision='allow', resolution_basis='repair_supported', reason='方案完整')])
    s.responses['data_appeal_review'] = review
    submitted = s.api('POST', 'data-appeals', {'appeal_category': 'data_fix', 'targets': [], 'player_note': '请增加一项照明技能。'}, expected=201)
    s.api('POST', 'data-appeals', {'appeal_category': 'data_fix', 'player_note': '再次提交'}, expected=400)
    s.drain()
    rows = s.api('GET', 'data-appeals/mine')
    if review_outcome != 'allow':
        from plugins.adventurer_s1.models import AdventurerS1Skill
        assert rows[0]['status'] == 'processing', rows
        with s.app.app_context():
            assert AdventurerS1Skill.query.filter_by(adventurer_id=s.members[1], name='申诉技能').count() == 0
        assert s.calls == ['data_appeal', 'data_appeal_review']
        return
    assert rows[0]['status'] == 'approved', rows
    assert s.api('GET', 'data-appeals/mine', uid=2) == []
    assert s.calls == ['data_appeal', 'data_appeal_review']
    s.responses['data_appeal_player_review'] = {'decision': 'original_ai_correct', 'reason': '新增技能已经落地，原处理符合本次请求。'}
    s.api('POST', f'data-appeals/{submitted["id"]}/ai-review', {'request': '检查是否完成'}, uid=2, expected=400)
    s.api('POST', f'data-appeals/{submitted["id"]}/ai-review', {'request': '检查是否完成'})
    s.drain()
    assert s.api('GET', 'data-appeals/mine')[0]['player_review_status'] == 'done'
    s.api('POST', f'data-appeals/{submitted["id"]}/ai-review', {'request': '重复检查'}, expected=400)
    assert s.calls == ['data_appeal', 'data_appeal_review', 'data_appeal_player_review']
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1Skill
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    with s.app.app_context():
        assert AdventurerS1Skill.query.filter_by(adventurer_id=s.members[1], name='申诉技能').count() == 1
        AdventurerS1DataAppealService.process_ai(submitted['id'])
        db.session.remove()
        assert AdventurerS1Skill.query.filter_by(adventurer_id=s.members[1], name='申诉技能').count() == 1
    s.api('POST', f'admin/data-appeals/{submitted["id"]}/revert', {}, uid=99)
    assert s.api('GET', 'data-appeals/mine')[0]['status'] == 'reverted'
    with s.app.app_context():
        assert AdventurerS1Skill.query.filter_by(adventurer_id=s.members[1], name='申诉技能').count() == 0


@pytest.mark.parametrize('handler', ['item_assembly_workshop', 'ember_kitchen'])
@pytest.mark.parametrize('outcome', ['success', 'lost_delivery', 'unauthorized', 'forbidden', 'gameplay_failure'])
def test_workshop_production_start_and_material_settlement(scenario, monkeypatch, handler, outcome):
    s = scenario
    from extensions import db
    from services.async_task_service import AsyncTaskService
    from plugins.adventurer_s1.models import AdventurerS1WarehouseItem, AdventurerS1BuildingUsageLog
    # Deterministic dispatcher models an available embedded worker, not Redis.
    monkeypatch.setattr(AsyncTaskService, 'can_run_database_startup_recovery', lambda: True)
    assert not AsyncTaskService.has_durable_queue()
    with s.app.app_context():
        materials = [AdventurerS1WarehouseItem(adventurer_id=s.members[1], name=name,
            type='material', rarity='common', quantity=1, custom_desc=desc)
            for name, desc in ([('铜片', '可折弯的铜片。'), ('麻绳', '可用于捆扎的麻绳。')]
                if handler == 'item_assembly_workshop' else [('土豆', '普通可食土豆。'), ('胡萝卜', '普通可食胡萝卜。')])]
        db.session.add_all(materials)
        db.session.commit()
        refs = [{'source': 'warehouse', 'id': item.id, 'name': item.name} for item in materials]
    s.responses['building_item_assembly'] = {'success': True, 'new_name': '绳扣', 'new_type': 'tool',
        'new_desc': '铜片折成扣，麻绳缠绕，可用于捆扎。', 'new_rarity': 'common',
        'direction_fit': 'not_requested', 'reason': '', 'narrative': '材料完成组装。'}
    s.responses['building_ember_kitchen'] = {'is_edible': True, 'new_name': '蔬菜汤',
        'new_desc': '以土豆和胡萝卜炖成的热汤，饮用可以充饥。', 'uses_left': 2, 'narrative': '食材经过慢炖成为热汤。'}
    feature = 'building_item_assembly' if handler == 'item_assembly_workshop' else 'building_ember_kitchen'
    output_name = '绳扣' if handler == 'item_assembly_workshop' else '蔬菜汤'
    if outcome in ('unauthorized', 'forbidden'):
        s.responses[feature] = outcome
    elif outcome == 'gameplay_failure':
        s.responses[feature] = ({'success': False, 'consumed_slots': [1], 'unreasonable_input': False,
            'narrative': '铜片在尝试折弯时损坏，麻绳保留。'} if handler == 'item_assembly_workshop' else
            {'is_edible': False, 'narrative': '食材烧焦，已经无法食用，只好倒掉。'})
    bid = s.buildings[handler]
    request = {'item_refs': refs, 'layout_mode': 'inside_out'} if handler == 'item_assembly_workshop' else {'item_refs': refs, 'cookware_code': 'stew_pot'}
    # No consumer means no costs or input mutations, even in production mode.
    monkeypatch.setattr(AsyncTaskService, 'can_run_database_startup_recovery', lambda: False)
    before = s.api('GET', str(s.members[1]))
    s.api('POST', f'buildings/{bid}/use-start', request, expected=400)
    assert s.api('GET', str(s.members[1]))['gold'] == before['gold']
    monkeypatch.setattr(AsyncTaskService, 'can_run_database_startup_recovery', lambda: True)
    s.api('POST', f'buildings/{bid}/use-start', request)
    if outcome == 'lost_delivery':
        from plugins.adventurer_s1.models import AdventurerS1
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        from plugins.adventurer_s1.services.adventurer_s1_building_ai_recovery_service import AdventurerS1BuildingAiRecoveryService as Recovery
        with s.app.app_context():
            member = db.session.get(AdventurerS1, s.members[1])
            member.building_assigned_at = utc_now() - timedelta(days=1)
            db.session.commit()
            AdventurerS1Service.auto_complete_expired_usage()
            db.session.commit()
            assert s.tasks
            s.tasks.clear()  # Model volatile queue loss after persisted dispatch.
            stats = Recovery.requeue_stuck_building_ai(stale_minutes=1, abort_minutes=3000, startup_recovery=True)
            assert stats['requeued'] == 1, stats
        s.drain()
    else:
        s.expire_usage()
    assert s.api('GET', str(s.members[1]))['status'] == 'resting'
    with s.app.app_context():
        rows = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=s.members[1]).all()
        if outcome in ('unauthorized', 'forbidden'):
            assert {item.name for item in rows} == {ref['name'] for ref in refs}
            assert all(item.quantity == 1 for item in rows)
            assert s.api('GET', str(s.members[1]))['gold'] == before['gold']
        elif outcome == 'gameplay_failure':
            assert [item.name for item in rows] == (['麻绳'] if handler == 'item_assembly_workshop' else [])
            log = AdventurerS1BuildingUsageLog.query.filter_by(adventurer_id=s.members[1]).one()
            assert log.result['success'] is False
            assert s.api('GET', str(s.members[1]))['gold'] < before['gold']
        else:
            assert [item.name for item in rows] == [output_name]
            log = AdventurerS1BuildingUsageLog.query.filter_by(adventurer_id=s.members[1]).one()
            assert log.result['settlement_receipt']['consumed_slots'] == [1, 2]
    assert s.calls == [feature]


def test_hospital_paid_treatment_completes_once_without_ai(scenario):
    s = scenario
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services.adventurer_s1_service import invalidate_adventurer_detail_cache
    with s.app.app_context():
        member = db.session.get(AdventurerS1, s.members[1])
        member.hp = 40
        member.hp_updated_at = utc_now()
        db.session.commit()
        invalidate_adventurer_detail_cache()
    before = s.api('GET', str(s.members[1]))
    bid = s.buildings['hospital']
    s.api('POST', f'buildings/{bid}/use-start', {'treatment_gold': 100})
    during = s.api('GET', str(s.members[1]))
    assert during['gold'] < before['gold']
    s.api('POST', f'buildings/{bid}/use-complete', {}, expected=400)
    s.expire_usage()
    after = s.api('GET', str(s.members[1]))
    assert after['status'] == 'resting' and after['hp'] > before['hp']
    assert after['gold'] == during['gold']
    s.api('POST', f'buildings/{bid}/use-complete', {}, expected=400)
    assert not s.calls


@pytest.mark.parametrize('dungeon', ['转职试炼场', '顿悟秘境', '祛厄药庐', '万象裂隙'])
def test_special_dungeon_departure_failure_refunds_ticket_once(scenario, monkeypatch, dungeon):
    s = scenario
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Equipment, AdventurerS1ExpeditionLog
    from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item
    from plugins.adventurer_s1.services.adventurer_s1_config_service import get_s1_special_dungeon_ticket
    from plugins.adventurer_s1.services import adventurer_s1_expedition_runner as runner
    monkeypatch.setattr(runner, '_roll_special_event', lambda level: (False, False, None, None))
    s.responses['expedition_main'] = 'unauthorized'
    s.api('POST', f'{s.members[1]}/create-special-party', {'dungeon': dungeon}, expected=400)
    ticket = get_s1_special_dungeon_ticket(dungeon)
    with s.app.app_context():
        member = db.session.get(AdventurerS1, s.members[1])
        result, error = grant_catalog_item(member, ticket)
        assert error is None and result, error
        db.session.commit()
    party = s.api('POST', f'{s.members[1]}/create-special-party', {'dungeon': dungeon})
    log_id = s.api('POST', f'admin/party/{party["party_id"]}/force-dispatch', {}, uid=99)['log_id']
    with s.app.app_context():
        assert AdventurerS1Equipment.query.filter_by(adventurer_id=s.members[1], name=ticket).count() == 0
    s.drain()
    result = s.api('GET', f'logs/{log_id}')
    assert result['status'] == 'failed', result
    with s.app.app_context():
        assert AdventurerS1Equipment.query.filter_by(adventurer_id=s.members[1], name=ticket).one().quantity == 1
        full = db.session.get(AdventurerS1ExpeditionLog, log_id).full_log
        assert full['_special_dungeon_ticket_refund']['items'][0]['quantity'] == 1
    s.api('POST', f'admin/expedition/logs/{log_id}/force-complete', {}, uid=99, expected=400)
    assert s.api('GET', str(s.members[1]))['stamina'] == 100
    assert s.calls == ['expedition_main']


def test_unopened_special_dungeon_remains_unavailable(scenario):
    scenario.api('POST', f'{scenario.members[1]}/create-special-party', {'dungeon': '超越之门'}, expected=400)
    assert not scenario.calls


@pytest.mark.parametrize('dungeon', ['万象裂隙', '转职试炼场', '顿悟秘境', '祛厄药庐'])
def test_special_dungeon_full_party_reward_settlement(scenario, monkeypatch, dungeon, caplog):
    s = scenario
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Equipment, AdventurerS1ExpeditionLog, AdventurerS1WarehouseItem, AdventurerS1Skill, AdventurerS1Trait
    from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item
    from plugins.adventurer_s1.services.adventurer_s1_config_service import get_s1_special_dungeon_ticket
    from plugins.adventurer_s1.services import adventurer_s1_expedition_runner as runner
    monkeypatch.setattr(runner, '_roll_special_event', lambda level: (False, False, None, None))
    if dungeon in ('转职试炼场', '顿悟秘境'):
        # Select the successful RNG branch only; contract and settlement are real.
        monkeypatch.setattr(runner, '_roll_special_dungeon_directives', lambda special, members: {
            member.id: 'new_class' if dungeon == '转职试炼场' else 'new_skill' for member in members})
    ticket = get_s1_special_dungeon_ticket(dungeon)
    with s.app.app_context():
        for member_id in s.members.values():
            granted, error = grant_catalog_item(db.session.get(AdventurerS1, member_id), ticket)
            assert granted and not error
            if dungeon == '祛厄药庐':
                db.session.add(AdventurerS1Trait(adventurer_id=member_id, name='寒疾', description='寒气侵体，四肢时常发冷。'))
        db.session.commit()
    party = s.api('POST', f'{s.members[1]}/create-special-party', {'dungeon': dungeon})
    for uid in (2, 3, 4):
        s.api('POST', f'{s.members[uid]}/join-party/{party["party_id"]}', {}, uid=uid)

    def main(payload):
        members = [json.loads(line) for line in payload['messages'][-1]['content'].splitlines() if line.startswith('{"id":')]
        assert len(members) == 4
        path = Path(__file__).resolve().parents[2] / 'backend/tests/fixtures/s1_playthrough/expedition_main.json'
        result = json.loads(path.read_text(encoding='utf-8').replace('哥布林矿坑', '万象裂隙中的矿坑'))
        for member, update, gain_loss in zip(members, result['member_updates'], result['completion_check']['member_gain_loss'], strict=True):
            update['id'] = gain_loss['id'] = member['id']
            update['remaining_hp'] = member['hp']
        if dungeon != '万象裂隙':
            result['gold_settlement']['team_gain'] = {'amount': 0, 'evidence_event_indexes': []}
            result['summary'] = '四人完成本次机缘，安全回到酒馆。'
            events = ['四名冒险者经过入口检查，进入本次机缘所在的场所。']
            for index, update in enumerate(result['member_updates'], 1):
                update.pop('new_equipments', None)
                if dungeon == '转职试炼场':
                    update['new_class'] = '守卫'
                    update['new_traits'] = [{'name': '稳固站姿', 'description': '站稳时更不容易被推倒。', 'asset_dependencies': []}]
                    update['new_skills'] = [{'name': '盾挡', 'description': '举起盾牌抵挡近身攻击。', 'recovery_minutes': 0}]
                    change = '通过守卫试炼，确立守卫职业，练成稳固站姿并学会盾挡'
                elif dungeon == '顿悟秘境':
                    update['new_skills'] = [{'name': '静心术', 'description': '短暂凝神以稳定自身的注意力。', 'recovery_minutes': 0}]
                    change = '在静坐中领悟静心术，能够凝神稳定自身的注意力'
                else:
                    update['removed_traits'] = ['寒疾']
                    change = '接受药庐诊治，体内寒气散尽，寒疾已经彻底治愈'
                events.append(f'【M{index}】{change}。在场的同伴见证了过程，确认其状态稳定后才一起准备返程。')
                update['history_story_append'] = f'【M{index}】{change}，随后与同伴安全返回酒馆。'
                result['completion_check']['member_gain_loss'][index - 1]['gained'] = [change]
            events.append('四人完成各自的机缘，沿原路安全回到酒馆，本次没有获得金币或其他物品。')
            result['events'] = events
        return result
    s.responses['expedition_main'] = main
    def observer(payload):
        request = json.loads(payload['messages'][-1]['content'])
        task = request.get('task_payload', request)
        scope = task['review_scope']
        names = task.get('member_id_to_name', {})
        updates = [{'id': int(mid), 'history_story_append': f'{name}完成{dungeon}的挑战，与同伴安全归来。'} for mid, name in names.items()]
        result = {'needs_regeneration': False, 'regeneration_basis': 'none',
            'regeneration_evidence': {'source_layer': 'none', 'pre_narrative_evidence_refs': [], 'main_event_indexes': []},
            'override_fields': {'member_updates': updates} if updates else {}, 'reason': '本场独立挑战完整，结算对象一致。'}
        trait_scope = scope.get('trait_persistence_review', {})
        if trait_scope.get('required'):
            result['trait_persistence_reviews'] = [{'candidate_ref': ref, 'decision': 'keep_persistent_trait',
                'evidence_event_indexes': [index + 1]} for index, ref in enumerate(trait_scope['candidate_refs'])]
        class_scope = scope.get('class_transfer_review', {})
        if class_scope.get('required'):
            result['class_transfer_reviews'] = [{'member_id': mid, 'decision': 'settlement_consistent'}
                for mid in class_scope['member_ids']]
        return result
    s.responses['expedition_observer'] = observer
    log_id = s.api('POST', f'admin/party/{party["party_id"]}/force-dispatch', {}, uid=99)['log_id']
    s.drain()
    with s.app.app_context():
        diagnostic = db.session.get(AdventurerS1ExpeditionLog, log_id).full_log
    assert s.api('GET', f'logs/{log_id}')['status'] == 'in_progress', (diagnostic.get('generation_failure_detail'), s.calls)
    assert s.calls == ['expedition_main', 'expedition_observer']
    assert not any('AI 调用失败' in record.getMessage() or 'AI 输出契约失败' in record.getMessage()
                   or 'review_failure_degraded' in record.getMessage() for record in caplog.records)
    s.api('POST', f'admin/expedition/logs/{log_id}/force-complete', {}, uid=99)
    with s.app.app_context():
        full = db.session.get(AdventurerS1ExpeditionLog, log_id).full_log
        assert full['settlement_receipt']['status'] == 'passed'
        if dungeon == '万象裂隙':
            assert full['material_rewards_settled']
            plan = full['_material_dungeon_reward_plan']
            assert plan['boxes_per_member'] > 0 and len(plan['members']) == 4
            for row in plan['members']:
                assert sum(item['quantity'] for item in row['items']) == plan['boxes_per_member']
                for item in row['items']:
                    saved = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=row['adventurer_id'], name=item['name']).one()
                    assert saved.quantity == item['quantity']
        for member_id in s.members.values():
            if dungeon == '转职试炼场':
                assert db.session.get(AdventurerS1, member_id).character_class == '守卫'
                assert AdventurerS1Trait.query.filter_by(adventurer_id=member_id, name='稳固站姿', is_archived=False).count() == 1
                assert AdventurerS1Skill.query.filter_by(adventurer_id=member_id, name='盾挡').count() == 1
            elif dungeon == '顿悟秘境':
                assert AdventurerS1Skill.query.filter_by(adventurer_id=member_id, name='静心术').count() == 1
            elif dungeon == '祛厄药庐':
                assert AdventurerS1Trait.query.filter_by(adventurer_id=member_id, name='寒疾', is_archived=False).count() == 0
        assert AdventurerS1Equipment.query.filter_by(name=ticket).count() == 0
    for uid in s.members:
        assert s.api('GET', str(s.members[uid]), uid=uid)['history_entry_count'] == (0 if dungeon == '万象裂隙' else 1)
    before_calls = list(s.calls)
    s.api('POST', f'admin/expedition/logs/{log_id}/force-complete', {}, uid=99, expected=400)
    assert s.calls == before_calls
