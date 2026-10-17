"""Contract for AI review of historical companion replacement continuity."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


SCHEMA_VERSION = 2
DECISIONS = frozenset({
    'bind_descendant',
    'not_same_entity',
    'insufficient_evidence',
})
MAX_ACTIONS = 12
MAX_EVIDENCE_REFS = 96
MAX_EVOLUTION_LOG_REFS = 64
MAX_REASON_LENGTH = 2000

_TOP_LEVEL_FIELDS = frozenset({'schema_version', 'evidence_digest', 'actions'})
_ACTION_FIELDS = frozenset({
    'action_ref',
    'decision',
    'source_successor_ref',
    'current_item_ref',
    'evolution_log_refs',
    'evidence_refs',
    'reason',
})


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def stable_ref(prefix: str, *parts: object) -> str:
    return f'{prefix}_{canonical_digest([str(part) for part in parts])[:24]}'


def _optional_ref(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('引用必须是非空字符串或 null')
    return value.strip()


def _unique_refs(
    value: object,
    *,
    maximum: int,
    label: str,
) -> tuple[list[str] | None, str | None]:
    if not isinstance(value, list):
        return None, f'{label} 必须是数组'
    if len(value) > maximum:
        return None, f'{label} 超过上限 {maximum}'
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None, f'{label} 只能包含非空字符串'
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        return None, f'{label} 不得重复'
    return normalized, None


def validate_companion_historical_review(
    raw_review: object,
    *,
    evidence: dict,
) -> tuple[dict | None, str | None]:
    """Allowlist-project and validate complete, frozen, item-scoped decisions."""
    if not isinstance(raw_review, dict):
        return None, '判定结果必须是 JSON 对象'
    review = {
        key: raw_review[key]
        for key in _TOP_LEVEL_FIELDS
        if key in raw_review
    }
    if frozenset(review) != _TOP_LEVEL_FIELDS:
        return None, '判定结果缺少 schema_version、evidence_digest 或 actions'
    schema_version, _ = normalize_ascii_integer(
        review.get('schema_version'),
        minimum=SCHEMA_VERSION,
        maximum=SCHEMA_VERSION,
    )
    if schema_version != SCHEMA_VERSION:
        return None, '判定结果 schema_version 不受支持'
    expected_digest = str(evidence.get('evidence_digest') or '')
    if not expected_digest or review.get('evidence_digest') != expected_digest:
        return None, '判定证据摘要与冻结证据不一致'

    cases = evidence.get('cases')
    actions = review.get('actions')
    if not isinstance(cases, list) or not cases:
        return None, '冻结证据没有可审核的伙伴连续性记录'
    if len(cases) > MAX_ACTIONS:
        return None, f'连续性记录超过单次审核上限 {MAX_ACTIONS}'
    if not isinstance(actions, list) or len(actions) != len(cases):
        return None, 'actions 必须逐项覆盖全部冻结记录'

    cases_by_ref = {
        str(item.get('action_ref') or ''): item
        for item in cases
        if isinstance(item, dict)
    }
    if len(cases_by_ref) != len(cases) or '' in cases_by_ref:
        return None, '冻结证据的 action_ref 无效'

    normalized_by_ref: dict[str, dict] = {}
    for position, raw_action in enumerate(actions, start=1):
        if not isinstance(raw_action, dict):
            return None, f'第 {position} 个 action 必须是对象'
        action = {
            key: raw_action[key]
            for key in _ACTION_FIELDS
            if key in raw_action
        }
        if frozenset(action) != _ACTION_FIELDS:
            return None, f'第 {position} 个 action 字段不完整'
        action_ref = action.get('action_ref')
        if not isinstance(action_ref, str) or action_ref not in cases_by_ref:
            return None, f'第 {position} 个 action_ref 不属于冻结证据'
        if action_ref in normalized_by_ref:
            return None, f'action_ref {action_ref} 重复'
        case = cases_by_ref[action_ref]
        decision = action.get('decision')
        if decision not in DECISIONS:
            return None, f'action_ref {action_ref} 的 decision 无效'

        try:
            source_successor_ref = _optional_ref(action.get('source_successor_ref'))
            current_item_ref = _optional_ref(action.get('current_item_ref'))
        except ValueError as exc:
            return None, f'action_ref {action_ref}：{exc}'
        evolution_refs, refs_err = _unique_refs(
            action.get('evolution_log_refs'),
            maximum=MAX_EVOLUTION_LOG_REFS,
            label=f'action_ref {action_ref} 的 evolution_log_refs',
        )
        if refs_err:
            return None, refs_err
        evidence_refs, refs_err = _unique_refs(
            action.get('evidence_refs'),
            maximum=MAX_EVIDENCE_REFS,
            label=f'action_ref {action_ref} 的 evidence_refs',
        )
        if refs_err:
            return None, refs_err

        reason = action.get('reason')
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason.strip()) > MAX_REASON_LENGTH
        ):
            return None, f'action_ref {action_ref} 的 reason 无效'
        allowed_successors = {
            str(item.get('item_ref') or '')
            for item in case.get('source_new_items') or []
            if isinstance(item, dict)
        }
        allowed_current = {
            str(item.get('item_ref') or '')
            for item in case.get('current_items') or []
            if isinstance(item, dict)
        }
        allowed_evolutions = {
            str(item.get('evolution_log_ref') or '')
            for item in case.get('evolution_logs') or []
            if isinstance(item, dict)
        }
        allowed_evidence = {
            str(item.get('evidence_ref') or '')
            for item in case.get('evidence_records') or []
            if isinstance(item, dict)
        }
        if source_successor_ref and source_successor_ref not in allowed_successors:
            return None, f'action_ref {action_ref} 引用了未冻结的同场后继'
        if current_item_ref and current_item_ref not in allowed_current:
            return None, f'action_ref {action_ref} 引用了未冻结的当前实例'
        if any(item not in allowed_evolutions for item in evolution_refs or []):
            return None, f'action_ref {action_ref} 引用了未冻结的嬗变日志'
        if any(item not in allowed_evidence for item in evidence_refs or []):
            return None, f'action_ref {action_ref} 引用了未冻结的剧情证据'

        if decision == 'bind_descendant':
            if case.get('timeline_truncated'):
                return None, f'action_ref {action_ref} 的后续历史不完整，不能绑定谱系'
            if not source_successor_ref or not current_item_ref:
                return None, f'action_ref {action_ref} 绑定时必须选择同场后继与当前实例'
            successor = next(
                item for item in case.get('source_new_items') or []
                if item.get('item_ref') == source_successor_ref
            )
            current = next(
                item for item in case.get('current_items') or []
                if item.get('item_ref') == current_item_ref
            )
            if successor.get('type') != 'companion' or current.get('type') != 'companion':
                return None, f'action_ref {action_ref} 的绑定目标必须仍是伙伴'
            if current.get('source') != 'equipment':
                return None, f'action_ref {action_ref} 只能绑定当前随身伙伴实例'
            if not evidence_refs:
                return None, f'action_ref {action_ref} 绑定时必须引用剧情证据'

            frozen_evolutions = [
                item for item in case.get('evolution_logs') or []
                if isinstance(item, dict)
            ]
            evolution_positions = {
                str(item.get('evolution_log_ref') or ''): position
                for position, item in enumerate(frozen_evolutions)
            }
            selected_positions = [evolution_positions[ref] for ref in evolution_refs or []]
            if selected_positions != sorted(selected_positions):
                return None, f'action_ref {action_ref} 的嬗变引用必须按冻结时间顺序排列'
            selected_by_ref = {
                str(item.get('evolution_log_ref') or ''): item
                for item in frozen_evolutions
            }
            current_name = str(successor.get('name') or '').strip()
            for ref in evolution_refs or []:
                evolution = selected_by_ref[ref]
                old_name = str(evolution.get('old_name') or '').strip()
                new_name = str(evolution.get('new_name') or '').strip()
                if not old_name or not new_name or old_name != current_name:
                    return None, f'action_ref {action_ref} 的嬗变引用不能形成连续实体链'
                current_name = new_name
            if current_name != str(current.get('name') or '').strip():
                return None, f'action_ref {action_ref} 的嬗变链未落到所选当前实例'
        elif decision == 'not_same_entity':
            if not evidence_refs:
                return None, f'action_ref {action_ref} 判定无关时必须引用剧情证据'
            if (
                source_successor_ref is not None
                or current_item_ref is not None
                or evolution_refs
            ):
                return None, f'action_ref {action_ref} 判定无关时不得生成绑定字段'
        elif (
            source_successor_ref is not None
            or current_item_ref is not None
            or evolution_refs
            or evidence_refs
        ):
            return None, f'action_ref {action_ref} 证据不足时不得生成绑定字段'

        normalized_by_ref[action_ref] = {
            'action_ref': action_ref,
            'decision': decision,
            'source_successor_ref': source_successor_ref,
            'current_item_ref': current_item_ref,
            'evolution_log_refs': list(evolution_refs or []),
            'evidence_refs': list(evidence_refs or []),
            'reason': reason.strip(),
        }

    expected_refs = [str(item['action_ref']) for item in cases]
    if set(normalized_by_ref) != set(expected_refs):
        return None, 'actions 未完整覆盖冻结记录'
    return {
        'schema_version': SCHEMA_VERSION,
        'evidence_digest': expected_digest,
        'actions': [normalized_by_ref[action_ref] for action_ref in expected_refs],
    }, None


__all__ = [
    'DECISIONS',
    'MAX_ACTIONS',
    'SCHEMA_VERSION',
    'canonical_digest',
    'stable_ref',
    'validate_companion_historical_review',
]
