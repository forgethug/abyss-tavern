import copy
from itertools import product
from types import SimpleNamespace as Obj

import pytest


def settings(**changes):
    from services.tavern_ai_stage_settings import read_settings, save_settings
    value, error, status = save_settings({**read_settings(), **changes})
    assert status == 200, error
    return value


@pytest.mark.parametrize('flags', list(product([False, True], repeat=3)))
def test_all_stage_combinations_are_independent(runtime_app, flags):
    from services.tavern_ai_stage_settings import FIELDS, enabled
    with runtime_app.app_context():
        settings(**dict(zip(FIELDS, flags)))
        assert tuple(enabled(key) for key in FIELDS) == flags


def test_fate_original_authority_preserves_identity_and_current_state(runtime_app):
    from services.tavern_ai_stage_authority import authorize_fate
    from plugins.adventurer_s1.services import s1_fate_destructive_change_contract as c
    with runtime_app.app_context():
        settings(expedition_observer=False)
        skill = dict(id=10, name='旧技能', description='原本的完整能力。', equipped=True)
        snapshot = [{'adventurer_id': 1, 'skills': [skill]}]
        candidate = {'fate_journey_context': {'event_type': 'final', 'final_attempt_id': 'attempt-1', 'adventurer_id': 1},
            'events': ['角色在最终命途中永久失去旧技能。'],
            'member_updates': [{'id': 1, 'removed_skills': ['旧技能']}]}
        candidate, context, errors = c.prepare_final_fate_destructive_context(candidate, snapshot, expedition_log_id=5)
        assert not errors and context
        candidate[c.FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD] = authorize_fate(context)
        assert not c.final_fate_destructive_change_requires_review(candidate)
        assert c.final_fate_destructive_approved_asset_ids(candidate) == {1: {'trait': set(), 'skill': {10}}}
        member = Obj(id=1, skill_items=[Obj(**skill, adventurer_id=1)], traits=[])
        assert c.validate_final_fate_destructive_settlement(candidate, [member]) == []
        # Persist and resume retains the exact policy authority, without calling AI.
        restored, _, errors = c.prepare_final_fate_destructive_context(copy.deepcopy(candidate), snapshot, expedition_log_id=5)
        assert not errors
        assert c.validate_final_fate_destructive_settlement(restored, [member]) == []
        for mutation in ('events', 'removal', 'signature'):
            tampered = copy.deepcopy(candidate)
            if mutation == 'events':
                tampered['events'][0] = '完全不同的事件'
            elif mutation == 'removal':
                tampered['member_updates'][0]['removed_skills'] = []
            else:
                tampered[c.FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD]['admin_policy']['signature'] = 'fake'
            assert c.validate_final_fate_destructive_settlement(tampered, [member])
        member.skill_items[0].description = '已经变化'
        assert c.validate_final_fate_destructive_settlement(candidate, [member])
        preserved, audit = c.quarantine_unexecutable_final_fate_destructive_changes(candidate, [member])
        assert audit and audit[0]['reason_code'] == 'predecessor_state_changed'
        assert c.validate_final_fate_destructive_settlement(preserved, [member]) == []
        assert not c.final_fate_destructive_approved_asset_ids(preserved)
        member.skill_items[0].description = skill['description']
        member.skill_items[0].adventurer_id = 2
        assert c.validate_final_fate_destructive_settlement(candidate, [member])
        runtime_app.config['GAME_RUNTIME_ID'] = 'other-tavern'
        assert c.validate_final_fate_destructive_settlement(candidate, [member])


def test_same_name_authority_does_not_authorize_modified_candidate(runtime_app):
    from services.tavern_ai_stage_authority import issue
    from plugins.adventurer_s1.services import adventurer_s1_expedition_runner as r
    with runtime_app.app_context():
        settings(expedition_observer=False)
        snapshot = [{'adventurer_id': 1, 'equipment': [{'asset_id': 2, 'name': '旧剑',
            'type': 'weapon', 'rarity': 'common', 'quantity': 1, 'description': '旧剑说明'}]}]
        candidate = {'events': ['旧剑改名为新剑，同时获得另一柄新剑。'], 'audit_member_snapshot': snapshot,
            'member_updates': [{'id': 1, 'equipment_updates': [{'equipment_id': 2, 'name': '新剑', 'desc': '仍是原来的剑'}],
                'new_equipments': {'新剑': {'type': 'weapon', 'rarity': 'common', 'quantity': 1, 'desc': '另一柄剑'}}}]}
        context = r._build_s1_same_name_gain_review_context(candidate, snapshot)
        context_digest = r._s1_same_name_gain_review_context_digest(context)
        candidate[r._S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD] = {'schema_version': 2, 'status': 'skipped_by_admin',
            'context_digest': context_digest, 'reviews': [], 'admin_policy': issue('expedition_observer', 'same_name', context_digest)}
        assert r._validate_s1_same_name_gain_review_receipt(candidate) == []
        tampered = copy.deepcopy(candidate)
        tampered['member_updates'][0]['new_equipments']['新剑']['quantity'] = 100
        assert r._validate_s1_same_name_gain_review_receipt(tampered)
        tampered = copy.deepcopy(candidate)
        tampered['member_updates'][0]['equipment_updates'][0]['equipment_id'] = 999
        assert r._validate_s1_same_name_gain_review_receipt(tampered)
