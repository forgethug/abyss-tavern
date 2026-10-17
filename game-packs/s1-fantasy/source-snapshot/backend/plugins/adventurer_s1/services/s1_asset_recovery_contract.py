"""Strict structured-output contract for expedition asset recovery triggers."""

from __future__ import annotations

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


S1_RECOVERY_TRIGGERS_FIELD = 'recovery_triggers'
S1_RECOVERY_TRIGGER_ERROR_MARKER = '技能/装备恢复触发协议错误'

_KINDS = frozenset({'skill', 'equipment'})
_KEYS = frozenset({'asset_kind', 'asset_id', 'evidence_event_indexes'})


def _assets_for_kind(member_asset_context: dict, asset_kind: str) -> dict:
    field = (
        'skill_assets_by_id'
        if asset_kind == 'skill'
        else 'equipment_assets_by_id'
    )
    raw = member_asset_context.get(field) if isinstance(member_asset_context, dict) else None
    if not isinstance(raw, dict):
        return {}
    assets: dict[int, dict] = {}
    for raw_id, row in raw.items():
        asset_id, _ = normalize_ascii_integer(raw_id, minimum=1)
        if type(asset_id) is int and isinstance(row, dict):
            assets[asset_id] = row
    return assets


def normalize_s1_recovery_triggers(
    member_update: dict,
    *,
    member_asset_context: dict,
    event_count: int | None,
    path: str,
    require_asset_context: bool = True,
) -> list[str]:
    """Project sparse triggers against frozen identity and effective minutes."""
    if S1_RECOVERY_TRIGGERS_FIELD not in member_update:
        member_update[S1_RECOVERY_TRIGGERS_FIELD] = []
        return []
    raw_value = member_update.get(S1_RECOVERY_TRIGGERS_FIELD)
    if not isinstance(raw_value, list):
        member_update[S1_RECOVERY_TRIGGERS_FIELD] = []
        return [f'{path}.{S1_RECOVERY_TRIGGERS_FIELD}: 必须是数组']

    normalized: list[dict] = []
    errors: list[str] = []
    seen: set[tuple[str, int]] = set()
    for index, raw_entry in enumerate(raw_value):
        entry_path = f'{path}.{S1_RECOVERY_TRIGGERS_FIELD}.{index}'
        if not isinstance(raw_entry, dict):
            errors.append(f'{entry_path}: 必须是对象')
            continue
        entry = {key: raw_entry[key] for key in _KEYS if key in raw_entry}
        missing = _KEYS - frozenset(entry)
        if missing:
            errors.append(f'{entry_path}: 缺少必填字段 {sorted(missing)}')
            continue
        asset_kind = entry.get('asset_kind')
        if asset_kind not in _KINDS:
            errors.append(f'{entry_path}.asset_kind: 必须是 skill 或 equipment')
            continue
        asset_id, _ = normalize_ascii_integer(entry.get('asset_id'), minimum=1)
        if type(asset_id) is not int or asset_id <= 0:
            errors.append(f'{entry_path}.asset_id: 必须是正整数')
            continue
        identity = (asset_kind, asset_id)
        if identity in seen:
            errors.append(f'{entry_path}: 同一资产本次最多触发一次')
            continue
        seen.add(identity)

        evidence_raw = entry.get('evidence_event_indexes')
        if not isinstance(evidence_raw, list) or not evidence_raw:
            errors.append(f'{entry_path}.evidence_event_indexes: 必须是非空数组')
            continue
        evidence: list[int] = []
        evidence_seen: set[int] = set()
        evidence_invalid = False
        for raw_event_index in evidence_raw:
            event_index, _ = normalize_ascii_integer(raw_event_index, minimum=0)
            if type(event_index) is not int or event_index < 0:
                evidence_invalid = True
                break
            if event_count is not None and event_index >= event_count:
                evidence_invalid = True
                break
            if event_index not in evidence_seen:
                evidence_seen.add(event_index)
                evidence.append(event_index)
        if evidence_invalid or not evidence:
            errors.append(
                f'{entry_path}.evidence_event_indexes: '
                '只能引用本次 events 中存在的非负整数下标'
            )
            continue

        assets = _assets_for_kind(member_asset_context, asset_kind)
        source = assets.get(asset_id)
        if require_asset_context and source is None:
            errors.append(f'{entry_path}: 资产 ID 不属于该成员的出发快照')
            continue
        if source is not None:
            update_field = (
                'skill_updates' if asset_kind == 'skill'
                else 'equipment_updates'
            )
            id_field = 'skill_id' if asset_kind == 'skill' else 'equipment_id'
            proposed_minutes = None
            for asset_update in member_update.get(update_field) or []:
                if not isinstance(asset_update, dict):
                    continue
                proposed_asset_id, _ = normalize_ascii_integer(
                    asset_update.get(id_field), minimum=1,
                )
                if proposed_asset_id != asset_id:
                    continue
                proposed_minutes, _ = normalize_ascii_integer(
                    asset_update.get('recovery_minutes'), minimum=0,
                )
                break
            recovery_minutes, _ = normalize_ascii_integer(
                (
                    proposed_minutes
                    if type(proposed_minutes) is int
                    else source.get('recovery_minutes')
                ),
                minimum=0,
            )
            if type(recovery_minutes) is not int or recovery_minutes <= 0:
                errors.append(f'{entry_path}: 该资产没有可触发的 recovery_minutes')
                continue
            if source.get('recovering') is True:
                errors.append(f'{entry_path}: 该资产出发时仍在恢复中，不能再次触发')
                continue

        normalized.append({
            'asset_kind': asset_kind,
            'asset_id': asset_id,
            'evidence_event_indexes': evidence,
        })

    member_update[S1_RECOVERY_TRIGGERS_FIELD] = normalized
    return errors
