"""把完整 S1 成员快照投影为当前副本 AI 所需的特质子集。"""

from __future__ import annotations

import logging
from collections import Counter
from copy import copy, deepcopy
from dataclasses import dataclass
from typing import Iterable

from flask import current_app

from plugins.adventurer_s1.services.s1_description_ai_service import (
    description_source_hash,
)
from plugins.adventurer_s1.services.s1_trait_context_selection_service import (
    TRAIT_CONTEXT_RULE_SCHEMA_VERSION,
    TRAIT_CONTEXT_SELECTION_RECEIPT_VERSION,
    TRAIT_CONTEXT_TAGS_OUTLINE_FIELD,
    TraitContextCandidate,
    project_outline_trait_context_scope,
    select_traits_for_ai_context,
)

logger = logging.getLogger(__name__)

TRAIT_CONTEXT_SELECTION_LOG_FIELD = '_trait_context_selection'
_VALID_SELECTION_MODES = frozenset({'off', 'shadow', 'active'})


@dataclass(frozen=True)
class S1TraitContextProjection:
    members: list
    receipt: dict
    reused_frozen_receipt: bool


def get_s1_trait_context_selection_mode() -> str:
    raw = str(current_app.config.get(
        'ADVENTURER_S1_TRAIT_CONTEXT_SELECTION_MODE',
        'active',
    ) or '').strip().lower()
    if raw in _VALID_SELECTION_MODES:
        return raw
    logger.warning('S1 特质上下文选择模式非法，安全回退 off: %r', raw)
    return 'off'


def _iter_member_traits(members: Iterable[object]):
    for member in members or []:
        for trait in getattr(member, 'traits', None) or []:
            yield member, trait


def _forced_trait_ids_from_frozen_event(
    members: Iterable[object],
    frozen_event_resolution: dict | None,
) -> set[int]:
    """只按结构化 owner+kind+exact name 映射，不解释任何自然语言。"""
    exact_trait_sources: set[tuple[int, str]] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if value.get('source_kind') == 'trait':
                owner_id = value.get('source_owner_id')
                source_name = value.get('source_name')
                if (
                    type(owner_id) is int
                    and owner_id > 0
                    and isinstance(source_name, str)
                    and source_name.strip()
                ):
                    exact_trait_sources.add((owner_id, source_name.strip()))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(frozen_event_resolution)
    if not exact_trait_sources:
        return set()
    return {
        int(getattr(trait, 'id'))
        for member, trait in _iter_member_traits(members)
        if getattr(trait, 'id', None) is not None
        and (
            int(getattr(member, 'id')),
            str(getattr(trait, 'name', '') or '').strip(),
        ) in exact_trait_sources
    }


def _build_candidates(
    members: Iterable[object],
    *,
    forced_trait_ids: set[int],
) -> list[TraitContextCandidate]:
    candidates: list[TraitContextCandidate] = []
    for _member, trait in _iter_member_traits(members):
        trait_id = int(getattr(trait, 'id', 0) or 0)
        candidates.append(TraitContextCandidate(
            trait_id=trait_id,
            classification_source_hash=description_source_hash(
                getattr(trait, 'description', None),
                kind='trait',
            ),
            context_rule=getattr(trait, 'trait_context_rule', None),
            is_archived=bool(getattr(trait, 'is_archived', False)),
            has_asset_dependencies=bool(
                getattr(trait, 'has_asset_dependencies', False)
            ),
            # 进入成员快照前已经过稳定资产 ID/名称兼容链的 active 过滤。
            asset_dependencies_satisfied=True,
            forced_by_frozen_event=trait_id in forced_trait_ids,
        ))
    return candidates


def _project_members(members: Iterable[object], effective_trait_ids: set[int]) -> list:
    projected = []
    for member in members or []:
        member_copy = copy(member)
        member_copy.traits = [
            trait
            for trait in (getattr(member, 'traits', None) or [])
            if int(getattr(trait, 'id', 0) or 0) in effective_trait_ids
        ]
        projected.append(member_copy)
    return projected


def _copy_members_with_all_traits(members: Iterable[object]) -> list:
    """异常回退不依赖特质 ID，连损坏快照也保持完整可见。"""
    projected = []
    for member in members or []:
        member_copy = copy(member)
        member_copy.traits = list(getattr(member, 'traits', None) or [])
        projected.append(member_copy)
    return projected


def _active_scope(
    *,
    objective_outline: dict | None,
    target_dungeon: str,
    independent_location: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None, str]:
    from plugins.adventurer_s1.services.adventurer_s1_config_service import (
        get_s1_dungeon_context_tag,
    )

    errors: list[str] = []
    dungeon_tag = get_s1_dungeon_context_tag(target_dungeon)
    if dungeon_tag is None and not independent_location:
        errors.append('dungeon_context_tag_missing')
    outline_tags, scope_status, outline_errors = project_outline_trait_context_scope(
        objective_outline,
    )
    errors.extend(outline_errors)
    active_tags = tuple(sorted({
        *outline_tags,
        *((dungeon_tag,) if dungeon_tag else ()),
    }))
    return active_tags, tuple(dict.fromkeys(errors)), dungeon_tag, scope_status


def _reuse_frozen_receipt(
    members: list,
    candidates: list[TraitContextCandidate],
    *,
    frozen_receipt: dict | None,
    mode: str,
    target_dungeon: str,
    dungeon_context_tag: str | None,
) -> S1TraitContextProjection | None:
    receipt = frozen_receipt if isinstance(frozen_receipt, dict) else None
    if not receipt or (
        receipt.get('schema_version') != TRAIT_CONTEXT_SELECTION_RECEIPT_VERSION
        or receipt.get('rule_schema_version') != TRAIT_CONTEXT_RULE_SCHEMA_VERSION
        or receipt.get('mode') != mode
        or receipt.get('target_dungeon') != target_dungeon
        or receipt.get('dungeon_context_tag') != dungeon_context_tag
    ):
        return None
    raw_effective_ids = receipt.get('effective_trait_ids')
    raw_decisions = receipt.get('decisions')
    if not isinstance(raw_effective_ids, list) or not isinstance(raw_decisions, list):
        return None
    if any(type(value) is not int or value < 1 for value in raw_effective_ids):
        return None
    decisions_by_id: dict[int, dict] = {}
    for raw_decision in raw_decisions:
        if not isinstance(raw_decision, dict):
            return None
        trait_id = raw_decision.get('trait_id')
        source_hash = raw_decision.get('classification_source_hash')
        if (
            type(trait_id) is not int
            or trait_id < 1
            or not isinstance(source_hash, str)
            or trait_id in decisions_by_id
            or type(raw_decision.get('included')) is not bool
        ):
            return None
        decisions_by_id[trait_id] = raw_decision
    frozen_effective = set(raw_effective_ids)
    if len(frozen_effective) != len(raw_effective_ids) or not frozen_effective.issubset(decisions_by_id):
        return None
    included_ids = {trait_id for trait_id, row in decisions_by_id.items() if row['included']}
    if (
        mode == 'active' and frozen_effective != included_ids
    ) or (
        mode != 'active' and frozen_effective != set(decisions_by_id)
    ):
        return None
    decisions = []
    current_effective = set()
    for candidate in candidates:
        previous = decisions_by_id.get(candidate.trait_id)
        changed = (
            previous is None
            or previous['classification_source_hash'] != candidate.classification_source_hash
        )
        if changed or candidate.forced_by_frozen_event:
            decision = {
                'trait_id': candidate.trait_id,
                'classification_source_hash': candidate.classification_source_hash,
                'included': True,
                'reason': 'frozen_source_changed_fail_open' if changed else 'frozen_event_focus',
                'matched_clauses': [],
                'rule_errors': [],
            }
        else:
            decision = deepcopy(previous)
        if changed or candidate.forced_by_frozen_event or candidate.trait_id in frozen_effective:
            current_effective.add(candidate.trait_id)
        decisions.append(decision)
    # 回执必须描述实际送入主剧情的集合，后续 Observer 读取同一份结果。
    reconciled_receipt = deepcopy(receipt)
    # 历史回执无法区分主动省略和字段丢失，不能用本次大纲改写旧决策原因。
    reconciled_receipt.setdefault('scope_status', 'legacy_unknown')
    reconciled_receipt.update({
        'decisions': decisions,
        'candidate_selected_trait_ids': [row['trait_id'] for row in decisions if row['included']],
        'effective_trait_ids': [row.trait_id for row in candidates if row.trait_id in current_effective],
        'reason_counts': dict(sorted(Counter(row['reason'] for row in decisions).items())),
    })
    return S1TraitContextProjection(
        members=_project_members(members, current_effective),
        receipt=reconciled_receipt,
        reused_frozen_receipt=True,
    )


def _fail_open_projection(
    members: list,
    candidates: list[TraitContextCandidate],
    *,
    mode: str,
    target_dungeon: str,
    error_code: str,
) -> S1TraitContextProjection:
    effective_ids = [candidate.trait_id for candidate in candidates]
    return S1TraitContextProjection(
        members=_copy_members_with_all_traits(members),
        receipt={
            'schema_version': TRAIT_CONTEXT_SELECTION_RECEIPT_VERSION,
            'rule_schema_version': TRAIT_CONTEXT_RULE_SCHEMA_VERSION,
            'mode': mode,
            'target_dungeon': target_dungeon,
            'dungeon_context_tag': None,
            'active_context_tags': [],
            'candidate_selected_trait_ids': effective_ids,
            'effective_trait_ids': effective_ids,
            'scope_errors': [error_code],
            'scope_status': 'projection_error',
            'reason_counts': {'projection_fail_open': len(effective_ids)},
            'decisions': [
                {
                    'trait_id': candidate.trait_id,
                    'classification_source_hash': candidate.classification_source_hash,
                    'included': True,
                    'reason': 'projection_fail_open',
                    'matched_clauses': [],
                    'rule_errors': [error_code],
                }
                for candidate in candidates
            ],
        },
        reused_frozen_receipt=False,
    )


def build_s1_trait_context_projection(
    members: Iterable[object],
    *,
    objective_outline: dict | None,
    target_dungeon: str,
    independent_location: bool = False,
    frozen_event_resolution: dict | None = None,
    frozen_receipt: dict | None = None,
    mode_override: str | None = None,
) -> S1TraitContextProjection:
    """构建 AI 专用成员副本；完整输入对象与审计快照不做删减。"""
    member_rows = list(members or [])
    mode = str(mode_override or get_s1_trait_context_selection_mode()).strip().lower()
    if (
        isinstance(frozen_receipt, dict)
        and frozen_receipt.get('mode') == 'off'
        and frozen_receipt.get('freeze_fallback') is True
    ):
        # 同一日志已实际使用并冻结的完整集合，不随下一次全局 active 配置缩回子集。
        mode = 'off'
    if mode not in _VALID_SELECTION_MODES:
        mode = 'off'
    forced_ids = _forced_trait_ids_from_frozen_event(
        member_rows,
        frozen_event_resolution,
    )
    candidates: list[TraitContextCandidate] = []
    try:
        candidates = _build_candidates(
            member_rows,
            forced_trait_ids=forced_ids,
        )
        active_tags, scope_errors, dungeon_tag, scope_status = _active_scope(
            objective_outline=objective_outline,
            target_dungeon=target_dungeon,
            independent_location=independent_location,
        )
        reused = _reuse_frozen_receipt(
            member_rows,
            candidates,
            frozen_receipt=frozen_receipt,
            mode=mode,
            target_dungeon=target_dungeon,
            dungeon_context_tag=dungeon_tag,
        )
        if reused is not None:
            return reused

        selection = select_traits_for_ai_context(
            candidates,
            active_context_tags=active_tags,
            selector_enabled=mode != 'off',
            scope_errors=scope_errors,
            full_context_requested=scope_status == 'full_context_requested',
        )
        candidate_selected_ids = set(selection.selected_trait_ids)
        effective_ids = (
            candidate_selected_ids
            if mode == 'active'
            else {candidate.trait_id for candidate in candidates}
        )
        receipt = selection.to_receipt(
            mode=mode,
            effective_trait_ids=(
                candidate.trait_id
                for candidate in candidates
                if candidate.trait_id in effective_ids
            ),
        )
        receipt.update({
            'target_dungeon': target_dungeon,
            'dungeon_context_tag': dungeon_tag,
            'scope_status': scope_status,
        })
        return S1TraitContextProjection(
            members=_project_members(member_rows, effective_ids),
            receipt=receipt,
            reused_frozen_receipt=False,
        )
    except Exception as exc:
        logger.warning(
            'S1 特质上下文投影异常，安全回退完整 active 特质: %s',
            exc,
            exc_info=True,
        )
        return _fail_open_projection(
            member_rows,
            candidates,
            mode=mode,
            target_dungeon=target_dungeon,
            error_code=f'projection_exception_{type(exc).__name__}',
        )
