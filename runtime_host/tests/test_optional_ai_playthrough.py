import copy
import json

import pytest

from test_s1_extended_playthrough import scenario


def disable(s, **flags):
    settings = s.api('GET', 'admin/ai/stage-settings', uid=99)
    s.api('PUT', 'admin/ai/stage-settings', {**settings, **flags}, uid=99)


def response(checklist, patch=None):
    return dict(summary='初审处理结果', player_reject_reason='按逐项结论处理',
        core_request_satisfied=all(r['status'] == 'done' for r in checklist if r['is_core']), withdrawal_requested=False, request_checklist=checklist,
        focus_refs={}, lookup_queries=[], warnings=[], patch=patch or {})


def row(status, basis, label='处理技能'):
    return dict(label=label, category='skill', is_core=True, status=status, resolution_basis=basis, detail='本项初审结论')


@pytest.mark.parametrize('kind', ['rejected', 'already', 'mixed'])
def test_first_appeal_conclusions_without_second_review(scenario, kind):
    s = scenario
    disable(s, appeal_second_review=False)
    patch = None
    checklist = [row('rejected', 'not_established')]
    if kind == 'already':
        checklist = [row('done', 'already_satisfied_same_case')]
    elif kind == 'mixed':
        checklist = [row('done', 'repair_supported'), row('rejected', 'not_established', '另一项')]
        patch = {'create_skills': [dict(name='政策技能', description='照亮周围。', equipped=True, recovery_minutes=0, reason='新增') ]}
    s.responses['data_appeal'] = response(checklist, patch)
    submitted = s.api('POST', 'data-appeals', {'appeal_category': 'data_fix', 'targets': [], 'player_note': '请处理这两项技能。'}, expected=201)
    s.drain()
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal, AdventurerS1Skill
    with s.app.app_context():
        appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
        assert appeal.status == ('rejected' if kind == 'rejected' else 'approved'), (appeal.ai_summary, appeal.review_ai_summary)
        assert appeal.review_ai_model is None
        review = json.loads(appeal.review_ai_raw_response)
        assert review['skipped_by_admin']
        assert [r['status'] for r in review['request_results']] == [r['status'] for r in checklist]
        assert AdventurerS1Skill.query.filter_by(name='政策技能').count() == int(kind == 'mixed')
    assert s.calls == ['data_appeal']


@pytest.mark.parametrize('invalid', ['type', 'outside_snapshot'])
def test_invalid_primary_proposal_writes_nothing_and_retries_primary(scenario, invalid):
    s = scenario
    disable(s, appeal_second_review=False)
    patch = {'create_skills': [dict(name='不得部分发放', description='完整方案之前不能发放。', equipped=True, recovery_minutes=0)]}
    if invalid == 'type':
        patch['create_skills'].append({'name': '坏类型', 'description': ['不是字符串']})
    else:
        patch['skills'] = {'update': [{'id': 999999, 'description': '不属于冻结目标'}]}
    s.responses['data_appeal'] = response([row('done', 'repair_supported')], patch)
    submitted = s.api('POST', 'data-appeals', {'appeal_category': 'data_fix', 'targets': [], 'player_note': '请修复技能。'}, expected=201)
    s.drain()
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal, AdventurerS1Skill
    with s.app.app_context():
        appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
        assert appeal.status == 'processing', (appeal.ai_raw_response, appeal.review_ai_raw_response)
        assert not AdventurerS1Skill.query.filter_by(name='不得部分发放').count()
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as S
        assert json.loads(appeal.review_ai_raw_response)['pending_stage'] == 'primary_ai'
        if invalid == 'outside_snapshot':
            assert not json.loads(appeal.review_ai_raw_response)['admin_policy']['payload']['contract_valid']
    assert s.calls == ['data_appeal']


@pytest.mark.parametrize('old_review', [False, True])
def test_primary_execution_does_not_apply_extra_supplements(scenario, monkeypatch, old_review):
    s = scenario
    disable(s, appeal_second_review=False)
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal, AdventurerS1Skill
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as S
    monkeypatch.setattr(S, '_build_mechanical_rarity_upgrade_supplement_patch',
        lambda *a, **kw: pytest.fail('disabled review must not add mechanical rarity proposals'))
    s.responses['data_appeal'] = response([row('done', 'repair_supported')], {'create_skills': [
        dict(name='当前原稿', description='只执行本次初审提案。', equipped=True, recovery_minutes=0)]})
    submitted = s.api('POST', 'data-appeals', {'appeal_category': 'data_fix', 'targets': [], 'player_note': '请恢复技能。'}, expected=201)
    if old_review:
        with s.app.app_context():
            appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
            appeal.review_supplement_patch = {'tavern_subsidy': {'amount': 99999, 'adventurer_id': s.members[1], 'expedition_log_id': 999}}
            db.session.commit()
        monkeypatch.setattr(S, '_apply_tavern_subsidy', lambda *a, **kw: pytest.fail('old supplement executed'))
    s.drain()
    with s.app.app_context():
        appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
        assert appeal.status == 'approved', (appeal.ai_raw_response, appeal.review_ai_raw_response)
        assert not appeal.review_supplement_patch
        assert AdventurerS1Skill.query.filter_by(name='当前原稿').count() == 1
    assert s.calls == ['data_appeal']


def test_primary_destructive_and_description_clear_land_once(scenario):
    s = scenario
    disable(s, appeal_second_review=False)
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal, AdventurerS1Skill, AdventurerS1WarehouseItem, AdventurerS1Item
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as S
    with s.app.app_context():
        skill = AdventurerS1Skill(adventurer_id=s.members[1], name='待移除技能', description='失效的旧能力。', equipped=True)
        item = AdventurerS1WarehouseItem(adventurer_id=s.members[1], name='待清空描述', type='material', rarity='common', quantity=1, custom_desc='旧描述')
        catalog = AdventurerS1Item(name='待清空描述', type='material', rarity='common', description='回退到图鉴中的完整说明。')
        db.session.add_all([skill, item, catalog]); db.session.commit()
        sid, iid = skill.id, item.id
    patch = {'skill_destructive_actions': [{'source_id': sid, 'mode': 'delete_only', 'reason': '按请求移除'}],
        'warehouse': {'update': [{'id': iid, 'custom_desc': '', 'quantity': 2}]}}
    s.responses['data_appeal'] = response([row('done', 'repair_supported')], patch)
    submitted = s.api('POST', 'data-appeals', {'appeal_category': 'data_fix',
        'targets': [{'type': 'skill', 'id': sid}, {'type': 'warehouse', 'id': iid}],
        'player_note': '删除指定旧技能，并清空指定物品自定义描述、恢复数量为2。'}, expected=201)
    s.drain()
    with s.app.app_context():
        appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
        assert appeal.status == 'approved', appeal.review_ai_summary
        assert db.session.get(AdventurerS1Skill, sid) is None
        saved = db.session.get(AdventurerS1WarehouseItem, iid)
        assert not saved.custom_desc and saved.quantity == 2
        S.process_ai(appeal.id)
        assert db.session.get(AdventurerS1WarehouseItem, iid).quantity == 2
    assert s.calls == ['data_appeal']


def test_execution_failure_rolls_back_then_resumes_without_ai(scenario, monkeypatch):
    s = scenario
    disable(s, appeal_second_review=False)
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal, AdventurerS1Skill
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as S
    original = S._apply_supplement_and_distributions
    attempts = []
    def fail_once(*args, **kwargs):
        attempts.append(1)
        return (None, 'injected transaction failure') if len(attempts) == 1 else original(*args, **kwargs)
    monkeypatch.setattr(S, '_apply_supplement_and_distributions', fail_once)
    extra = row('rejected', 'not_established', '非核心项目')
    extra['is_core'] = False
    s.responses['data_appeal'] = response([row('done', 'repair_supported'), extra], {'create_skills': [
        dict(name='仅一次写入', description='回滚后恢复执行。', equipped=True, recovery_minutes=0)]})
    submitted = s.api('POST', 'data-appeals', {'appeal_category': 'data_fix', 'targets': [], 'player_note': '恢复技能，其他项次要。'}, expected=201)
    s.drain()
    with s.app.app_context():
        appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
        review = json.loads(appeal.review_ai_raw_response)
        assert review['pending_stage'] == 'execution', review
        assert S._appeal_frozen_review_ready_for_execution(appeal)
        assert AdventurerS1Skill.query.filter_by(name='仅一次写入').count() == 0
        result, error = S.retry_ai(appeal.id)
        assert not error and result['resume_stage'] == 'execution'
    s.drain()
    with s.app.app_context():
        appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
        review = json.loads(appeal.review_ai_raw_response)
        assert appeal.status == 'approved' and appeal.ai_status == 'done'
        assert appeal.core_request_satisfied is True
        assert review['resolution_state'] == 'completed'
        assert 'pending_stage' not in review and '_backend_auto_retry_after' not in review
        assert review['request_results'][0]['detail'] == '本项初审结论'
        assert AdventurerS1Skill.query.filter_by(name='仅一次写入').count() == 1
    assert s.calls == ['data_appeal'] and len(attempts) == 2


def test_pending_primary_retries_exhaust_existing_budget(scenario):
    s = scenario
    disable(s, appeal_second_review=False)
    s.app.config['ADVENTURER_S1_DATA_APPEAL_AUTO_RETRY_MAX_MODEL_RETRIES'] = 2
    s.responses['data_appeal'] = response([row('partial', 'partially_supported')])
    submitted = s.api('POST', 'data-appeals', {'appeal_category': 'data_fix', 'targets': [], 'player_note': '请完成修复。'}, expected=201)
    s.drain()
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as S
    with s.app.app_context():
        for expected in (1, 2, 3):
            appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
            review = json.loads(appeal.review_ai_raw_response)
            assert review['_backend_auto_retry_attempts']['primary_ai'] == expected, review
            if expected < 3:
                # Model a scheduler dispatch after the persisted backoff elapsed.
                review['_backend_auto_retry_after'] = '2000-01-01T00:00:00Z'
                appeal.review_ai_raw_response = json.dumps(review)
                db.session.commit()
                S.process_ai(appeal.id, invoke_reason='auto_retry')
                db.session.remove()
        appeal = db.session.get(AdventurerS1DataAppeal, submitted['id'])
        assert appeal.ai_status == 'failed'
        db.session.commit()
        S.process_ai(appeal.id, invoke_reason='auto_retry')
        assert appeal.ai_status == 'failed'
    assert s.calls == ['data_appeal'] * 3


def test_shared_loot_policy_registration_is_exact_and_idempotent(scenario):
    s = scenario
    disable(s, appeal_second_review=False)
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal, AdventurerS1ExpeditionLog
    from plugins.adventurer_s1.team_loot_models import AdventurerS1TeamLootLot
    from plugins.adventurer_s1.services.s1_team_loot_service import S1TeamLootService, TeamLootError
    from plugins.adventurer_s1.services.s1_team_loot_service import team_loot_appeal_source_hash
    from services.tavern_appeal_policy import create_policy
    with s.app.app_context():
        full = {'events': ['小队带回两块铜锭，暂未分配。'], 'audit_member_snapshot': [
            {'adventurer_id': mid, 'owner_id': uid, 'name': f'测试冒险者{uid}'} for uid, mid in s.members.items()]}
        log = AdventurerS1ExpeditionLog(target_dungeon='测试矿坑', status='completed', full_log=full)
        db.session.add(log); db.session.flush()
        loot = [{'name': '铜锭', 'source_event_index': 0, 'evidence_event_indexes': [0],
            'item': {'type': 'material', 'rarity': 'common', 'desc': '普通铜锭。', 'quantity': 2}}]
        patch = {'unassigned_items': loot}
        appeal = AdventurerS1DataAppeal(user_id=1, adventurer_id=s.members[1], selected_targets=[], snapshot_before={},
            expedition_log_id=log.id, expedition_evidence={'_team_loot_source_hash': team_loot_appeal_source_hash(full)},
            proposed_patch=patch)
        db.session.add(appeal); db.session.flush()
        policy = create_policy(appeal, response([row('done', 'repair_supported')], patch), patch, None, contract_valid=True)
        appeal.review_ai_raw_response = json.dumps(policy)
        S1TeamLootService.register_appeal_in_transaction(appeal, loot)
        db.session.flush()
        S1TeamLootService.register_appeal_in_transaction(appeal, loot)
        assert AdventurerS1TeamLootLot.query.filter_by(expedition_log_id=log.id).count() == 1
        forged = copy.deepcopy(loot); forged[0]['item']['quantity'] = 200
        with pytest.raises(TeamLootError):
            S1TeamLootService.register_appeal_in_transaction(appeal, forged)
        log.full_log = {**full, 'events': ['改变后的事件']}
        with pytest.raises(TeamLootError):
            S1TeamLootService.register_appeal_in_transaction(appeal, loot)


@pytest.mark.parametrize('enabled', [False, True])
def test_high_risk_stage_keeps_outline_and_gates_both_auxiliaries(scenario, monkeypatch, enabled):
    s = scenario
    disable(s, high_risk_adjudication=enabled)
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1
    from plugins.adventurer_s1.services import adventurer_s1_expedition_runner as r
    calls = []
    def outline(*args, **kwargs):
        calls.append('outline')
        return {'objective_situation': '危险的试炼已经开始，出口仍然开放。'}
    def resolution(**kwargs):
        calls.append('resolution')
        return None
    monkeypatch.setattr(r, '_call_s1_objective_outline_ai', outline)
    monkeypatch.setattr(r, '_call_s1_expedition_event_resolution_ai', resolution)
    monkeypatch.setattr(r, '_call_s1_expedition_death_resource_review_ai', lambda **kw: pytest.fail('unexpected resource review'))
    s.responses['expedition_main'] = 'unauthorized'
    with s.app.app_context():
        member = db.session.get(AdventurerS1, s.members[1])
        snapshots, _ = r._build_s1_expedition_member_snapshots([member])
        def run_main():
            return r._call_main_expedition_ai(snapshots, {}, '顿悟秘境', {'min_level': 1, 'max_level': 999},
                has_special_event=True, special_event_type='danger', special_event_risk_tier='red',
                is_extreme_crisis=True, special_dungeon_context={'type': 'test'}, skip_length_checks=True)
        if enabled:
            result, error = run_main()
            assert result is None and error
        else:
            from requests.exceptions import HTTPError
            with pytest.raises(HTTPError, match='401'):
                run_main()
    assert calls == (['outline', 'resolution'] if enabled else ['outline'])
    assert s.calls == ([] if enabled else ['expedition_main'])
