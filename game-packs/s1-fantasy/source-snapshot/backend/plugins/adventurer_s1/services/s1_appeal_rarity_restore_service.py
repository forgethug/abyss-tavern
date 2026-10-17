"""Bound ordinary appeals to an audited equipment rarity before-image.

These candidates establish identity and an upper bound, not whether the story
justified the downgrade. Both appeal jurors must decide that separately.
"""
from copy import deepcopy
import json

from flask import current_app
from sqlalchemy import or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1DataAppealAuditLog,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1ItemLog,
    AdventurerS1WarehouseItem,
)


EVIDENCE_KEY = 'equipment_rarity_restore_candidates'
_RARITIES = ('common', 'rare', 'epic', 'legendary')
_STATE_FIELDS = (
    'name', 'type', 'rarity', 'custom_desc', 'quantity', 'uses_left',
    'evolution_stage', 'recovery_minutes',
)
_AI_FIELDS = (
    'adventurer_id', 'expedition_log_id', 'source_equipment_id',
    'target_type', 'target_id', 'name', 'before_rarity', 'settled_rarity',
)


def item_state(record: dict) -> dict:
    """Canonical persisted state; zero/default omissions carry no new authority."""
    return {
        'name': record.get('name'),
        'type': record.get('type'),
        'rarity': record.get('rarity'),
        'custom_desc': record.get('custom_desc') or record.get('desc') or '',
        'quantity': record.get('quantity', 1),
        'uses_left': record.get('uses_left'),
        'evolution_stage': record.get('evolution_stage') or 0,
        'recovery_minutes': record.get('recovery_minutes') or 0,
    }


def _member_mutations(full_log: dict, adventurer_id: int) -> list[dict] | None:
    receipt = full_log.get('settlement_receipt') or {}
    if not isinstance(receipt, dict) or receipt.get('status') != 'passed':
        return None
    members = [
        member for member in receipt.get('members') or []
        if isinstance(member, dict)
        and member.get('adventurer_id') == adventurer_id
        and member.get('status') == 'passed'
    ]
    if len(members) != 1 or not isinstance(members[0].get('asset_mutations'), list):
        return None
    return members[0]['asset_mutations']


def build_candidates(
    full_log: dict, adventurer_id: int, expedition_log_id: int,
    snapshot_before: dict, current_records: dict,
) -> list[dict]:
    """Map complete passed before/after images to selected, still-owned rows."""
    mutations = _member_mutations(full_log, adventurer_id)
    if mutations is None:
        return []
    selected = {
        (section, row['id']): row
        for section in ('equipment', 'warehouse')
        for row in snapshot_before.get(section) or []
        if isinstance(row, dict) and type(row.get('id')) is int
    }
    current = [
        (section, row)
        for section in ('equipment', 'warehouse')
        for row in current_records.get(section) or []
        if isinstance(row, dict) and type(row.get('id')) is int
    ]
    candidates = []
    for mutation in mutations:
        if not isinstance(mutation, dict) or (
            mutation.get('asset_type') != 'equipment'
            or mutation.get('operation') != 'updated'
            or type(mutation.get('asset_id')) is not int
        ):
            continue
        before, after = mutation.get('before'), mutation.get('after')
        # Sparse AI projections are not complete identity evidence.
        if not all(
            isinstance(state, dict)
            and {'name', 'type', 'rarity', 'desc', 'quantity'} <= state.keys()
            for state in (before, after)
        ):
            continue
        old_rarity, settled_rarity = before['rarity'], after['rarity']
        if (
            old_rarity not in _RARITIES or settled_rarity not in _RARITIES
            or _RARITIES.index(old_rarity) <= _RARITIES.index(settled_rarity)
        ):
            continue
        expected = item_state(after)
        # A concurrent rename, transformation, or other settlement change needs
        # its own repair contract, rather than inferring the former identity.
        if any(item_state(before)[key] != expected[key] for key in _STATE_FIELDS if key != 'rarity'):
            continue
        source_id = mutation['asset_id']
        stable = [entry for entry in current if entry[0] == 'equipment' and entry[1]['id'] == source_id]
        matches = stable or [entry for entry in current if item_state(entry[1]) == expected]
        if len(matches) != 1:
            continue
        section, row = matches[0]
        frozen = selected.get((section, row['id']))
        if (
            frozen is None or frozen.get('text_locked') is True
            or item_state(row) != expected or item_state(frozen) != expected
            or any(row.get(key) or frozen.get(key) for key in (
                'damage_note', 'recovering', 'recovery_started_at', 'recovery_ends_at',
            ))
        ):
            continue
        candidates.append({
            'adventurer_id': adventurer_id,
            'expedition_log_id': expedition_log_id,
            'source_equipment_id': source_id,
            'target_type': section,
            'target_id': row['id'],
            'name': expected['name'],
            'before_rarity': old_rarity,
            'settled_rarity': settled_rarity,
            'expected_state': expected,
        })
    # Multiple source rows cannot give one current row several repair baselines.
    return [candidate for candidate in candidates if sum(
        other['target_type'] == candidate['target_type']
        and other['target_id'] == candidate['target_id']
        for other in candidates
    ) == 1]


def project_candidates_for_ai(candidates: list) -> list[dict]:
    return [
        {key: deepcopy(candidate[key]) for key in _AI_FIELDS if key in candidate}
        for candidate in candidates if isinstance(candidate, dict)
    ]


def permits_restore(evidence: dict | None, section: str, record: dict, rarity: str, adventurer_id: int | None) -> bool:
    """Only the exact frozen row and its exact historical rarity gain permission."""
    if not isinstance(evidence, dict) or type(adventurer_id) is not int:
        return False
    matches = [candidate for candidate in evidence.get(EVIDENCE_KEY) or []
        if isinstance(candidate, dict)
        and candidate.get('adventurer_id') == adventurer_id
        and candidate.get('expedition_log_id') == evidence.get('expedition_log_id')
        and candidate.get('target_type') == section
        and candidate.get('target_id') == record.get('id')
        and candidate.get('before_rarity') == rarity
        and candidate.get('settled_rarity') == record.get('rarity')
        and candidate.get('expected_state') == item_state(record)
    ]
    return len(matches) == 1


def has_restore_verdict(appeal: object, section: str, record_id: int, rarity: str) -> bool:
    """Require an effective v2 allow for the actual frozen rarity payload."""
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import (
        effective_data_appeal_operation_decisions, iter_data_appeal_patch_operations,
    )
    try:
        review = json.loads(getattr(appeal, 'review_ai_raw_response', None) or '{}')
    except (TypeError, ValueError):
        return False
    if not isinstance(review, dict) or review.get('verdict_contract_version') != 2:
        return False
    decisions = effective_data_appeal_operation_decisions(review.get('verdicts'))
    manifest = {
        operation.get('operation_id'): operation
        for operation in review.get('_backend_operation_manifest') or []
        if isinstance(operation, dict)
    }
    operations = list(iter_data_appeal_patch_operations(getattr(appeal, 'proposed_patch', None), source='primary'))
    authored = review.get('_backend_authorized_review_operations')
    if isinstance(authored, list):
        operations.extend({**operation, '_payload': operation.get('payload')}
            for operation in authored if isinstance(operation, dict))
    else:
        patches = review.get('_backend_authorized_review_operation_patches') or {}
        for source in ('supplement', 'override'):
            operations.extend(iter_data_appeal_patch_operations(patches.get(source), source=source))
    return any(
        decisions.get(operation.get('operation_id')) == 'allow'
        and 'rarity' in (manifest.get(operation.get('operation_id'), {}).get('fields') or [])
        and operation.get('section') == section
        and operation.get('action') == 'update'
        and operation.get('target_id') == record_id
        and (operation.get('_payload') or {}).get('rarity') == rarity
        for operation in operations
    )


def refresh_candidates(
    evidence: dict | None, adventurer_id: int, snapshot_before: dict, *,
    lock: bool = False, current_record_ids_out: dict | None = None,
) -> dict | None:
    """Refresh from persisted source and current ownership, in a short DB phase.

    The caller holds the adventurer/source locks at final execution. Inventory
    rows are then locked too; no AI or other network work runs in this function.
    Later asset writes and external flows invalidate the source baseline. Storage
    moves can still match by their complete, unique persisted state.
    """
    if not isinstance(evidence, dict):
        return evidence
    out = deepcopy(evidence)
    out.pop(EVIDENCE_KEY, None)
    log_id = evidence.get('expedition_log_id')
    if type(log_id) is not int:
        return out
    source = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=log_id).populate_existing().first()
    if not source or source.status != 'completed' or not isinstance(source.full_log, dict):
        return out
    if _member_mutations(source.full_log, adventurer_id) is None:
        return out
    records = {}
    for section, model in (('equipment', AdventurerS1Equipment), ('warehouse', AdventurerS1WarehouseItem)):
        query = db_manager.query(model).filter_by(adventurer_id=adventurer_id).populate_existing()
        if lock:
            query = query.with_for_update()
        records[section] = [
            {'id': row.id, **{key: getattr(row, key, None) for key in (
                *_STATE_FIELDS, 'damage_note', 'recovery_started_at', 'recovery_ends_at',
            )}}
            for row in query.order_by(model.id).all()
        ]
        if isinstance(current_record_ids_out, dict):
            current_record_ids_out[section] = {row['id'] for row in records[section]}
    candidates = build_candidates(source.full_log, adventurer_id, log_id, snapshot_before, records)
    if not candidates:
        return out
    # updated_at can move forward during later cold compression or maintenance.
    source_time = source.created_at
    if source_time is None:
        return out
    names = {candidate['name'] for candidate in candidates}
    # Name matching can invalidate a candidate, never authorize a replacement.
    blocked_names = {row.item_name for row in db_manager.query(AdventurerS1ItemLog.item_name).filter(
        AdventurerS1ItemLog.adventurer_id == adventurer_id,
        AdventurerS1ItemLog.item_name.in_(names),
        AdventurerS1ItemLog.created_at >= source_time,
        AdventurerS1ItemLog.change_type.notin_(('equip', 'unequip')),
        or_(
            AdventurerS1ItemLog.source_type != 'expedition',
            AdventurerS1ItemLog.related_type.is_(None),
            AdventurerS1ItemLog.related_type != 'expedition_log',
            AdventurerS1ItemLog.related_id.is_(None),
            AdventurerS1ItemLog.related_id != str(log_id),
        ),
    ).distinct().all()}
    refs = {(candidate['target_type'], candidate['target_id']) for candidate in candidates}
    refs.update(('equipment', candidate['source_equipment_id']) for candidate in candidates)
    audits = db_manager.query(
        AdventurerS1DataAppealAuditLog.target_type, AdventurerS1DataAppealAuditLog.target_id,
    ).filter(
        AdventurerS1DataAppealAuditLog.adventurer_id == adventurer_id,
        AdventurerS1DataAppealAuditLog.target_id.in_({rid for _, rid in refs}),
        AdventurerS1DataAppealAuditLog.created_at >= source_time,
        AdventurerS1DataAppealAuditLog.reverted.is_(False),
    ).all()
    blocked_refs = {(row.target_type, row.target_id) for row in audits}
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    history_key = 'ADVENTURER_S1_DATA_APPEAL_RARITY_RESTORE_HISTORY_LIMIT'
    history_limit = max(1, int(current_app.config.get(
        history_key, AdventurerS1Plugin.default_config[history_key],
    )))
    later_logs = db_manager.query(AdventurerS1ExpeditionLog.full_log).join(
        AdventurerS1ExpeditionLogMember,
        AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id,
    ).filter(
        AdventurerS1ExpeditionLogMember.adventurer_id == adventurer_id,
        AdventurerS1ExpeditionLog.id != log_id,
        AdventurerS1ExpeditionLog.created_at >= source.created_at,
        AdventurerS1ExpeditionLog.status == 'completed',
    ).order_by(AdventurerS1ExpeditionLog.id).limit(history_limit + 1).all()
    if len(later_logs) > history_limit:
        return out  # Never treat a truncated history as complete proof.
    for later in later_logs:
        later_full = later.full_log if isinstance(later.full_log, dict) else {}
        mutations = _member_mutations(later_full, adventurer_id)
        if mutations is None:
            return out  # Incomplete subsequent history cannot prove continuity.
        for mutation in mutations:
            if isinstance(mutation, dict) and mutation.get('asset_type') == 'equipment':
                blocked_refs.add(('equipment', mutation.get('asset_id')))
                for state in (mutation.get('before'), mutation.get('after')):
                    if isinstance(state, dict) and state.get('name') in names:
                        blocked_names.add(state['name'])
        for member in later_full.get('member_updates') or []:
            if isinstance(member, dict) and member.get('id') == adventurer_id:
                for consumed in member.get('consumed_equipments') or []:
                    name = consumed.get('name') if isinstance(consumed, dict) else consumed
                    if isinstance(name, str) and name in names:
                        blocked_names.add(name)
    candidates = [candidate for candidate in candidates if (
        candidate['name'] not in blocked_names
        and (candidate['target_type'], candidate['target_id']) not in blocked_refs
        and ('equipment', candidate['source_equipment_id']) not in blocked_refs
    )]
    if candidates:
        out[EVIDENCE_KEY] = candidates
    return out
