"""Stable per-operation authorization contract for S1 data appeals.

The existing appeal patch remains the only write payload.  This module gives
each concrete row in that payload a deterministic identifier so the second
reviewer can authorize, reject, group, and later reconcile it without relying
on section-wide or natural-language matching.
"""
from __future__ import annotations

import copy
import math

from plugins.adventurer_s1.services.adventurer_s1_data_appeal_destructive_contract import (
    DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_finite_number,
    normalize_ascii_integer,
)


DATA_APPEAL_OPERATION_CONTRACT_VERSION = 2
DATA_APPEAL_OPERATION_SOURCES = frozenset({
    'primary', 'supplement', 'override', 'clear',
})
DATA_APPEAL_OPERATION_RECORD_SECTIONS = (
    'traits', 'equipment', 'warehouse', 'skills',
)
DATA_APPEAL_OPERATION_ARRAY_SECTIONS = (
    'create_items', 'create_traits', 'create_skills',
    'currency_adjustments', 'reclaim_items',
    'unassigned_items',
)


def normalize_data_appeal_number(value: object) -> int | float | None:
    """Keep exact numeric representations; reject booleans and nonfinite values."""
    normalized, _ = normalize_ascii_finite_number(value)
    if type(normalized) not in (int, float):
        return None
    try:
        return normalized if math.isfinite(normalized) else None
    except OverflowError:
        return None


def normalize_data_appeal_currency_adjustment(row: dict) -> tuple[dict, str | None]:
    """Project one grant and losslessly resolve the observed ``gold`` shorthand.

    Keep conflicting/malformed consumed fields for the caller's normal failure
    path. This never supplies an amount from review evidence or chooses between
    disagreeing aliases. Ownership assertions remain available to the guard.
    """
    projected = {
        key: copy.deepcopy(row[key])
        for key in (
            'currency_code', 'amount', 'gold', 'reason',
            'owner_adventurer_id', 'target_adventurer_id',
        )
        if key in row
    }
    has_gold = 'gold' in projected
    currency_code = projected.get('currency_code', 'gold' if has_gold else None)
    if not isinstance(currency_code, str) or currency_code.strip().lower() != 'gold':
        return projected, 'currency_code_invalid'

    amounts = []
    for field in ('amount', 'gold'):
        if field not in projected:
            continue
        amount = normalize_data_appeal_number(projected[field])
        if amount is None:
            return projected, 'currency_amount_invalid'
        amounts.append(amount)
    if not amounts:
        return projected, 'currency_amount_missing'
    if any(amount != amounts[0] for amount in amounts[1:]):
        return projected, 'currency_amount_alias_conflict'

    projected.pop('gold', None)
    projected['currency_code'] = 'gold'
    projected['amount'] = amounts[0]
    return projected, None


def _normalized_positive_id(value: object) -> int | None:
    normalized, _ = normalize_ascii_integer(value, minimum=1)
    return normalized if type(normalized) is int and normalized > 0 else None


def _operation_id(source: str, section: str, action: str, selector: int) -> str:
    return f'{source}:{section}:{action}:{selector}'


def _snapshot_record_map(snapshot_before: dict | None, section: str) -> dict[int, dict]:
    if not isinstance(snapshot_before, dict):
        return {}
    rows = snapshot_before.get(section)
    if not isinstance(rows, list):
        return {}
    result: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        record_id = _normalized_positive_id(row.get('id'))
        if record_id is not None:
            result[record_id] = row
    return result


def iter_data_appeal_patch_operations(
    patch: dict | None,
    *,
    source: str = 'primary',
    snapshot_before: dict | None = None,
    consumption_restore_candidate_names: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Return concrete patch rows with deterministic operation identifiers."""
    if source not in DATA_APPEAL_OPERATION_SOURCES or not isinstance(patch, dict):
        return []

    candidate_names = {
        str(name or '').strip()
        for name in (consumption_restore_candidate_names or set())
        if str(name or '').strip()
    }
    operations: list[dict] = []
    for section in DATA_APPEAL_OPERATION_RECORD_SECTIONS:
        section_value = patch.get(section)
        if not isinstance(section_value, dict):
            continue
        snapshot_map = _snapshot_record_map(snapshot_before, section)
        for action in ('update', 'delete'):
            rows = section_value.get(action)
            if not isinstance(rows, list):
                continue
            for row in rows:
                raw_id = row.get('id') if isinstance(row, dict) else row
                record_id = _normalized_positive_id(raw_id)
                if record_id is None:
                    continue
                payload = copy.deepcopy(row) if isinstance(row, dict) else {'id': record_id}
                payload['id'] = record_id
                risk_flags: list[str] = []
                if action == 'delete':
                    risk_flags.append('destructive')
                if section in ('equipment', 'warehouse') and action == 'update':
                    frozen = snapshot_map.get(record_id, {})
                    changes_name = (
                        'name' in payload
                        and str(payload.get('name') or '').strip()
                        != str(frozen.get('name') or '').strip()
                    )
                    changes_type = (
                        'type' in payload
                        and str(payload.get('type') or '').strip().lower()
                        != str(frozen.get('type') or '').strip().lower()
                    )
                    if changes_name:
                        risk_flags.append('item_name_change')
                    if changes_type:
                        risk_flags.append('item_type_change')
                    if changes_name or changes_type:
                        risk_flags.append('item_identity_change')
                    if 'quantity' in payload:
                        previous = snapshot_map.get(record_id, {}).get('quantity')
                        proposed, _ = normalize_ascii_integer(payload.get('quantity'), minimum=0)
                        previous_int, _ = normalize_ascii_integer(previous, minimum=0)
                        if (
                            type(proposed) is int
                            and type(previous_int) is int
                            and proposed > previous_int
                        ):
                            risk_flags.append('quantity_increase')
                operation = {
                    'operation_id': _operation_id(source, section, action, record_id),
                    'source': source,
                    'section': section,
                    'action': action,
                    'target_id': record_id,
                    'target_name': str(
                        payload.get('name')
                        or snapshot_map.get(record_id, {}).get('name')
                        or ''
                    ).strip(),
                    'fields': sorted(key for key in payload if key != 'id'),
                    'risk_flags': risk_flags,
                    '_payload': payload,
                }
                if 'quantity' in payload:
                    operation['numeric_target'] = copy.deepcopy(payload.get('quantity'))
                operations.append(operation)

    for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
        rows = patch.get(section)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_id = _normalized_positive_id(row.get('source_id'))
            if source_id is None:
                continue
            operations.append({
                'operation_id': _operation_id(source, section, 'action', source_id),
                'source': source,
                'section': section,
                'action': 'action',
                'target_id': source_id,
                'target_name': str(row.get('source_name') or row.get('name') or '').strip(),
                'fields': sorted(str(key) for key in row if key != 'source_id'),
                'risk_flags': ['destructive'],
                '_payload': copy.deepcopy(row),
            })

    for section in DATA_APPEAL_OPERATION_ARRAY_SECTIONS:
        rows = patch.get(section)
        if not isinstance(rows, list):
            continue
        action = {
            'currency_adjustments': 'adjust',
            'reclaim_items': 'reclaim',
            'unassigned_items': 'register',
        }.get(section, 'create')
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            currency_error = None
            if section == 'currency_adjustments':
                row, currency_error = normalize_data_appeal_currency_adjustment(row)
            risk_flags = ['asset_grant']
            if section == 'reclaim_items':
                risk_flags = [
                    'destructive', 'asset_transfer',
                    'exact_numeric_evidence_required',
                ]
            elif section == 'create_items':
                risk_flags.append('exact_numeric_evidence_required')
                if str(row.get('name') or '').strip() in candidate_names:
                    risk_flags.append('consumption_restore_candidate')
            elif section == 'currency_adjustments':
                risk_flags.append('exact_numeric_evidence_required')
            audit_match = {
                key: copy.deepcopy(row[key])
                for key in (
                    'name', 'destination', 'quantity',
                    'from_adventurer_id', 'currency_code', 'amount', 'asset_kind', 'asset_id',
                )
                if key in row
            }
            operation = {
                'operation_id': _operation_id(source, section, action, index),
                'source': source,
                'section': section,
                'action': action,
                'index': index,
                'target_name': str(row.get('name') or '').strip(),
                'fields': sorted(str(key) for key in row),
                'risk_flags': risk_flags,
                'audit_match': audit_match,
                '_payload': copy.deepcopy(row),
            }
            ownership_source = str(row.get('ownership_source') or '').strip()
            if section in ('create_items', 'create_traits', 'create_skills'):
                owner_id = _normalized_positive_id(row.get('owner_adventurer_id'))
                if owner_id is not None:
                    operation['owner_adventurer_id'] = owner_id
                if ownership_source:
                    operation['ownership_source'] = ownership_source
                if ownership_source == 'narrative_event':
                    risk_flags.append('narrative_evidence_required')
                    operation['narrative_claim_kind'] = str(
                        row.get('narrative_claim_kind') or ''
                    ).strip()
                    operation['evidence_event_indexes'] = copy.deepcopy(
                        row.get('evidence_event_indexes') or []
                    )
            if section in ('create_items', 'reclaim_items'):
                operation['numeric_target'] = copy.deepcopy(row.get('quantity', 1))
            elif section == 'currency_adjustments':
                operation['numeric_target'] = (
                    None if currency_error else copy.deepcopy(row.get('amount'))
                )
            elif section == 'unassigned_items':
                from plugins.adventurer_s1.services.s1_team_loot_contract import team_loot_registration_hash
                risk_flags.extend(['team_distribution', 'exact_numeric_evidence_required',
                                   'narrative_evidence_required'])
                operation['numeric_target'] = copy.deepcopy((row.get('item') or {}).get('quantity'))
                operation['evidence_event_indexes'] = copy.deepcopy(row.get('evidence_event_indexes'))
                operation['audit_match'] = {'registration_hash': team_loot_registration_hash(row)}
            operations.append(operation)

    team_pool = patch.get('team_pool')
    if isinstance(team_pool, dict) and team_pool:
        operations.append({
            'operation_id': _operation_id(source, 'team_pool', 'distribute', 0),
            'source': source,
            'section': 'team_pool',
            'action': 'distribute',
            'index': 0,
            'target_name': '',
            'fields': sorted(str(key) for key in team_pool),
            'risk_flags': [
                'asset_grant', 'team_distribution',
                'exact_numeric_evidence_required',
            ],
            'audit_match': {
                'total_amount': copy.deepcopy(team_pool.get('total_amount')),
            },
            'numeric_target': copy.deepcopy(team_pool.get('total_amount')),
            '_payload': copy.deepcopy(team_pool),
        })
    return operations


def iter_data_appeal_clear_operations(
    clear_fields: dict | None,
) -> list[dict]:
    """Return deterministic operations for the reviewer's explicit clear contract."""
    if not isinstance(clear_fields, dict):
        return []
    operations: list[dict] = []
    for section in DATA_APPEAL_OPERATION_RECORD_SECTIONS:
        rows = clear_fields.get(section)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            record_id = _normalized_positive_id(row.get('id'))
            if record_id is None:
                continue
            operations.append({
                'operation_id': _operation_id('clear', section, 'clear', record_id),
                'source': 'clear',
                'section': section,
                'action': 'clear',
                'target_id': record_id,
                'target_name': '',
                'fields': sorted(
                    str(field) for field in (row.get('fields') or [])
                    if isinstance(field, str) and field
                ),
                'risk_flags': ['field_clear'],
                '_payload': copy.deepcopy(row),
            })
    return operations


def build_data_appeal_operation_manifest(
    patch: dict | None,
    *,
    source: str = 'primary',
    snapshot_before: dict | None = None,
    clear_fields: dict | None = None,
    consumption_restore_candidate_names: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Build the compact reviewer-facing manifest without duplicating payload text."""
    operations = iter_data_appeal_patch_operations(
        patch,
        source=source,
        snapshot_before=snapshot_before,
        consumption_restore_candidate_names=consumption_restore_candidate_names,
    )
    if clear_fields:
        operations.extend(iter_data_appeal_clear_operations(clear_fields))
    return [
        {key: copy.deepcopy(value) for key, value in operation.items() if key != '_payload'}
        for operation in operations
    ]


def effective_data_appeal_operation_decisions(
    verdicts: list[dict] | None,
) -> dict[str, str]:
    """Derive fail-closed decisions, including all-or-nothing atomic groups."""
    normalized: dict[str, dict] = {}
    for verdict in verdicts or []:
        if not isinstance(verdict, dict):
            continue
        operation_id = str(verdict.get('operation_id') or '').strip()
        decision = str(verdict.get('decision') or '').strip().lower()
        if not operation_id or decision not in ('allow', 'reject'):
            continue
        normalized[operation_id] = verdict

    rejected_groups = {
        str(verdict.get('atomic_group_id') or '').strip()
        for verdict in normalized.values()
        if str(verdict.get('decision') or '').strip().lower() == 'reject'
        and str(verdict.get('atomic_group_id') or '').strip()
    }
    return {
        operation_id: (
            'reject'
            if str(verdict.get('atomic_group_id') or '').strip() in rejected_groups
            else str(verdict.get('decision') or '').strip().lower()
        )
        for operation_id, verdict in normalized.items()
    }


def filter_data_appeal_patch_by_operation_ids(
    patch: dict | None,
    *,
    source: str,
    allowed_operation_ids: set[str],
) -> dict:
    """Project a patch to exact allowed rows; an operation is never field-split."""
    if not isinstance(patch, dict):
        return {}
    selected: dict = {}
    for operation in iter_data_appeal_patch_operations(patch, source=source):
        if operation['operation_id'] not in allowed_operation_ids:
            continue
        section = operation['section']
        action = operation['action']
        payload = copy.deepcopy(operation['_payload'])
        if section in DATA_APPEAL_OPERATION_RECORD_SECTIONS:
            selected.setdefault(section, {'update': [], 'delete': []})[action].append(payload)
        elif section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            selected.setdefault(section, []).append(payload)
        elif section == 'team_pool':
            selected['team_pool'] = payload
        else:
            selected.setdefault(section, []).append(payload)
    return selected


def filter_data_appeal_clear_fields_by_operation_ids(
    clear_fields: dict | None,
    *,
    allowed_operation_ids: set[str],
) -> dict:
    """Project clear_fields to exact allowed rows."""
    selected: dict[str, list[dict]] = {}
    for operation in iter_data_appeal_clear_operations(clear_fields):
        if operation['operation_id'] not in allowed_operation_ids:
            continue
        selected.setdefault(operation['section'], []).append(
            copy.deepcopy(operation['_payload'])
        )
    return selected
