"""S1 地牢公共地点状态的 AI 输出契约。"""

from __future__ import annotations

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)


_ALLOWED_KEYS = frozenset({
    'location_id', 'location_name', 'expected_revision', 'new_state', 'change_summary',
    'public_risk_notice',
})
_DUNGEON_EXPLORABILITY_ALLOWED_KEYS = frozenset({
    'expected_is_explorable', 'is_explorable', 'change_summary',
})
_NEW_LOCATION_INITIAL_CHANGE_SUMMARY = '首次记录该地点当前状态。'
_MAX_DB_INTEGER = 2_147_483_647
PUBLIC_RISK_NOTICE_MAX_LENGTH = 500
LOCATION_STATE_TEXT_MAX_LENGTH = 2000
LOCATION_CHANGE_SUMMARY_MAX_LENGTH = 500


def normalize_location_state_changes(
    value: object,
    *,
    max_changes: int = 4,
) -> tuple[list[dict], list[str]]:
    """规范化地点变更提议；任何不确定项均丢弃并返回诊断。"""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['location_state_changes 必须是数组']

    normalized: list[dict] = []
    diagnostics: list[str] = []
    seen: set[tuple[str, object]] = set()
    for index, raw in enumerate(value[:max(0, int(max_changes or 0))]):
        prefix = f'location_state_changes[{index}]'
        if not isinstance(raw, dict):
            diagnostics.append(f'{prefix} 必须是对象')
            continue
        raw = {str(key): item for key, item in raw.items() if str(key) in _ALLOWED_KEYS}

        raw_id = raw.get('location_id')
        location_id: int | None = None
        if raw_id is not None:
            normalized_id, _ = normalize_ascii_integer(
                raw_id,
                minimum=1,
                maximum=_MAX_DB_INTEGER,
            )
            if (
                type(normalized_id) is not int
                or not 1 <= normalized_id <= _MAX_DB_INTEGER
            ):
                diagnostics.append(f'{prefix}.location_id 必须是正整数')
                continue
            location_id = normalized_id

        location_name = raw.get('location_name')
        if location_name is not None:
            if not isinstance(location_name, str) or not location_name.strip():
                diagnostics.append(f'{prefix}.location_name 必须是非空字符串')
                continue
            location_name = location_name.strip()[:120]
        if location_id is None and not location_name:
            diagnostics.append(f'{prefix} 新地点必须提供 location_name')
            continue

        raw_revision = raw.get('expected_revision')
        normalized_revision, _ = normalize_ascii_integer(
            raw_revision,
            minimum=0,
            maximum=_MAX_DB_INTEGER,
        )
        if (
            type(normalized_revision) is not int
            or not 0 <= normalized_revision <= _MAX_DB_INTEGER
        ):
            diagnostics.append(f'{prefix}.expected_revision 必须是非负整数')
            continue
        expected_revision = normalized_revision
        if expected_revision < 0 or (location_id is None and expected_revision != 0):
            diagnostics.append(f'{prefix}.expected_revision 与地点类型不匹配')
            continue

        new_state = raw.get('new_state')
        change_summary = raw.get('change_summary')
        public_risk_notice = raw.get('public_risk_notice', '')
        if (
            not isinstance(public_risk_notice, str)
            or len(public_risk_notice.strip()) > PUBLIC_RISK_NOTICE_MAX_LENGTH
        ):
            diagnostics.append(
                f'{prefix}.public_risk_notice 必须是不超过 {PUBLIC_RISK_NOTICE_MAX_LENGTH} 字符的字符串'
            )
            continue
        if not isinstance(new_state, str) or not new_state.strip():
            diagnostics.append(f'{prefix}.new_state 必须是非空字符串')
            continue
        if location_id is None:
            change_summary = _NEW_LOCATION_INITIAL_CHANGE_SUMMARY
        elif not isinstance(change_summary, str) or not change_summary.strip():
            diagnostics.append(f'{prefix}.change_summary 必须是非空字符串')
            continue

        identity = ('id', location_id) if location_id is not None else ('name', location_name)
        if identity in seen:
            diagnostics.append(f'{prefix} 与同批其他地点重复')
            continue
        seen.add(identity)
        row = {
            'expected_revision': expected_revision,
            'new_state': new_state.strip()[:LOCATION_STATE_TEXT_MAX_LENGTH],
            'change_summary': change_summary.strip()[:LOCATION_CHANGE_SUMMARY_MAX_LENGTH],
        }
        if location_id is not None:
            row['location_id'] = location_id
        if location_name:
            row['location_name'] = location_name
        if 'public_risk_notice' in raw:
            row['public_risk_notice'] = public_risk_notice.strip()
        normalized.append(row)

    if len(value) > max_changes:
        diagnostics.append(f'location_state_changes 超过上限 {max_changes}')
    return normalized, diagnostics


def validate_location_state_changes_strict(
    value: object,
    *,
    max_changes: int = 4,
) -> tuple[list[dict], list[str]]:
    return normalize_location_state_changes(value, max_changes=max_changes)


def normalize_dungeon_explorability_change(
    value: object,
) -> tuple[dict | None, list[str]]:
    """规范化整个地牢可探索性的单次变更；未知状态只能作为预期值。"""
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, ['dungeon_explorability_change 必须是对象或 null']

    raw = {
        str(key): item
        for key, item in value.items()
        if str(key) in _DUNGEON_EXPLORABILITY_ALLOWED_KEYS
    }
    expected = raw.get('expected_is_explorable')
    if expected is not None:
        expected, _ = normalize_exact_boolean(expected)
        if type(expected) is not bool:
            return None, [
                'dungeon_explorability_change.expected_is_explorable 必须是 true、false 或 null'
            ]
    current = raw.get('is_explorable')
    current, _ = normalize_exact_boolean(current)
    if type(current) is not bool:
        return None, ['dungeon_explorability_change.is_explorable 必须是布尔值']
    summary = raw.get('change_summary')
    if expected is None:
        summary = (
            '首次确认该地牢当前仍可探索。'
            if current
            else '首次确认该地牢当前不可探索。'
        )
    elif not isinstance(summary, str) or not summary.strip():
        return None, ['dungeon_explorability_change.change_summary 必须是非空字符串']
    if expected is current:
        return None, ['dungeon_explorability_change 不得提交无变化结果']
    return {
        'expected_is_explorable': expected,
        'is_explorable': current,
        'change_summary': summary.strip()[:500],
    }, []


def validate_dungeon_explorability_change_strict(
    value: object,
) -> tuple[dict | None, list[str]]:
    return normalize_dungeon_explorability_change(value)
