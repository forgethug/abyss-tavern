"""Call reduction must preserve persisted results and independent fallbacks."""
from runtime_core.time import utc_now
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest


def forbidden(*args, **kwargs):
    raise AssertionError('unexpected AI call')


def test_policy_only_and_unchanged_short_asset_never_call_provider(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services import s1_description_summary_review_ai_service as audit
    from plugins.adventurer_s1.services.s1_description_ai_service import description_source_hash
    monkeypatch.setattr(audit, 'post_s1_ai_request', forbidden)
    text = '每次使用消耗一枚晶石。'
    key = description_source_hash(text, kind='skill')
    for extra in ({'ai_input_policy_only': True, 'ai_input_policy_required': True},
                  {'summary_decision': 'keep_original', 'description_ai': None, 'trait_context_rule': None}):
        with runtime_app.app_context():
            result, error = audit.audit_description_summary_samples([
                {'review_key': key, 'kind': 'skill', 'name': '测试', 'description': text, **extra}])
        assert error is None
        assert result[key]['ai_input_policy']['decision'] == 'allow'
        assert result[key]['ai_input_policy']['source_hash'] == key
    assert audit.audit_description_summary_samples([
        {'review_key': key}, {'review_key': key}])[1] == 'invalid_input'


def test_mixed_summary_batch_calls_only_for_semantic_work(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services import s1_description_summary_review_ai_service as audit
    seen = []
    def review(items, **kwargs):
        seen.extend(deepcopy(items))
        return ({'long': {'summary_review': {'decision': 'rejected', 'issues': ['遗漏代价']}}}, None)
    monkeypatch.setattr(audit, '_audit_description_candidates', review)
    rows = [
        {'review_key': 'policy', 'description': '原文', 'ai_input_policy_only': True},
        {'review_key': 'long', 'kind': 'skill', 'description': '完整原文与代价',
         'description_ai': '原文', 'summary_decision': 'compressed', 'ai_input_policy_required': True},
    ]
    original = deepcopy(rows)
    result, error = audit.audit_description_summary_samples(rows)
    assert error is None and [row['review_key'] for row in seen] == ['long']
    assert seen[0]['ai_input_policy_required'] is False
    assert result['long']['summary_review']['decision'] == 'rejected'
    assert rows == original
    assert 'ai_input_policy' not in audit._build_summary_sample_audit_user_prompt(seen)
    assert '输入资格' not in audit._build_summary_sample_audit_system_prompt(seen)


def test_completed_description_without_legacy_policy_is_not_requeued(runtime_app, monkeypatch):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache
    from plugins.adventurer_s1.services import s1_description_ai_service as descriptions
    with runtime_app.app_context():
        text = '已验收的完整原文。' * 80
        key = descriptions.description_source_hash(text, kind='skill')
        item = {'source_hash': key, 'kind': 'skill', 'name': '测试', 'description': text}
        row = AdventurerS1DescriptionAiCache(source_hash=key, description_kind='skill',
            status='active', description_ai='已验收摘要', audit_status='passed', ai_input_policy=None)
        db.session.add(row); db.session.commit()
        assert descriptions._description_backlog_complete(item, row)
        assert descriptions._reserve_description_items([item], review_cached_policy=True) == []
        assert row.ai_input_policy is None  # No rewrite of historical evidence.


@pytest.mark.parametrize('axis,expected_summary', [
    ({'decision': 'approved', 'issues': [], 'extra': 'ignored'}, '保留代价的新机制'),
    ({'decision': 'rejected', 'issues': ['遗漏代价']}, None),
    ({'decision': 'approved', 'issues': ['冲突']}, None),
    ({'decision': 'invalid', 'issues': []}, None),
    ({'decision': ['approved'], 'issues': []}, None),
    ({'decision': {'approved': True}, 'issues': []}, None),
    (None, None),
])
def test_merged_review_keeps_new_full_text_when_summary_fails(runtime_app, monkeypatch, axis, expected_summary):
    from plugins.adventurer_s1.services import s1_asset_description_update_ai_service as service
    target = {'update_ref': 'u1', 'asset_kind': 'skill', 'current_full_description': '旧机制与代价。',
              'current_summary': '旧机制', 'proposed_description': '新机制与代价。'}
    full = '当前技能保留原来的代价，并形成了新的护卫机制。'
    results = {'u1': {'full_description': full, 'description_ai': '保留代价的新机制'}}
    raw = {'reviews': [{'update_ref': 'u1', 'decision': 'approved', 'issues': [],
                        'summary_review': axis, 'raw_sql': 'ignored'}], 'grant': 'ignored'}
    parsed, valid = service._parse_update_review_results_with_status(raw, expected_by_ref={'u1': target})
    assert valid and 'raw_sql' not in parsed['u1']
    assert not parsed['u1']['summary_review'] or 'extra' not in parsed['u1']['summary_review']
    call = Mock(return_value=parsed)
    monkeypatch.setattr(service, '_call_asset_description_update_review_ai', call)
    with runtime_app.app_context():
        approved, statuses = service._review_generated_descriptions([target], results,
            expedition_log_id=1, party_id=1, correlation_id='test', batch_number=1)
        merged, fallbacks = service._review_generated_summaries([target], approved, expedition_log_id=1)
    assert call.call_count == 1
    assert statuses['u1'] == 'ai_approved'
    assert merged['u1']['full_description'] == full
    assert merged['u1']['description_ai'] == (expected_summary or full)
    assert fallbacks == (0 if expected_summary else 1)
    assert '_summary_review' not in results['u1']


def test_merged_review_rejects_wrong_reference_and_full_rejection(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services import s1_asset_description_update_ai_service as service
    raw = {'reviews': [{'update_ref': 'wrong', 'decision': 'approved', 'issues': [],
                        'summary_review': {'decision': 'approved', 'issues': []}}]}
    assert service._parse_update_review_results_with_status(raw, expected_by_ref={'u1': {}}) == (None, False)
    monkeypatch.setattr(service, '_call_asset_description_update_review_ai', lambda *a, **k: {
        'u1': {'decision': 'rejected', 'issues': ['删除了旧能力'], 'summary_review': {'decision': 'approved', 'issues': []}}})
    approved, statuses = service._review_generated_descriptions(
        [{'update_ref': 'u1', 'asset_kind': 'skill'}], {'u1': {'full_description': '错误成品'}},
        expedition_log_id=1, party_id=1, correlation_id='test', batch_number=1)
    assert approved == {} and statuses['u1'] == 'review_rejected'


def test_legacy_assembly_exclusion_does_not_hide_item_or_bypass_ticket_rules(runtime_app):
    from plugins.adventurer_s1.services import s1_item_use_condition_service as conditions
    old = {'assembly_authority_review': {'version': 1, 'decision': 'unsupported'}}
    original = deepcopy(old)
    assert conditions.equipment_visible_in_expedition(old, '矿洞')
    assert not conditions.is_equipment_hidden_from_expedition_ai(old)
    constrained = {**old, 'expedition_dungeon': '试炼', 'target_class': '守卫'}
    assert not conditions.equipment_visible_in_expedition(constrained, '矿洞')
    assert not conditions.class_transfer_invitation_matches_dungeon(constrained, '矿洞')
    assert conditions.class_transfer_invitation_matches_dungeon(constrained, '试炼')
    assert conditions.is_equipment_hidden_from_expedition_ai({'item_kind': 'special_dungeon_ticket'})
    assert old == original


@pytest.mark.parametrize('operation', ['learn', 'advance'])
@pytest.mark.parametrize('stale_session', [False, True])
def test_mentor_single_call_persists_exact_skill_and_keeps_five_slots(runtime_app, monkeypatch, operation, stale_session):
    from extensions import db
    from plugins.adventurer_s1.models import (AdventurerS1, AdventurerS1Skill, AdventurerS1BuildingTemplate,
        AdventurerS1BuildingInstance, AdventurerS1BuildingUsageLog)
    from plugins.adventurer_s1.seed import seed_building_templates
    from plugins.adventurer_s1.services import s1_building_ai_service as building
    from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
    from plugins.adventurer_s1.services.s1_building_ai_ownership_service import stamp_current_building_ai_owner
    with runtime_app.app_context():
        seed_building_templates()
        template = AdventurerS1BuildingTemplate.query.filter_by(code='skill_school').first()
        inst = AdventurerS1BuildingInstance(template_id=template.id, status='built')
        adv = AdventurerS1(name='学员', owner_id=42, status='building', hp=100, max_hp=100, stamina=100)
        db.session.add_all([inst, adv]); db.session.flush()
        skills = [AdventurerS1Skill(adventurer_id=adv.id, name=f'旧技能{i}', description='原机制。',
                                  equipped=True, recovery_minutes=0) for i in range(5)]
        db.session.add_all(skills); db.session.flush()
        target_id = skills[0].id
        adv.building_instance_id = inst.id
        adv.building_assigned_at = utc_now() - timedelta(minutes=61)
        extra = {'usage_session_id': 'test-session', 'operation': operation, 'direction': '学习守护',
                 'gold_cost': 300 if operation == 'learn' else 600, 'stamina_cost': 40,
                 'usage_duration_minutes': 60}
        if operation == 'advance':
            extra.update(target_skill_id=target_id, target_skill_name=skills[0].name,
                         target_skill_description=skills[0].description)
        adv.building_usage_extra, _ = stamp_current_building_ai_owner(extra)
        adv_id, inst_id = adv.id, inst.id
        db.session.commit()
        generated = {'direction_reasonable': True, 'reason': '完成授艺。',
            'skill': {'name': '护卫术', 'description': '保留原机制并可守护邻近伙伴。', 'recovery_minutes': 0,
                      'owner_id': 999}, 'narrative': '师傅教成了护卫术。', 'grant_gold': 999}
        call = Mock(return_value=generated)
        monkeypatch.setattr(building, '_call_building_ai', call)
        monkeypatch.setattr(building, '_notify_building_ai_result', lambda *a, **k: None)
        monkeypatch.setattr(S1SkillSchoolService, 'promote_next_waiting', lambda *a, **k: None)
        if stale_session:
            monkeypatch.setattr(building, '_lock_building_usage_for_terminal', lambda *a, **k: None)
        building._call_skill_training_ai_impl(adv_id, inst_id, 'test-session')
        assert call.call_count == 1
        assert call.call_args.kwargs['call_type'] == 'skill_training_ai'
        db.session.expire_all()
        if stale_session:
            assert AdventurerS1Skill.query.filter_by(adventurer_id=adv_id, name='护卫术').count() == 0
            assert AdventurerS1Skill.query.filter_by(adventurer_id=adv_id).count() == 5
            assert AdventurerS1BuildingUsageLog.query.filter_by(adventurer_id=adv_id).count() == 0
            return
        skill = AdventurerS1Skill.query.filter_by(adventurer_id=adv_id, name='护卫术').one()
        assert skill.description == generated['skill']['description']
        assert skill.id == target_id if operation == 'advance' else not skill.equipped
        assert AdventurerS1Skill.query.filter_by(adventurer_id=adv_id, equipped=True).count() == 5
        log = AdventurerS1BuildingUsageLog.query.filter_by(adventurer_id=adv_id).one()
        assert log.result['success'] and 'grant_gold' not in log.result
        assert db.session.get(AdventurerS1, adv_id).status == 'resting'


def test_retired_reviewers_not_registered(runtime_app):
    from services import ai_runtime_config_service as config
    from plugins.adventurer_s1.services import s1_building_ai_service as building
    assert not hasattr(building, '_SKILL_TRAINING_CONTENT_REVIEW_SYSTEM_PROMPT')
    assert not hasattr(building, '_review_item_assembly_authority')
    with runtime_app.app_context():
        assert 'building_skill_training_review' not in str(config.get_ai_provider_config(scope='s1'))


def test_assembly_pipeline_calls_generation_once_and_preserves_consumed_slots(runtime_app, monkeypatch):
    from plugins.adventurer_s1.services import s1_building_ai_service as building
    candidate = {'success': True, 'new_name': '安全扣件', 'new_type': 'tool',
                 'new_desc': '由两件投入物固定而成的便携扣件。', 'new_rarity': 'common',
                 'direction_fit': 'not_requested', 'reason': '', 'narrative': '投入物成为扣件。',
                 'grant_other_owner': 999}
    call = Mock(return_value=candidate)
    monkeypatch.setattr(building, '_call_building_ai', call)
    monkeypatch.setattr(building, '_persist_item_assembly_ai_checkpoint', lambda *a, **k: True)
    with runtime_app.app_context():
        pipeline = building._generate_item_assembly(adv_id=1, building_instance_id=1,
            usage_session_id='assembly-session', input_snapshot_hash='hash', stored_extra={},
            request_payload={'desired_direction': ''}, slot_count=2, input_rarities=['common', 'common'])
    assert pipeline['status'] == 'approved'
    assert pipeline['result']['consumed_slots'] == [1, 2]
    assert 'grant_other_owner' not in pipeline['result']
    assert 'authority_review' not in pipeline
    assert call.call_count == 1 and call.call_args.kwargs['call_type'] == 'item_assembly_workshop_ai'


@pytest.mark.parametrize('summary_approved', [True, False])
def test_description_finalize_uses_two_calls_and_replays_old_receipt(runtime_app, monkeypatch, summary_approved):
    from plugins.adventurer_s1.services import s1_asset_description_update_ai_service as service
    from plugins.adventurer_s1.services import s1_description_summary_review_ai_service as audit
    snapshot = [{'adventurer_id': 7, 'skills': [
        {'asset_id': 12, 'name': '旧技能', 'description': '护卫同伴，每次消耗一枚晶石。'}]}]
    candidate = {'events': ['技能增加了阻挡飞箭的作用，原消耗保持。'], 'member_updates': [
        {'id': 7, 'skill_updates': [{'skill_id': 12, 'name': '旧技能',
         'desc': '新增阻挡飞箭的作用。', 'evidence_event_indexes': [0]}]}]}
    full = '可护卫同伴并阻挡飞箭，每次消耗一枚晶石。'
    summary = '护卫与挡箭，每次耗一枚晶石'
    edit = Mock(side_effect=lambda items, **k: {item['update_ref']: {
        'full_description': full, 'description_ai': summary} for item in items})
    review = Mock(side_effect=lambda items, results, **k: {item['update_ref']: {
        'decision': 'approved', 'issues': [], 'summary_review': {
            'decision': 'approved' if summary_approved else 'rejected',
            'issues': [] if summary_approved else ['遗漏限定']}} for item in items})
    monkeypatch.setattr(service, '_call_asset_description_update_ai', edit)
    monkeypatch.setattr(service, '_call_asset_description_update_review_ai', review)
    monkeypatch.setattr(audit, 'post_s1_ai_request', forbidden)
    with runtime_app.app_context():
        result, changed = service.finalize_s1_existing_asset_descriptions(candidate,
            audit_member_snapshot=snapshot, expedition_log_id=99, party_id=3)
        assert changed and edit.call_count == review.call_count == 1
        assert result['member_updates'][0]['skill_updates'][0]['desc'] == full
        receipt = result[service.S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD]
        assert receipt['records'][0]['description_ai'] == (summary if summary_approved else full)
        assert receipt['review_protocol_version'].endswith('-v4')
        replay = deepcopy(result)
        replay[service.S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD]['review_protocol_version'] = 's1-existing-asset-description-update-review-v3'
        again, changed = service.finalize_s1_existing_asset_descriptions(replay,
            audit_member_snapshot=snapshot, expedition_log_id=99, party_id=3)
        assert not changed and again == replay and edit.call_count == review.call_count == 1
        resolved, projected = service.resolve_s1_finalized_asset_description(
            member_id=7, asset_kind='skill', asset_id=12, frozen_full_description=snapshot[0]['skills'][0]['description'],
            proposed_description=full, receipt=receipt)
        assert resolved == full and projected == (summary if summary_approved else full)


def test_legacy_short_policy_backlog_is_retired_locally(runtime_app, monkeypatch):
    from extensions import db
    from db_repository.s1_description_backlog_repository import remember_description_sources
    from plugins.adventurer_s1.models import AdventurerS1DescriptionAiBacklog, AdventurerS1DescriptionAiCache
    from plugins.adventurer_s1.services.s1_description_backlog_service import submit_description_backlog
    from plugins.adventurer_s1.services import s1_description_ai_service as descriptions
    from plugins.adventurer_s1.services import s1_description_summary_review_ai_service as audit
    monkeypatch.setattr(audit, 'post_s1_ai_request', forbidden)
    with runtime_app.app_context():
        items = [{'source_hash': descriptions.description_source_hash('旧资格任务', kind=kind),
                  'kind': kind, 'name': '测试', 'description': '旧资格任务'}
                 for kind in ('skill', 'companion_policy')]
        remember_description_sources('region_1', items)
        assert AdventurerS1DescriptionAiBacklog.query.count() == 2
        assert submit_description_backlog(realm_code='region_1', max_items=10, max_batches=1) == 0
        assert AdventurerS1DescriptionAiBacklog.query.count() == 0
        assert submit_description_backlog(realm_code='region_1', max_items=10, max_batches=1) == 0
        assert AdventurerS1DescriptionAiCache.query.count() == 0


def test_already_queued_policy_task_reuses_verified_summary_without_ai(runtime_app, monkeypatch):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache
    from plugins.adventurer_s1.services import s1_description_ai_service as descriptions
    from plugins.adventurer_s1.services import s1_description_summary_review_ai_service as audit
    monkeypatch.setattr(audit, 'post_s1_ai_request', forbidden)
    monkeypatch.setattr(descriptions, '_call_description_compress_ai', forbidden)
    with runtime_app.app_context():
        text = '完整机制、限制和代价。' * 80
        key = descriptions.description_source_hash(text, kind='skill')
        row = AdventurerS1DescriptionAiCache(source_hash=key, description_kind='skill',
            status='pending', description_ai='已经审核的摘要', audit_status='passed', ai_input_policy=None)
        db.session.add(row); db.session.commit()
        attempted, stored = descriptions.process_description_compression_task({'items': [
            {'source_hash': key, 'kind': 'skill', 'name': '测试', 'description': text}]})
        assert (attempted, stored) == (1, 1)
        db.session.expire_all()
        row = db.session.get(AdventurerS1DescriptionAiCache, key)
        assert row.status == 'active' and row.audit_status == 'passed'
        assert row.description_ai == '已经审核的摘要'
        assert row.ai_input_policy['decision'] == 'allow'


def test_merged_prompt_has_one_consistent_output_contract(runtime_app):
    from plugins.adventurer_s1.services import s1_asset_description_update_ai_service as service
    prompt = service._ASSET_DESCRIPTION_UPDATE_REVIEW_SYSTEM_PROMPT
    assert '你只审核候选全文' not in prompt
    assert '"summary_review":{' in prompt
    assert '摘要失真不能拒绝已成立的全文变化' in prompt


def test_ecology_diagnostic_retirement_keeps_resource_review_contract(runtime_app):
    from plugins.adventurer_s1.services import adventurer_s1_expedition_observer_ai_service as observer
    scope = observer._build_resource_ecology_review_scope(None)
    assert not scope.get('ecology_identity_review', {}).get('required')
    prompt = observer._RESOURCE_ECOLOGY_OBSERVER_SYSTEM_PROMPT
    assert 'ecology_identity_diagnostic' not in prompt
    for decision in ('preserved', 'state_updated', 'defer_successor', 'regenerate'):
        assert decision in prompt


@pytest.mark.parametrize('fail_grant', [False, True])
def test_assembly_consumption_is_atomic_without_authority_reviewer(runtime_app, monkeypatch, fail_grant):
    from extensions import db
    from plugins.adventurer_s1.models import (AdventurerS1, AdventurerS1WarehouseItem,
        AdventurerS1BuildingTemplate, AdventurerS1BuildingInstance, AdventurerS1BuildingUsageLog)
    from plugins.adventurer_s1.seed import seed_building_templates
    from plugins.adventurer_s1.services.adventurer_s1_building_handler import ItemAssemblyWorkshopHandler
    from plugins.adventurer_s1.services import s1_building_ai_service as building
    from plugins.adventurer_s1.services import s1_item_grant_service as grant
    with runtime_app.app_context():
        seed_building_templates()
        template = AdventurerS1BuildingTemplate.query.filter_by(code='item_assembly_workshop').one()
        inst = AdventurerS1BuildingInstance(template_id=template.id, status='built')
        adv = AdventurerS1(name='构装学员', status='resting', hp=100, max_hp=100, stamina=100)
        db.session.add_all([adv, inst]); db.session.flush()
        rows = [AdventurerS1WarehouseItem(adventurer_id=adv.id, name=name, type='material',
            rarity='common', quantity=1, custom_desc=description)
            for name, description in [('铜片', '可折弯的铜片。'), ('麻绳', '可用于捆扎的麻绳。')]]
        db.session.add_all(rows); db.session.commit()
        prepared, error = ItemAssemblyWorkshopHandler().prepare_start(adv, {
            'item_refs': [{'source': 'warehouse', 'id': row.id, 'name': row.name} for row in rows],
            'layout_mode': 'inside_out'})
        assert error is None
        adv.status = 'building'; adv.building_instance_id = inst.id
        adv.building_assigned_at = utc_now(); adv.building_usage_extra = prepared
        adv_id, inst_id, ids = adv.id, inst.id, [row.id for row in rows]
        db.session.commit()
        call = Mock(return_value={'success': True, 'new_name': '绳扣', 'new_type': 'tool',
            'new_desc': '铜片折成扣，麻绳缠绕，可用于捆扎。', 'new_rarity': 'common',
            'direction_fit': 'not_requested', 'reason': '', 'narrative': '材料完成组装。'})
        monkeypatch.setattr(building, '_call_building_ai', call)
        monkeypatch.setattr(building, '_notify_building_ai_result', lambda *a, **k: None)
        if fail_grant:
            monkeypatch.setattr(grant, 'grant_raw_item', lambda *a, **k: (None, 'test_grant_failure'))
        building.call_item_assembly_workshop_ai(adv_id, inst_id, prepared['item_refs'],
            usage_session_id=prepared['usage_session_id'])
        db.session.expire_all()
        assert call.call_count == 1
        materials = AdventurerS1WarehouseItem.query.filter(AdventurerS1WarehouseItem.id.in_(ids),
            AdventurerS1WarehouseItem.name.in_(['铜片', '麻绳'])).all()
        outputs = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adv_id, name='绳扣').all()
        if fail_grant:
            assert len(materials) == 2 and all(row.quantity == 1 for row in materials)
            assert not outputs
        else:
            assert not materials and len(outputs) == 1
            log = AdventurerS1BuildingUsageLog.query.filter_by(adventurer_id=adv_id).one()
            assert log.result['settlement_receipt']['consumed_slots'] == [1, 2]
            assert log.result['settlement_receipt']['output']['name'] == '绳扣'
            assert 'ai_review' not in log.result


def test_provider_envelope_preserves_merged_summary_decision(runtime_app, monkeypatch):
    import json
    from types import SimpleNamespace
    from requests import Response
    from plugins.adventurer_s1.services import s1_asset_description_update_ai_service as service
    response = Response(); response.status_code = 200
    raw = {'reviews': [{'update_ref': 'u1', 'decision': 'approved', 'issues': [],
                        'summary_review': {'decision': 'rejected', 'issues': ['遗漏代价'], 'extra': 9}}]}
    response._content = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
        'role': 'assistant', 'content': json.dumps(raw, ensure_ascii=False)}}]}, ensure_ascii=False).encode('utf-8')
    response.encoding = 'utf-8'
    monkeypatch.setattr(service, 'resolve_s1_ai_profile_snapshot', lambda *a: SimpleNamespace(
        api_key='test-only', api_base='https://example.invalid/v1', model='test-model'))
    monkeypatch.setattr(service, 'record_token_usage', lambda *a, **k: None)
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        kwargs['response_validator'](service.parse_s1_ai_http_json_response(response))
        return response
    monkeypatch.setattr(service, 'post_s1_ai_request', post)
    item = {'update_ref': 'u1', 'asset_kind': 'skill', 'current_name': '技能', 'proposed_name': '技能',
            'current_full_description': '旧全文', 'proposed_description': '新变化', 'evidence_events': ['变化']}
    with runtime_app.app_context():
        result = service._call_asset_description_update_review_ai([item], {'u1': {
            'full_description': '新全文与旧代价', 'description_ai': '新全文'}},
            expedition_log_id=1, party_id=1, correlation_id='test', batch_number=1)
    assert len(calls) == 1
    assert result['u1']['summary_review'] == {'decision': 'rejected', 'issues': ['遗漏代价']}
    assert 'summary_review_required' in calls[0]['json']['messages'][1]['content']
