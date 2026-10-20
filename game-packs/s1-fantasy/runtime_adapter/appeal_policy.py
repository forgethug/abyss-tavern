"""Execute the first adjudicator's proposal when independent review is disabled.

One atomic proposal is authorized. No request-to-operation semantic mapping or
independent reviewer judgment is invented by the server.
"""
import copy
import json

from runtime_core.time import utc_now
from services.tavern_ai_stage_authority import digest, issue, verify


def create_policy_service(app):
    from types import SimpleNamespace
    return SimpleNamespace(explicit_clear_ids=explicit_clear_ids, create_policy=create_policy,
        payload=payload, parse_policy=parse_policy, finalize=finalize)


def explicit_clear_ids(patch):
    return {section: {row['id'] for row in (patch.get(section) or {}).get('update', [])
        if isinstance(row, dict) and type(row.get('id')) is int and row.get('custom_desc') == ''}
        for section in ('equipment', 'warehouse')}


def create_policy(appeal, raw, patch, error, *, contract_valid):
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import build_data_appeal_operation_manifest
    checklist = copy.deepcopy(raw.get('request_checklist') or [])
    payload = {'appeal_id': appeal.id, 'adventurer_id': appeal.adventurer_id,
        'snapshot_digest': digest(appeal.snapshot_before), 'patch': copy.deepcopy(patch or {}),
        'checklist': checklist, 'normalization_error': error, 'contract_valid': contract_valid is True,
        'operations': build_data_appeal_operation_manifest(patch, source='primary', snapshot_before=appeal.snapshot_before)}
    previous = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
    counters = {key: copy.deepcopy(previous[key]) for key in (
        '_backend_auto_retry_attempts', '_backend_zero_token_retry_attempts') if isinstance(previous.get(key), dict)}
    return {**counters, 'skipped_review': True, 'skipped_by_admin': True, 'decision': 'allow',
        'summary': '管理员已关闭二审，按初审原稿和后端校验处理。',
        'admin_policy': issue('appeal_second_review', 'appeal', payload)}


def payload(review, appeal=None):
    record = review.get('admin_policy') if isinstance(review, dict) else None
    data = record.get('payload') if isinstance(record, dict) else None
    if not isinstance(data, dict) or not verify(record, 'appeal', data):
        return None
    if appeal is not None and (data['appeal_id'] != appeal.id
            or data['adventurer_id'] != appeal.adventurer_id
            or data['snapshot_digest'] != digest(appeal.snapshot_before)
            or data['patch'] != (appeal.proposed_patch or {})):
        return None
    return data


def parse_policy(appeal):
    if not appeal:
        return None
    return payload(appeal._parse_json_text(appeal.review_ai_raw_response) or {}, appeal)


def finalize(appeal):
    from db_repository import db_manager
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService as S
    data = parse_policy(appeal)
    if data is None:
        S._mark_appeal_resolution_pending(appeal, '初审原稿或后台执行凭据未通过校验，等待初审重新处理。', stage='primary_ai')
        return
    review = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
    if not data.get('contract_valid'):
        S._mark_appeal_resolution_pending(appeal, '初审原稿未完整通过数据校验，等待初审修正；本次未写入。', stage='primary_ai')
        return
    checklist = data['checklist']
    states = [row.get('status') for row in checklist if isinstance(row, dict)]
    # A partial proposal has no machine-readable mapping from operations to
    # pending requests. Keep the complete proposal until the first adjudicator
    # resolves those requests; never manufacture a per-request success receipt.
    if not states or len(states) != len(checklist) or any(s not in ('done', 'rejected', 'partial') for s in states):
        S._mark_appeal_resolution_pending(appeal, '初审仍有待查证或未完成项目，等待初审补齐；二审已关闭。', stage='primary_ai')
        return
    patch = data['patch']
    has_ops = S._patch_has_operations(patch)
    rejected = all(state == 'rejected' for state in states)
    already = all(row.get('status') == 'done' and row.get('resolution_basis') == 'already_satisfied_same_case' for row in checklist)
    if (rejected and has_ops) or (not has_ops and not rejected and not already):
        S._mark_appeal_resolution_pending(appeal, '初审结论与可执行操作不一致，等待初审修正；二审已关闭。', stage='primary_ai')
        return
    if has_ops and review.get('policy_execution', {}).get('status') != 'executed':
        # A prior reviewed attempt must not supply extra operations to this
        # independently authorized primary proposal during recovery.
        appeal.review_supplement_patch = None
        records = S._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
        record_patch, unlandable = S._strip_unlandable_create_items(S._record_patch_only(patch), appeal=appeal)
        if unlandable:
            S._mark_appeal_resolution_pending(appeal, '初审补发缺少可展示说明，等待初审补齐；本次未写入。', stage='primary_ai')
            return
        savepoint = db_manager.begin_nested()
        try:
            start_seq = S._next_audit_seq(appeal.id)
            error = None
            if S._patch_has_operations(record_patch):
                _, error = S._apply_patch(appeal.adventurer_id, record_patch, appeal.snapshot_before,
                    appeal_id=appeal.id, all_player_records=records, allow_quantity_increase=True)
            if not error:
                _, error = S._apply_supplement_and_distributions(appeal, all_player_records=records,
                    proposed_team_pool=patch.get('team_pool'), proposed_tavern_subsidy=patch.get('tavern_subsidy'),
                    proposed_reclaim_items=patch.get('reclaim_items'), apply_supplement_patch=False)
            if not error:
                from plugins.adventurer_s1.models import AdventurerS1DataAppealAuditLog
                from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import iter_data_appeal_patch_operations
                audits = db_manager.query(AdventurerS1DataAppealAuditLog).filter(
                    AdventurerS1DataAppealAuditLog.appeal_id == appeal.id,
                    AdventurerS1DataAppealAuditLog.op_seq >= start_seq).all()
                current = S._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
                used = set()
                for op in iter_data_appeal_patch_operations(patch, source='primary', snapshot_before=appeal.snapshot_before):
                    expected = copy.deepcopy(op.get('_payload') or {})
                    # The item consumer stores an explicitly cleared override
                    # as NULL so the catalog description becomes effective.
                    if op.get('section') in ('equipment', 'warehouse') and expected.get('custom_desc') == '':
                        expected['custom_desc'] = None
                    matched = next((audit for audit in audits if audit.id not in used
                        and S._appeal_audit_matches_operation(op, audit, payload=expected, current_records=current)), None)
                    if matched:
                        used.add(matched.id)
                    elif not S._appeal_operation_already_satisfied(appeal, op, expected, current):
                        error = '初审操作未形成完整执行回执: ' + op['operation_id']
                        break
            if error:
                savepoint.rollback()
                from flask import current_app
                current_app.logger.warning('Primary appeal policy execution deferred appeal=%s error=%s', appeal.id, error)
                S._mark_appeal_resolution_pending(appeal, '初审修复未能落账，已保留待重试；不视为诉求被驳回。', stage='execution')
                return
            review['policy_execution'] = {'status': 'executed', 'proposal_digest': digest(patch),
                'audit_seq_start': start_seq, 'audit_seq_end': S._next_audit_seq(appeal.id),
                'operation_ids': [op['operation_id'] for op in data['operations']]}
            appeal.review_ai_raw_response = json.dumps(review, ensure_ascii=False)
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise
    review['request_results'] = [{
        'request_id': row.get('request_id', f'request_{index + 1}'),
        'label': row.get('label', ''), 'status': row['status'],
        'resolution_basis': row.get('resolution_basis'), 'detail': row.get('detail', ''),
        'is_core': row.get('is_core') is True, 'category': row.get('category', 'other'),
        'outcome': 'rejected' if row['status'] == 'rejected' else 'already_satisfied' if already else 'primary_proposal_executed',
        'source': 'primary_adjudicator',
    } for index, row in enumerate(checklist)]
    review['resolution_state'] = 'completed'
    for key in ('pending_stage', '_backend_resume_stage', '_backend_auto_retry_after', '_backend_auto_retry_exhausted', 'execution_note'):
        review.pop(key, None)
    appeal.review_ai_raw_response = json.dumps(review, ensure_ascii=False)
    appeal.status = 'rejected' if rejected else 'approved'
    appeal.ai_status = 'done'
    appeal.reviewed_by = None
    appeal.reviewed_at = utc_now()
    appeal.applied_at = utc_now() if has_ops else None
    appeal.core_request_satisfied = all(row['status'] == 'done' for row in checklist if row.get('is_core') is True)
    if rejected:
        appeal.reject_reason = S.get_suggested_reject_reason(appeal)
    S._notify_player_result(appeal, approved=not rejected, auto=True)
