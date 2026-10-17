"""Contract for reviewing legacy final-fate destructive settlement actions."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


SCHEMA_VERSION = 1
DECISIONS = frozenset({
    'preserve_existing',
    'retire_without_successor',
    'replace_with_successors',
})
MAX_ACTIONS = 256
MAX_EVIDENCE_EVENT_INDEXES = 64
MAX_SUCCESSOR_REFS = 32
MAX_REASON_LENGTH = 2000

_TOP_LEVEL_FIELDS = frozenset({'schema_version', 'evidence_digest', 'actions'})
_ACTION_FIELDS = frozenset({
    'action_ref',
    'decision',
    'successor_refs',
    'evidence_event_indexes',
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


def validate_historical_review(
    raw_review: object,
    *,
    evidence: dict,
) -> tuple[dict | None, str | None]:
    """Project the allowlist, then validate complete item-by-item coverage."""
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
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        return None, '判定结果 schema_version 不受支持'
    expected_digest = str(evidence.get('evidence_digest') or '')
    if not expected_digest or review.get('evidence_digest') != expected_digest:
        return None, '判定证据摘要与冻结证据不一致'

    removed_assets = evidence.get('removed_assets')
    actions = review.get('actions')
    if not isinstance(removed_assets, list) or not removed_assets:
        return None, '冻结证据没有可审核的删除项'
    if len(removed_assets) > MAX_ACTIONS:
        return None, f'删除项超过单次审核上限 {MAX_ACTIONS}'
    if not isinstance(actions, list):
        return None, 'actions 必须是数组'
    if len(actions) != len(removed_assets):
        return None, 'actions 必须逐项覆盖全部冻结删除项'

    evidence_by_ref = {
        str(item.get('action_ref') or ''): item
        for item in removed_assets
        if isinstance(item, dict)
    }
    if len(evidence_by_ref) != len(removed_assets) or '' in evidence_by_ref:
        return None, '冻结证据的 action_ref 无效'
    valid_event_indexes = {
        int(item['event_index'])
        for item in evidence.get('events') or []
        if isinstance(item, dict)
        and isinstance(item.get('event_index'), int)
        and not isinstance(item.get('event_index'), bool)
    }

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
        if not isinstance(action_ref, str) or action_ref not in evidence_by_ref:
            return None, f'第 {position} 个 action_ref 不属于冻结证据'
        if action_ref in normalized_by_ref:
            return None, f'action_ref {action_ref} 重复'
        decision = action.get('decision')
        if decision not in DECISIONS:
            return None, f'action_ref {action_ref} 的 decision 无效'

        successor_refs = action.get('successor_refs')
        if (
            not isinstance(successor_refs, list)
            or len(successor_refs) > MAX_SUCCESSOR_REFS
            or any(not isinstance(item, str) or not item.strip() for item in successor_refs)
            or len(set(successor_refs)) != len(successor_refs)
        ):
            return None, f'action_ref {action_ref} 的 successor_refs 无效'
        allowed_successor_refs = set(
            evidence_by_ref[action_ref].get('same_log_successor_refs') or [],
        )
        if any(item not in allowed_successor_refs for item in successor_refs):
            return None, f'action_ref {action_ref} 引用了未冻结的后继项'

        event_indexes = action.get('evidence_event_indexes')
        if not isinstance(event_indexes, list) or len(event_indexes) > MAX_EVIDENCE_EVENT_INDEXES:
            return None, f'action_ref {action_ref} 的 evidence_event_indexes 无效'
        normalized_event_indexes: list[int] = []
        for item in event_indexes:
            normalized_index, _ = normalize_ascii_integer(item, minimum=0)
            if (
                type(normalized_index) is not int
                or normalized_index not in valid_event_indexes
            ):
                return None, f'action_ref {action_ref} 的 evidence_event_indexes 无效'
            normalized_event_indexes.append(normalized_index)
        if len(set(normalized_event_indexes)) != len(normalized_event_indexes):
            return None, f'action_ref {action_ref} 的 evidence_event_indexes 无效'
        event_indexes = normalized_event_indexes

        reason = action.get('reason')
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason.strip()) > MAX_REASON_LENGTH
        ):
            return None, f'action_ref {action_ref} 的 reason 无效'
        if decision == 'preserve_existing' and successor_refs:
            return None, f'action_ref {action_ref} 保留原项时不得声明后继项'
        if decision == 'retire_without_successor' and (successor_refs or not event_indexes):
            return None, f'action_ref {action_ref} 永久退场必须有剧情事件证据且无后继项'
        if decision == 'replace_with_successors' and (not successor_refs or not event_indexes):
            return None, f'action_ref {action_ref} 替换裁决必须有后继项和剧情事件证据'

        normalized_by_ref[action_ref] = {
            'action_ref': action_ref,
            'decision': decision,
            'successor_refs': list(successor_refs),
            'evidence_event_indexes': list(event_indexes),
            'reason': reason.strip(),
        }

    expected_refs = [str(item['action_ref']) for item in removed_assets]
    if set(normalized_by_ref) != set(expected_refs):
        return None, 'actions 未完整覆盖冻结删除项'
    return {
        'schema_version': SCHEMA_VERSION,
        'evidence_digest': expected_digest,
        'actions': [normalized_by_ref[action_ref] for action_ref in expected_refs],
    }, None
