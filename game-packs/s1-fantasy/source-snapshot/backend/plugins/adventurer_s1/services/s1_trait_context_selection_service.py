"""S1 特质上下文标签的确定性选择合同。

本模块只消费结构化标签，不从特质名称、正文或场景自然语言猜测语义。
规则采用 ``match_any``：同一子句内全部标签都命中才成立，多个子句任一成立
即可注入。主动能力由独立 activity 子句保守召回；具体场景机制仍可使用 AND
避免污染。任何缺失、过期或非法规则都 fail-open。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

TRAIT_CONTEXT_RULE_SCHEMA_VERSION = 's1_trait_context_v4'
TRAIT_CONTEXT_REVIEW_PROTOCOL_VERSION = 's1_trait_context_review_v2'
TRAIT_CONTEXT_SELECTION_RECEIPT_VERSION = 's1_trait_context_selection_v1'
TRAIT_CONTEXT_TAGS_OUTLINE_FIELD = 'trait_context_tags'
TRAIT_CONTEXT_REVIEW_DECISIONS = frozenset({
    'approved', 'corrected', 'unclassifiable',
})
TRAIT_CONTEXT_UNCLASSIFIABLE_REASONS = frozenset({
    'unbounded_current_relevance',
    'catalog_gap',
    'source_underdetermined',
})
TRAIT_CONTEXT_REVIEW_MAX_ISSUES = 8
TRAIT_CONTEXT_REVIEW_MAX_ISSUE_CHARS = 500

TRAIT_EFFECT_POLARITY_PURE_POSITIVE = 'pure_positive'
TRAIT_EFFECT_POLARITY_NON_PURE_POSITIVE = 'non_pure_positive'
TRAIT_EFFECT_POLARITIES = frozenset({
    TRAIT_EFFECT_POLARITY_PURE_POSITIVE,
    TRAIT_EFFECT_POLARITY_NON_PURE_POSITIVE,
})

# 这些上限属于版本化 JSON 合同，不是玩法平衡参数。修改时须同步升级 schema。
TRAIT_CONTEXT_MAX_CLAUSES = 8
TRAIT_CONTEXT_MAX_TAGS_PER_CLAUSE = 4
TRAIT_CONTEXT_MAX_ACTIVE_TAGS = 16
TRAIT_CONTEXT_MAX_SCENE_TAGS = 12
TRAIT_CONTEXT_MAX_TAG_LENGTH = 80

_TAG_PATTERN = re.compile(
    r'^[a-z][a-z0-9_]*:[a-z0-9][a-z0-9_]*$',
    re.ASCII,
)
_SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$', re.ASCII)

# 这是“上下文路由词表”，不是故事类型菜单。无法忠实落入这些值的特质必须
# always_include，场景 AI 也不得为了凑标签改写客观局势。
TRAIT_CONTEXT_TAG_CATALOG: dict[str, tuple[str, ...]] = {
    'activity': (
        'combat', 'exploration', 'traversal', 'investigation', 'social',
        'stealth', 'survival', 'protection', 'ritual', 'extraction',
        'crafting', 'restoration',
    ),
    'subject': (
        'humanoid', 'beast', 'undead', 'demon', 'dragon', 'construct',
        'spirit', 'elemental', 'plant', 'swarm', 'aberration', 'deity',
        'artifact', 'environment',
    ),
    'mechanism': (
        'physical_force', 'weapon', 'projectile', 'fire', 'cold',
        'lightning', 'poison', 'disease', 'curse', 'blood', 'shadow',
        'light', 'mind', 'soul', 'illusion', 'stealth', 'trap', 'barrier',
        'seal', 'spatial', 'temporal', 'gravity', 'sound', 'weather',
        'terrain', 'water', 'heat', 'corrosion', 'corruption', 'death',
        'healing', 'perception', 'mobility', 'magic', 'technology',
        'resource',
    ),
    'pressure': (
        'ambush', 'pursuit', 'siege', 'attrition', 'scarcity',
        'containment', 'crowd_control', 'environmental_hazard',
        'time_limit', 'displacement', 'isolation', 'mass_threat',
    ),
}

_STATIC_CONTEXT_TAGS = frozenset(
    f'{dimension}:{value}'
    for dimension, values in TRAIT_CONTEXT_TAG_CATALOG.items()
    for value in values
)

_TRAIT_CONTEXT_TAG_SEMANTIC_NOTES = (
    '空间连接改变、空间错位或传送统一由 mechanism:spatial 覆盖；'
    '被迫离位或分离本身构成场景压力时另用 pressure:displacement。'
    '目录故意没有 mechanism:displacement，不得跨命名空间创造该值。',
    '持续主动运转并改变现场、又是明确交互或成败对象的人造系统可用 '
    'subject:construct；仅作为被动背景的器物不因此成立。',
    '群体或机制限制接近、通过或行动自由用 pressure:containment；'
    '区域断联、成员或目标被空间分隔用 pressure:isolation。',
    'mechanism:trap 只覆盖已明确建立的预置触发装置或隐蔽危险；'
    '普通守卫战术、防卫节点或包围本身不是陷阱。',
)


def trait_context_tag_catalog_for_prompt(
    *,
    include_dungeons: bool,
) -> dict:
    """返回给分类/场景 AI 的单一标签目录；副本名只作稳定键映射。"""
    result = {
        'schema_version': TRAIT_CONTEXT_RULE_SCHEMA_VERSION,
        'match_logic': 'match_any 内：AND；match_any 之间：OR',
        **scene_trait_context_tag_catalog_for_prompt(),
    }
    if include_dungeons:
        from plugins.adventurer_s1.services.adventurer_s1_config_service import (
            get_s1_dungeon_context_tag_catalog,
        )

        result['dungeon_tags_by_display_name'] = (
            get_s1_dungeon_context_tag_catalog()
        )
    return result


def scene_trait_context_tag_catalog_for_prompt() -> dict:
    """场景 AI 只需要平面标签白名单，不接触特质规则结构。"""
    return {
        'static_tags': {
            dimension: [f'{dimension}:{value}' for value in values]
            for dimension, values in TRAIT_CONTEXT_TAG_CATALOG.items()
        },
        'semantic_notes': list(_TRAIT_CONTEXT_TAG_SEMANTIC_NOTES),
    }


def is_trait_context_tag(
    value: object,
    *,
    allow_dungeon: bool,
) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > TRAIT_CONTEXT_MAX_TAG_LENGTH
        or not _TAG_PATTERN.fullmatch(value)
    ):
        return False
    if value in _STATIC_CONTEXT_TAGS:
        return True
    if not allow_dungeon or not value.startswith('dungeon:'):
        return False
    from plugins.adventurer_s1.services.adventurer_s1_config_service import (
        get_s1_dungeon_context_tag_catalog,
    )

    return value in get_s1_dungeon_context_tag_catalog().values()


@dataclass(frozen=True)
class TraitContextRule:
    """白名单投影后的单条特质上下文规则。"""

    schema_version: str
    classification_source_hash: str
    always_include: bool
    match_any: tuple[tuple[str, ...], ...]

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'classification_source_hash': self.classification_source_hash,
            'always_include': self.always_include,
            'match_any': [list(clause) for clause in self.match_any],
        }


@dataclass(frozen=True)
class TraitContextCandidate:
    """选择器所需的最小特质事实，不携带玩家正文。"""

    trait_id: int
    classification_source_hash: str
    context_rule: object = None
    is_archived: bool = False
    has_asset_dependencies: bool = False
    asset_dependencies_satisfied: bool = True
    forced_by_frozen_event: bool = False


@dataclass(frozen=True)
class TraitContextDecision:
    trait_id: int
    classification_source_hash: str
    included: bool
    reason: str
    matched_clauses: tuple[tuple[str, ...], ...] = ()
    rule_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            'trait_id': self.trait_id,
            'classification_source_hash': self.classification_source_hash,
            'included': self.included,
            'reason': self.reason,
            'matched_clauses': [list(clause) for clause in self.matched_clauses],
            'rule_errors': list(self.rule_errors),
        }


@dataclass(frozen=True)
class TraitContextSelection:
    active_context_tags: tuple[str, ...]
    selected_trait_ids: tuple[int, ...]
    decisions: tuple[TraitContextDecision, ...]
    scope_errors: tuple[str, ...] = ()

    def to_receipt(self, *, mode: str, effective_trait_ids: Iterable[int]) -> dict:
        reasons = Counter(decision.reason for decision in self.decisions)
        return {
            'schema_version': TRAIT_CONTEXT_SELECTION_RECEIPT_VERSION,
            'rule_schema_version': TRAIT_CONTEXT_RULE_SCHEMA_VERSION,
            'mode': mode,
            'active_context_tags': list(self.active_context_tags),
            'candidate_selected_trait_ids': list(self.selected_trait_ids),
            'effective_trait_ids': [int(value) for value in effective_trait_ids],
            'scope_errors': list(self.scope_errors),
            'reason_counts': dict(sorted(reasons.items())),
            'decisions': [decision.to_dict() for decision in self.decisions],
        }


def _unique_errors(errors: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in errors if value))


def _project_clause(
    raw: object,
    *,
    field_name: str,
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    if not isinstance(raw, list):
        return None, (f'{field_name}_not_array',)
    if not raw:
        return None, (f'{field_name}_empty',)
    errors: list[str] = []
    if len(raw) > TRAIT_CONTEXT_MAX_TAGS_PER_CLAUSE:
        errors.append(f'{field_name}_overflow')
    projected: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not is_trait_context_tag(value, allow_dungeon=True):
            errors.append(f'{field_name}_item_invalid')
            continue
        if value in seen:
            errors.append(f'{field_name}_duplicate')
            continue
        seen.add(value)
        projected.append(value)
    if errors:
        return None, _unique_errors(errors)
    return tuple(sorted(projected)), ()


def project_scene_trait_context_tags(
    raw: object,
    *,
    allow_dungeon: bool,
    minimum: int = 1,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """白名单投影场景标签；非法、重复、缺失或溢出均显式报错。"""
    if not isinstance(raw, list):
        return (), ('active_context_tags_not_array',)
    errors: list[str] = []
    if len(raw) < minimum:
        errors.append('active_context_tags_too_few')
    if len(raw) > TRAIT_CONTEXT_MAX_ACTIVE_TAGS:
        errors.append('active_context_tags_overflow')
    projected: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not is_trait_context_tag(value, allow_dungeon=allow_dungeon):
            errors.append('active_context_tags_item_invalid')
            continue
        if value in seen:
            errors.append('active_context_tags_duplicate')
            continue
        seen.add(value)
        projected.append(value)
    return tuple(sorted(projected)), _unique_errors(errors)


def project_outline_trait_context_scope(
    objective_outline: object,
) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    """区分完整标签、明确全量、缺字段和非法字段；不推断省略意图。"""
    if (
        not isinstance(objective_outline, Mapping)
        or TRAIT_CONTEXT_TAGS_OUTLINE_FIELD not in objective_outline
    ):
        return (), 'missing_tags', ('active_context_tags_missing',)
    raw = objective_outline[TRAIT_CONTEXT_TAGS_OUTLINE_FIELD]
    if raw is None:
        return (), 'full_context_requested', ()
    tags, errors = project_scene_trait_context_tags(
        raw,
        allow_dungeon=False,
        minimum=2,
    )
    errors = list(errors)
    if len(tags) > TRAIT_CONTEXT_MAX_SCENE_TAGS:
        errors.append('active_context_tags_scene_overflow')
    if isinstance(raw, list) and not any(
        tag.startswith('activity:') for tag in tags
    ):
        errors.append('active_context_tags_activity_missing')
    if errors:
        return (), 'invalid_tags', _unique_errors(errors)
    return tags, 'tagged', ()


def project_trait_context_rule(
    raw: object,
) -> tuple[TraitContextRule | None, tuple[str, ...]]:
    """白名单投影持久化规则；额外字段不进入规范结果。"""
    if raw is None:
        return None, ()
    if not isinstance(raw, Mapping):
        return None, ('rule_not_object',)

    errors: list[str] = []
    if raw.get('schema_version') != TRAIT_CONTEXT_RULE_SCHEMA_VERSION:
        errors.append('schema_version_invalid')

    source_hash = raw.get('classification_source_hash')
    if not isinstance(source_hash, str) or not _SHA256_PATTERN.fullmatch(source_hash):
        errors.append('classification_source_hash_invalid')

    always_include = raw.get('always_include')
    if not isinstance(always_include, bool):
        errors.append('always_include_not_boolean')

    raw_match_any = raw.get('match_any')
    if not isinstance(raw_match_any, list):
        errors.append('match_any_not_array')
        raw_match_any = []
    elif len(raw_match_any) > TRAIT_CONTEXT_MAX_CLAUSES:
        errors.append('match_any_overflow')

    clauses: list[tuple[str, ...]] = []
    seen_clauses: set[tuple[str, ...]] = set()
    for index, raw_clause in enumerate(raw_match_any):
        clause, clause_errors = _project_clause(
            raw_clause,
            field_name=f'match_any_{index}',
        )
        errors.extend(clause_errors)
        if clause is None:
            continue
        if clause in seen_clauses:
            errors.append('match_any_duplicate_clause')
            continue
        seen_clauses.add(clause)
        clauses.append(clause)

    if isinstance(always_include, bool):
        if always_include and raw_match_any:
            errors.append('always_include_requires_empty_match_any')
        if not always_include and not raw_match_any:
            errors.append('scoped_rule_requires_match_any')

    if errors:
        return None, _unique_errors(errors)
    return TraitContextRule(
        schema_version=TRAIT_CONTEXT_RULE_SCHEMA_VERSION,
        classification_source_hash=source_hash,
        always_include=always_include,
        match_any=tuple(sorted(clauses)),
    ), ()


def normalize_generated_trait_context_rule(
    raw: object,
    *,
    classification_source_hash: str,
) -> tuple[dict | None, tuple[str, ...]]:
    """绑定原文哈希并严格投影路由；效果性质按独立闭合枚举保留。"""
    if not isinstance(raw, Mapping):
        return None, ('generated_rule_not_object',)
    candidate = {
        'schema_version': raw.get('schema_version'),
        'classification_source_hash': classification_source_hash,
        'effect_polarity': raw.get('effect_polarity'),
        'always_include': raw.get('always_include'),
        'match_any': raw.get('match_any'),
    }
    rule, errors = project_trait_context_rule(candidate)
    if rule is None:
        return None, errors
    normalized = rule.to_dict()
    effect_polarity = raw.get('effect_polarity')
    if effect_polarity in TRAIT_EFFECT_POLARITIES:
        normalized['effect_polarity'] = effect_polarity
    return normalized, errors


def project_trait_effect_polarity(
    raw_rule: object,
    *,
    classification_source_hash: str,
) -> str | None:
    """独立投影效果性质；路由字段无效时也不污染本轴。"""
    if not isinstance(raw_rule, Mapping):
        return None
    if raw_rule.get('schema_version') != TRAIT_CONTEXT_RULE_SCHEMA_VERSION:
        return None
    source_hash = raw_rule.get('classification_source_hash')
    if (
        not isinstance(source_hash, str)
        or not _SHA256_PATTERN.fullmatch(source_hash)
        or source_hash != classification_source_hash
    ):
        return None
    effect_polarity = raw_rule.get('effect_polarity')
    return (
        effect_polarity
        if effect_polarity in TRAIT_EFFECT_POLARITIES
        else None
    )


def _project_complete_trait_context_rule(
    raw_rule: object,
    *,
    classification_source_hash: str,
) -> dict | None:
    rule, errors = project_trait_context_rule(raw_rule)
    polarity = project_trait_effect_polarity(
        raw_rule,
        classification_source_hash=classification_source_hash,
    )
    if (
        rule is None
        or errors
        or rule.classification_source_hash != classification_source_hash
        or polarity is None
    ):
        return None
    normalized = rule.to_dict()
    normalized['effect_polarity'] = polarity
    return normalized


def project_completed_trait_context_review_receipt(
    raw_receipt: object,
    *,
    current_rule: object,
    classification_source_hash: str,
) -> tuple[str | None, tuple[str, ...]]:
    """校验新协议完成回执及其决定不变量；不读取 issues 的自然语言。"""
    if not isinstance(raw_receipt, Mapping):
        return None, ('review_receipt_not_object',)
    errors: list[str] = []
    if (
        raw_receipt.get('protocol_version')
        != TRAIT_CONTEXT_REVIEW_PROTOCOL_VERSION
    ):
        errors.append('review_protocol_version_invalid')
    if raw_receipt.get('state') != 'completed':
        errors.append('review_receipt_not_completed')

    decision = raw_receipt.get('decision')
    if decision not in TRAIT_CONTEXT_REVIEW_DECISIONS:
        errors.append('review_decision_invalid')
    issues = raw_receipt.get('issues')
    if (
        not isinstance(issues, list)
        or len(issues) > TRAIT_CONTEXT_REVIEW_MAX_ISSUES
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > TRAIT_CONTEXT_REVIEW_MAX_ISSUE_CHARS
            for value in (issues if isinstance(issues, list) else ())
        )
    ):
        errors.append('review_issues_invalid')
        issues = []
    reason = raw_receipt.get('unclassifiable_reason')

    candidate = _project_complete_trait_context_rule(
        raw_receipt.get('candidate_rule'),
        classification_source_hash=classification_source_hash,
    )
    reviewed = _project_complete_trait_context_rule(
        raw_receipt.get('reviewed_rule'),
        classification_source_hash=classification_source_hash,
    )
    current = _project_complete_trait_context_rule(
        current_rule,
        classification_source_hash=classification_source_hash,
    )
    if candidate is None:
        errors.append('review_candidate_rule_invalid')
    if reviewed is None:
        errors.append('reviewed_rule_invalid')
    if current is None or current != reviewed:
        errors.append('reviewed_rule_not_current')

    if decision == 'approved' and not (
        reviewed is not None
        and reviewed.get('always_include') is False
        and reviewed == candidate
        and not issues
        and reason is None
    ):
        errors.append('approved_review_invariant_invalid')
    elif decision == 'corrected' and not (
        reviewed is not None
        and reviewed.get('always_include') is False
        and reviewed != candidate
        and bool(issues)
        and reason is None
    ):
        errors.append('corrected_review_invariant_invalid')
    elif decision == 'unclassifiable' and not (
        reviewed is not None
        and reviewed.get('always_include') is True
        and reviewed.get('match_any') == []
        and bool(issues)
        and reason in TRAIT_CONTEXT_UNCLASSIFIABLE_REASONS
    ):
        errors.append('unclassifiable_review_invariant_invalid')

    unique_errors = _unique_errors(errors)
    return (str(decision), ()) if not unique_errors else (None, unique_errors)


def project_reviewed_trait_effect_polarity(
    raw_rule: object,
    *,
    classification_source_hash: str,
    audit_status: object,
) -> str | None:
    """仅投影独立审核通过且与当前完整原文哈希精确绑定的效果性质。"""
    if str(audit_status or '') != 'passed':
        return None
    return project_trait_effect_polarity(
        raw_rule,
        classification_source_hash=classification_source_hash,
    )


def _decision(
    candidate: TraitContextCandidate,
    included: bool,
    reason: str,
    *,
    matched_clauses: tuple[tuple[str, ...], ...] = (),
    rule_errors: tuple[str, ...] = (),
) -> TraitContextDecision:
    return TraitContextDecision(
        trait_id=candidate.trait_id,
        classification_source_hash=candidate.classification_source_hash,
        included=included,
        reason=reason,
        matched_clauses=matched_clauses,
        rule_errors=rule_errors,
    )


def _candidate_decision(
    candidate: TraitContextCandidate,
    *,
    active_context_tags: tuple[str, ...],
    scope_errors: tuple[str, ...],
    selector_enabled: bool,
    full_context_requested: bool,
) -> TraitContextDecision:
    if candidate.is_archived:
        return _decision(candidate, False, 'archived')
    if not candidate.asset_dependencies_satisfied:
        return _decision(candidate, False, 'asset_dependencies_inactive')
    if candidate.forced_by_frozen_event:
        return _decision(candidate, True, 'frozen_event_focus')
    if not selector_enabled:
        return _decision(candidate, True, 'selector_disabled')
    if scope_errors:
        return _decision(
            candidate,
            True,
            'invalid_context_scope_fail_open',
            rule_errors=scope_errors,
        )
    if full_context_requested:
        return _decision(candidate, True, 'full_context_requested')

    rule, rule_errors = project_trait_context_rule(candidate.context_rule)
    if candidate.context_rule is None:
        return _decision(candidate, True, 'unclassified_fail_open')
    if rule is None:
        return _decision(
            candidate,
            True,
            'invalid_rule_fail_open',
            rule_errors=rule_errors,
        )
    if candidate.classification_source_hash != rule.classification_source_hash:
        return _decision(candidate, True, 'stale_rule_fail_open')
    if rule.always_include:
        return _decision(candidate, True, 'always_include')
    if candidate.has_asset_dependencies:
        return _decision(candidate, True, 'asset_dependencies_active')

    active = set(active_context_tags)
    matched_clauses = tuple(
        clause for clause in rule.match_any if set(clause).issubset(active)
    )
    if matched_clauses:
        return _decision(
            candidate,
            True,
            'context_clause_match',
            matched_clauses=matched_clauses,
        )
    return _decision(candidate, False, 'no_context_clause_match')


def select_traits_for_ai_context(
    candidates: Sequence[TraitContextCandidate],
    *,
    active_context_tags: Iterable[str],
    selector_enabled: bool,
    scope_errors: Iterable[str] = (),
    full_context_requested: bool = False,
) -> TraitContextSelection:
    """按输入顺序选择特质，不按条数或字符数截断任何合法命中项。"""
    if not isinstance(selector_enabled, bool):
        raise TypeError('selector_enabled must be a boolean')
    if not isinstance(full_context_requested, bool):
        raise TypeError('full_context_requested must be a boolean')
    seen_trait_ids: set[int] = set()
    for candidate in candidates:
        if type(candidate.trait_id) is not int or candidate.trait_id < 1:
            raise ValueError('trait_id must be a positive integer')
        if candidate.trait_id in seen_trait_ids:
            raise ValueError('trait_id must be unique within one selection')
        if not _SHA256_PATTERN.fullmatch(candidate.classification_source_hash):
            raise ValueError('classification_source_hash must be sha256')
        seen_trait_ids.add(candidate.trait_id)

    active_tags, active_errors = project_scene_trait_context_tags(
        list(active_context_tags),
        allow_dungeon=True,
        minimum=0,
    )
    all_scope_errors = _unique_errors([*scope_errors, *active_errors])
    decisions = tuple(
        _candidate_decision(
            candidate,
            active_context_tags=active_tags,
            scope_errors=all_scope_errors,
            selector_enabled=selector_enabled,
            full_context_requested=full_context_requested,
        )
        for candidate in candidates
    )
    return TraitContextSelection(
        active_context_tags=active_tags,
        selected_trait_ids=tuple(
            decision.trait_id for decision in decisions if decision.included
        ),
        decisions=decisions,
        scope_errors=all_scope_errors,
    )
