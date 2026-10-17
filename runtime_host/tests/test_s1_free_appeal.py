"""Portable appeals accept player-directed changes without historical proof."""
import ast
import copy
import importlib.util
from pathlib import Path


def test_export_removes_only_retired_appeal_proof_block():
    path = Path(__file__).resolve().parents[2] / 'tools/sync_s1_portable_source.py'
    spec = importlib.util.spec_from_file_location('portable_export_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = b'''def _validate_operation_contract_review_result(rows):
    for row in rows:
        normalized = row
        if not balance_limits_enabled():
            normalized_verdicts.append(normalized)
            continue
        require_historical_proof(row)
    return rows
def unrelated(rows):
    for row in rows:
        normalized_verdicts.append(normalized)
        continue
        unrelated_statement(row)
'''
    output = module.project_source('example.py', source).decode()
    ast.parse(output)
    assert 'require_historical_proof' not in output
    assert 'normalized_verdicts.append(normalized)' in output
    assert 'return rows' in output
    assert 'unrelated_statement(row)' in output


def test_free_appeal_contract_and_technical_review(runtime_app):
    from plugins.adventurer_s1.services import adventurer_s1_data_appeal_ai_service as first
    from plugins.adventurer_s1.services import adventurer_s1_data_appeal_review_ai_service as second
    from plugins.adventurer_s1.services import s1_ai_runtime_config_service as gateway
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import build_data_appeal_operation_manifest
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as Appeals
    with runtime_app.app_context():
        assert '负责把玩家诉求转成可执行方案' in first._DATA_APPEAL_DATA_FIX_SYSTEM_PROMPT
        assert '不审核玩家诉求合理性' in second._REVIEW_SYSTEM_PROMPT
        assert '无需验证其合理性' in second._REVERT_REQUEST_SYSTEM_PROMPT
        for feature in ('data_appeal','data_appeal_review','data_appeal_rarity','tavern_relief'):
            capability = gateway._resolve_s1_ai_capability(feature,feature)
            assert '自由申诉权限' in gateway._s1_ai_player_claim_boundary(capability)
        patch = {'currency_adjustments':[{'currency_code':'gold','amount':900,'reason':'玩家要求'}],
                 'create_skills':[{'name':'自选能力','description':'按玩家要求形成的能力','reason':'玩家要求'}]}
        normalized,error = Appeals.normalize_patch(patch,{},narrative_currency_cap=0)
        assert error is None, error
        assert normalized['currency_adjustments'][0]['amount'] == 900
        manifest = build_data_appeal_operation_manifest(normalized)
        candidate = dict(verdict_contract_version=2,decision='allow',summary='按要求修改',player_reason='方案完整',issues=[],
            verdicts=[dict(operation_id=row['operation_id'],request_ids=['request_1'],decision='allow',reason='玩家要求') for row in manifest],
            request_verdicts=[dict(request_id='request_1',decision='allow',resolution_basis='repair_supported',reason='方案完整')])
        accepted,error = second._validate_data_appeal_review_result_with_error(candidate,normalized_patch=normalized,
            snapshot_before={},valid_request_ids={'request_1'})
        assert error is None, error
        assert accepted['decision'] == 'allow'
        candidate['verdicts'][0]['operation_id'] = 'foreign'
        assert second._validate_data_appeal_review_result_with_error(candidate,normalized_patch=normalized,
            snapshot_before={},valid_request_ids={'request_1'})[0] is None


def test_free_request_executes_once_and_can_be_reverted(runtime_app, monkeypatch):
    from extensions import db
    from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Skill, AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as Appeals
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import build_data_appeal_operation_manifest
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_ai_service import _validate_data_appeal_ai_result_with_status
    with runtime_app.app_context():
        member = AdventurerS1(name='自由申请者',owner_id=1,status='resting',is_player_created=True)
        db.session.add(member)
        db.session.flush()
        member_id = member.id
        appeal = AdventurerS1DataAppeal(user_id=1,adventurer_id=member_id,appeal_category='data_fix',
            player_note='给我自选技能，无需过去取得',selected_targets=[],snapshot_before={},
            expedition_evidence={},ai_status='pending',status='processing')
        db.session.add(appeal)
        db.session.commit()
        appeal_id = appeal.id
        raw = dict(summary='按要求新增技能',player_reject_reason='为你提出新增方案',
            core_request_satisfied=True,withdrawal_requested=False,
            request_checklist=[dict(label='新增技能',category='skill',is_core=True,status='done',
                resolution_basis='repair_supported',detail='按要求新增')],focus_refs={},lookup_queries=[],warnings=[],
            patch={'create_skills':[dict(name='自选能力',description='玩家自选的长期主动能力',
                equipped=True,recovery_minutes=0,reason='玩家要求')]})
        primary,valid = _validate_data_appeal_ai_result_with_status(raw)
        assert valid
        monkeypatch.setattr('plugins.adventurer_s1.services.adventurer_s1_data_appeal_ai_service.call_data_appeal_ai',lambda *a,**kw:copy.deepcopy(primary))
        def review(*args,**kwargs):
            manifest = build_data_appeal_operation_manifest(kwargs['normalized_patch'])
            return dict(verdict_contract_version=2,decision='allow',summary='按要求修改',player_reason='方案完整',issues=[],
                verdicts=[dict(operation_id=row['operation_id'],request_ids=['request_1'],decision='allow',reason='玩家要求') for row in manifest],
                request_verdicts=[dict(request_id='request_1',decision='allow',resolution_basis='repair_supported',reason='方案完整')],
                _backend_operation_manifest=manifest)
        monkeypatch.setattr('plugins.adventurer_s1.services.adventurer_s1_data_appeal_review_ai_service.call_data_appeal_review_ai',review)
        monkeypatch.setattr(Appeals,'_emit_data_appeal_changed',lambda *a,**kw:None)
        Appeals.process_ai(appeal_id)
        db.session.remove()
        saved = db.session.get(AdventurerS1DataAppeal,appeal_id)
        assert saved.status == 'approved', (saved.ai_summary,saved.reject_reason,saved.review_ai_summary)
        assert db.session.query(AdventurerS1Skill).filter_by(adventurer_id=member_id,name='自选能力').count() == 1
        audits = db.session.query(AdventurerS1DataAppealAuditLog).filter_by(appeal_id=appeal_id).count()
        Appeals.process_ai(appeal_id)
        assert db.session.query(AdventurerS1Skill).filter_by(adventurer_id=member_id,name='自选能力').count() == 1
        assert db.session.query(AdventurerS1DataAppealAuditLog).filter_by(appeal_id=appeal_id).count() == audits
        result,error = Appeals.revert_appeal(appeal_id,admin_id=1)
        assert error is None,error
        assert result['status'] == 'reverted'
        db.session.remove()
        assert db.session.query(AdventurerS1Skill).filter_by(adventurer_id=member_id,name='自选能力').count() == 0
