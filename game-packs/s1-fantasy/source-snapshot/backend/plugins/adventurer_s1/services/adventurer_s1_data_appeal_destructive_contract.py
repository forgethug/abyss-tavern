"""S1 数据申诉中，特质/技能破坏性动作的结构化契约。"""
from __future__ import annotations

from copy import deepcopy

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)


DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS = (
    'trait_destructive_actions',
    'skill_destructive_actions',
)
_DATA_APPEAL_DESTRUCTIVE_ACTION_MAX_DEFAULT = 16

_SOURCE_KIND_BY_SECTION = {
    'trait_destructive_actions': 'trait',
    'skill_destructive_actions': 'skill',
}
_TARGET_KIND_BY_MODE = {
    'delete_only': None,
    'replace_with_trait': 'trait',
    'replace_with_skill': 'skill',
}
_TRAIT_TARGET_FIELDS = frozenset({
    'id', 'name', 'description', 'is_archived',
    'recovery_policy', 'recovery_stage', 'recovery_progress',
    'recovery_required', 'recovery_hours', 'recovered_desc',
})
_SKILL_TARGET_FIELDS = frozenset({
    'id', 'name', 'description', 'equipped', 'recovery_minutes',
})


def _positive_int(value: object) -> int | None:
    result, _ = normalize_ascii_integer(value, minimum=1)
    return result if type(result) is int and result > 0 else None


def get_data_appeal_destructive_action_max() -> int:
    from flask import current_app, has_app_context

    if not has_app_context():
        return _DATA_APPEAL_DESTRUCTIVE_ACTION_MAX_DEFAULT
    return max(1, int(current_app.config.get(
        'ADVENTURER_S1_DATA_APPEAL_DESTRUCTIVE_ACTION_MAX',
        _DATA_APPEAL_DESTRUCTIVE_ACTION_MAX_DEFAULT,
    )))


def _project_target(kind: str, value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    fields = _TRAIT_TARGET_FIELDS if kind == 'trait' else _SKILL_TARGET_FIELDS
    projected = {key: deepcopy(value[key]) for key in fields if key in value}
    if 'id' in projected:
        target_id = _positive_int(projected['id'])
        if target_id is None:
            return None
        projected['id'] = target_id
    for field in ('name', 'description'):
        text = projected.get(field)
        if not isinstance(text, str) or not text.strip():
            return None
        projected[field] = text.strip()
    boolean_field = 'equipped' if kind == 'skill' else 'is_archived'
    if boolean_field in projected:
        boolean_value, _ = normalize_exact_boolean(projected[boolean_field])
        if type(boolean_value) is not bool:
            return None
        projected[boolean_field] = boolean_value
    if kind == 'skill' and 'recovery_minutes' in projected:
        recovery_minutes, _ = normalize_ascii_integer(
            projected['recovery_minutes'], minimum=0,
        )
        if type(recovery_minutes) is not int or recovery_minutes < 0:
            return None
        projected['recovery_minutes'] = recovery_minutes
    if kind == 'trait':
        for field in ('recovery_policy', 'recovery_stage', 'recovered_desc'):
            if field in projected and projected[field] is not None and not isinstance(projected[field], str):
                return None
        for field in ('recovery_progress', 'recovery_required', 'recovery_hours'):
            if field not in projected or projected[field] is None:
                continue
            normalized_integer, _ = normalize_ascii_integer(projected[field])
            if type(normalized_integer) is not int:
                return None
            projected[field] = normalized_integer
        recovery_fields = {
            'recovery_policy', 'recovery_stage', 'recovery_progress',
            'recovery_required', 'recovery_hours', 'recovered_desc',
        }
        if recovery_fields.intersection(projected):
            from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
                VALID_RECOVERY_POLICIES,
                VALID_RECOVERY_STAGES,
                sanitize_trait_recovery_metadata_for_appeal,
            )

            policy = projected.get('recovery_policy')
            stage = projected.get('recovery_stage')
            if type(policy) is not str or policy not in VALID_RECOVERY_POLICIES:
                return None
            if policy != 'none' and (
                type(stage) is not str or stage not in VALID_RECOVERY_STAGES
            ):
                return None
            if policy == 'none' and stage is not None:
                return None
            if stage == 'trace' and 'recovery_hours' not in projected:
                return None
            if stage == 'medium' and 'recovery_required' not in projected:
                return None

            canonical = sanitize_trait_recovery_metadata_for_appeal(projected)
            for field in recovery_fields.intersection(projected):
                if projected[field] != canonical.get(field):
                    return None
    return projected


def project_data_appeal_destructive_actions(section: str, value: object) -> list[dict] | None:
    """校验并白名单投影一个破坏性动作数组；非法或溢出时整体拒绝。"""
    if section not in _SOURCE_KIND_BY_SECTION or not isinstance(value, list):
        return None
    if len(value) > get_data_appeal_destructive_action_max():
        return None

    projected: list[dict] = []
    seen_source_ids: set[int] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return None
        source_id = _positive_int(raw.get('source_id'))
        if source_id is None or source_id in seen_source_ids:
            return None
        mode = raw.get('mode')
        if mode not in _TARGET_KIND_BY_MODE:
            return None
        reason = raw.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            return None

        target_kind = _TARGET_KIND_BY_MODE[mode]
        target = raw.get('target')
        if target_kind is None:
            if target is not None:
                return None
            normalized_target = None
        else:
            normalized_target = _project_target(target_kind, target)
            if normalized_target is None:
                return None

        seen_source_ids.add(source_id)
        row = {
            'source_id': source_id,
            'mode': mode,
            'reason': reason.strip()[:500],
        }
        if normalized_target is not None:
            row['target'] = normalized_target
        projected.append(row)
    return projected


def project_valid_data_appeal_destructive_actions(
    section: str, value: object,
) -> list[dict] | None:
    """逐项保留合法动作；非数组或数组溢出时拒绝整个 section，绝不截断。"""
    if section not in _SOURCE_KIND_BY_SECTION or not isinstance(value, list):
        return None
    if len(value) > get_data_appeal_destructive_action_max():
        return None
    projected: list[dict] = []
    seen_source_ids: set[int] = set()
    for raw in value:
        one = project_data_appeal_destructive_actions(section, [raw])
        if not one:
            continue
        source_id = one[0]['source_id']
        if source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        projected.append(one[0])
    return projected


def strip_uncontracted_trait_skill_deletes(patch: object) -> tuple[dict, list[dict]]:
    """移除普通 traits/skills.delete；它们在未来 AI 申诉链路中没有删除授权。"""
    cleaned = deepcopy(patch) if isinstance(patch, dict) else {}
    blocked: list[dict] = []
    for section in ('traits', 'skills'):
        section_value = cleaned.get(section)
        if not isinstance(section_value, dict):
            continue
        deletes = section_value.get('delete')
        if isinstance(deletes, list):
            for row in deletes:
                record_id = row.get('id') if isinstance(row, dict) else row
                blocked.append({'section': section, 'id': record_id})
        section_value.pop('delete', None)
        if not section_value.get('update'):
            cleaned.pop(section, None)
    return cleaned, blocked


def destructive_action_source_ids(patch: object, section: str) -> set[int]:
    if not isinstance(patch, dict):
        return set()
    rows = patch.get(section)
    if not isinstance(rows, list):
        return set()
    return {
        int(row['source_id'])
        for row in rows
        if isinstance(row, dict) and _positive_int(row.get('source_id')) is not None
    }


def destructive_action_source_kind(section: str) -> str | None:
    return _SOURCE_KIND_BY_SECTION.get(section)


def destructive_action_target_kind(mode: str) -> str | None:
    return _TARGET_KIND_BY_MODE.get(mode)
