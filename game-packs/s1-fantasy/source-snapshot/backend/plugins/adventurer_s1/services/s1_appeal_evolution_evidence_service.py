"""Freeze owner-scoped altar records as read-only appeal evidence.

Exact names select relevant records; they never establish an instance binding
or grant permission to migrate a trait's dependencies.
"""
from copy import deepcopy

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1BuildingUsageLog
from utils.time_utils import to_utc_iso


HISTORY_KEY = 'equipment_evolution_history'
_STATE_FIELDS = (
    'name', 'type', 'rarity', 'custom_desc', 'uses_left', 'evolution_stage',
    'quantity', 'damage_stage', 'damage_note',
)
_RECORD_FIELDS = (
    'usage_log_id', 'adventurer_id', 'companion_id', 'used_at', 'success',
    'old_name', 'new_name', 'evolution_stage', 'auxiliary_material_name',
    'auxiliary_material_consumed', 'reverted_at',
)


def project_history_for_ai(history: dict) -> dict:
    """Drop internal undo/request fields while retaining explicit negative facts."""
    out = {key: deepcopy(history[key]) for key in (
        'schema_version', 'history_truncated',
    ) if key in history}
    out['records'] = []
    for record in history.get('records') or []:
        if not isinstance(record, dict):
            continue
        projected = {key: deepcopy(record[key]) for key in _RECORD_FIELDS if key in record}
        for state in ('before', 'after'):
            if isinstance(record.get(state), dict):
                projected[state] = {
                    key: deepcopy(record[state][key])
                    for key in _STATE_FIELDS if key in record[state]
                }
        out['records'].append(projected)
    return out


def preserve_histories_after_compaction(
    compacted: dict, original: dict, *, record_blocks: tuple[str, ...],
) -> dict:
    """Restore allowlisted history at each role's declared snapshot path."""
    for key in record_blocks:
        block = original.get(key)
        if isinstance(block, dict) and isinstance(block.get(HISTORY_KEY), dict):
            compacted.setdefault(key, {})[HISTORY_KEY] = project_history_for_ai(block[HISTORY_KEY])
    return compacted


def select_relevant_records(rows, adventurer_id: int, names: set[str]) -> list[dict]:
    """Select a name-connected evidence subset without merging source identities."""
    records = []
    for row in rows:
        result = row.result
        if row.adventurer_id != adventurer_id or not isinstance(result, dict):
            continue
        if result.get('type') != 'pet_evolution':
            continue
        undo = result.get('_undo_snapshot') or {}
        reversal = result.get('undo') or {}
        records.append({
            **{key: result[key] for key in _RECORD_FIELDS if key in result},
            'usage_log_id': row.id,
            'adventurer_id': row.adventurer_id,
            'used_at': to_utc_iso(row.used_at),
            'before': result.get('pre_evolution_snapshot') or {},
            'after': (
                undo.get('after', {})
                if (
                    isinstance(undo, dict)
                    and undo.get('kind') == 'pet_evolution'
                    and undo.get('companion_id') == result.get('companion_id')
                )
                else {}
            ),
            'reverted_at': reversal.get('reverted_at') if isinstance(reversal, dict) else None,
        })
    relevant_names = set(names)
    selected = set()
    while True:
        added = False
        for index, record in enumerate(records):
            if index in selected:
                continue
            record_names = {
                name for name in (record.get('old_name'), record.get('new_name'))
                if isinstance(name, str) and name
            }
            if not relevant_names.intersection(record_names):
                continue
            selected.add(index)
            if record.get('success') is True:
                relevant_names.update(record_names)
            added = True
        if not added:
            break
    return project_history_for_ai({
        'records': [record for index, record in enumerate(records) if index in selected],
    })['records']


def capture_evolution_history(adventurer_id: int, snapshot: dict) -> dict | None:
    names = {
        row['name'] for section in ('equipment', 'warehouse')
        for row in snapshot.get(section) or []
        if isinstance(row, dict) and isinstance(row.get('name'), str) and row['name']
    }
    for trait in snapshot.get('traits') or []:
        if not isinstance(trait, dict):
            continue
        for dependency in trait.get('asset_dependencies') or []:
            if (
                isinstance(dependency, dict)
                and dependency.get('asset_kind') == 'equipment'
                and isinstance(dependency.get('asset_name'), str)
                and dependency['asset_name']
            ):
                names.add(dependency['asset_name'])
    if not names:
        return None
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin

    key = 'ADVENTURER_S1_DATA_APPEAL_EVOLUTION_HISTORY_LIMIT'
    limit = max(1, int(current_app.config.get(key, AdventurerS1Plugin.default_config[key])))
    rows = db_manager.query(AdventurerS1BuildingUsageLog).filter(
        AdventurerS1BuildingUsageLog.adventurer_id == int(adventurer_id),
        AdventurerS1BuildingUsageLog.result['type'].as_string() == 'pet_evolution',
    ).order_by(
        AdventurerS1BuildingUsageLog.used_at.desc(), AdventurerS1BuildingUsageLog.id.desc(),
    ).limit(limit + 1).all()
    return {
        'schema_version': 1,
        'history_truncated': len(rows) > limit,
        'records': select_relevant_records(rows[:limit], int(adventurer_id), names),
    }
