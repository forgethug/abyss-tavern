"""深渊酒馆 S1 探险结果解析与轻量校验。

解析直接复用原版 `parse_deepseek_expedition_response`（分段文本 + JSON 结算的纯解析，
不依赖①②）。校验本期用轻量版：确保结构完整、覆盖全部出发成员、每人带合法
`id` / `remaining_hp`、新装备 `rarity` 合法，外加 `completion_check` 收束检查（遵循
S1 的收束状态合同，与 prompt 的「缺失/false 会被打回」承诺对齐）；
原版那套严格的串人/消耗一致性校验留待第二期对齐。

校验层用 pydantic 做结构校验，错误信息自带逐字段路径，便于拼回 validation_feedback
给 AI 重试。
"""
import copy
import json
import logging
import math
import re
from typing import Any, Dict, List, Optional

import json_repair
from flask import current_app
from pydantic import BaseModel, ConfigDict, ValidationError

from plugins.adventurer.services.adventurer_parser_service import (
    parse_deepseek_expedition_response,
    _extract_json_object_from_text,
    _strip_markdown_code_fence,
    _parse_story_events,
    _is_valid_expedition_result,
    _normalize_completion_check_payload,
    _EXPEDITION_EVENT_FIELDNAME_PREFIX_PATTERN,
    _sanitize_expedition_story_events,
)
from plugins.adventurer_s1.s1_item_types import (
    S1_INTERNAL_ITEM_TYPE_SET,
    S1_ITEM_TYPE_SET,
    normalize_item_type,
)
from plugins.adventurer_s1.services.s1_visible_name_policy import (
    visible_name_pollution_reason,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_finite_number,
    normalize_ascii_integer,
    normalize_exact_boolean,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    repair_s1_ai_json_brackets,
)
from plugins.adventurer_s1.services.s1_settlement_field_projection import (
    project_s1_member_settlement_fields,
    project_s1_top_settlement_fields,
)
from plugins.adventurer_s1.services.s1_existing_asset_update_contract import (
    S1_EXISTING_ASSET_UPDATE_ERROR_MARKER,
    normalize_s1_existing_asset_updates,
)
from plugins.adventurer_s1.services.s1_asset_recovery_contract import (
    S1_RECOVERY_TRIGGER_ERROR_MARKER,
    normalize_s1_recovery_triggers,
)
from plugins.adventurer_s1.services.s1_ai_asset_reference_service import (
    resolve_s1_ai_asset_references,
)
from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import (
    TRAIT_ASSET_DEPENDENCY_ERROR_MARKER,
    normalize_member_new_trait_asset_dependencies,
)
from plugins.adventurer_s1.services.s1_gold_settlement_contract import (
    S1_GOLD_SETTLEMENT_FIELD,
    S1_GOLD_SETTLEMENT_PERSONAL_REPAIR_MARKER,
    has_current_s1_gold_settlement_contract,
    normalize_s1_gold_settlement,
    project_s1_gold_settlement,
)
logger = logging.getLogger(__name__)

VALID_RARITIES = {'common', 'rare', 'epic', 'legendary'}
HIDDEN_BOSS_COMPLETION_FIELD = 'hidden_boss_completed'
LEGACY_HIDDEN_BOSS_COMPLETION_FIELD = 'hidden_boss_commission_completed'

_S1_EXPEDITION_CONSUMER_FIELDS = frozenset({
    'summary', 'events', 'member_updates', 'completion_check',
    'commission_results', 'hidden_boss_completed',
    'hidden_boss_commission_completed', 'pre_narrative_life_adjustments',
    'world_fact_updates', 'dm_handoff_note', 'fate_progress',
    'location_state_changes', 'dungeon_explorability_change', 'countermeasure_learning',
    'waiting_npc_updates', S1_GOLD_SETTLEMENT_FIELD,
    'unassigned_items',
})
_S1_CURRENT_MEMBER_UPDATE_FIELD_ORDER = (
    'id', 'remaining_hp', 'is_dead', 'death_reason',
    'score',
    'strength_increase', 'agility_increase', 'intelligence_increase',
    'luck_increase', 'max_hp_increase', 'new_class',
    'new_equipments', 'consumed_equipments',
    'new_traits', 'removed_traits', 'new_skills', 'removed_skills',
    'skill_updates', 'equipment_updates',
    'recovery_triggers', 'received_assets',
    'relationship_changes', 'mental_state',
    'history_story_append', 'story_threads',
)
_S1_CURRENT_MEMBER_UPDATE_FIELDS = frozenset(
    _S1_CURRENT_MEMBER_UPDATE_FIELD_ORDER
)
_S1_LEGACY_MEMBER_UPDATE_FIELD_ORDER = (
    'gold_change', 'exp_gain', 'exp', 'character_class',
    'used_equipments', 'used_skills',
)
_S1_LEGACY_MEMBER_UPDATE_FIELDS = frozenset(
    _S1_LEGACY_MEMBER_UPDATE_FIELD_ORDER
)
_S1_MEMBER_UPDATE_CONSUMER_FIELDS = (
    _S1_CURRENT_MEMBER_UPDATE_FIELDS | _S1_LEGACY_MEMBER_UPDATE_FIELDS
)
_S1_MEMBER_INTEGER_FIELDS = frozenset({
    'remaining_hp', 'exp_gain', 'exp',
    'strength_increase', 'agility_increase', 'intelligence_increase',
    'luck_increase', 'max_hp_increase',
})
_S1_MEMBER_FINITE_NUMBER_FIELDS = frozenset({'gold_change', 'score'})

# 主剧情允许模型省略没有内容的成员变化字段，以减少重复空容器和注意力噪声。
# 在任何 schema / Observer / Guard / 最终消费者读取前，把这些缺席字段机械还原为
# 旧合同中的稳定空值；这里不补语义标量，也不替模型猜测任何变化。
_S1_MEMBER_UPDATE_SPARSE_EMPTY_DEFAULTS = {
    'new_equipments': {},
    'consumed_equipments': [],
    'new_traits': [],
    'removed_traits': [],
    'new_skills': [],
    'removed_skills': [],
    'skill_updates': [],
    'equipment_updates': [],
    'recovery_triggers': [],
    'received_assets': [],
    'relationship_changes': [],
    'story_threads': [],
}

_S1_VISIBLE_NAME_POLLUTION_MARKER = '玩家可见名称包含结算协议污染'
_S1_ASSET_REFERENCE_ERROR_MARKER = '请求内资产引用合同错误'
_S1_STORY_ASSET_ID_POLLUTION_MARKER = '玩家可见剧情包含内部资产编号'
_S1_DEATH_REASON_REQUIRED_MARKER = '被标记死亡/失踪时必须提供 death_reason'
_S1_STORY_HARD_MIN_ERROR_MARKER = '玩家可见剧情正文异常短'
S1_MEMBER_CODENAME_ANCHOR_ERROR_MARKER = 'member_codename_anchor_missing'
S1_MEMBER_HISTORY_ANCHOR_ERROR_MARKER = 'member_history_anchor_missing'
# ── 校验错误分级 ──────────────────────────────────────────────────────────
# soft errors：不阻断主 AI，交给剧情审核 AI 在结算前补正。
# hard errors：必须重试主 AI 重新生成。
_SOFT_ERROR_PATTERNS = (
    '的 rarity 非法',       # rarity 值不在 VALID_RARITIES 内，observer 可修正
    '公共世界辅助字段已局部丢弃',
)
_S1_LIFE_ADJUSTMENT_MISSING_MARKER = '生命结果变化成员缺少追加链'
_S1_COUNTERMEASURE_LEARNING_ERROR_MARKER = 'countermeasure_learning'
_S1_WAITING_NPC_ERROR_MARKER = 'waiting_npc_updates'
_S1_COMMISSION_RESULT_ERROR_MARKER = 'commission_results 合同错误'
_S1_EXISTING_ASSET_UPDATE_ERROR_MARKER = S1_EXISTING_ASSET_UPDATE_ERROR_MARKER
_S1_TRAIT_ASSET_DEPENDENCY_ERROR_MARKER = TRAIT_ASSET_DEPENDENCY_ERROR_MARKER

S1_EXPEDITION_FORMAT_REPAIR_INLINE_STATUS_HEADING = (
    'inline_status_heading_joined'
)
S1_EXPEDITION_FORMAT_REPAIR_BOLD_SECTION_HEADINGS = (
    'bold_section_headings'
)
S1_EXPEDITION_FORMAT_REPAIR_MARKDOWN_SECTION_HEADINGS = (
    'markdown_section_headings'
)
S1_EXPEDITION_FORMAT_REPAIR_STATUS_BRACKET = 'single_status_json_bracket'
S1_EXPEDITION_PARSE_FAILURE_EMPTY_CONTENT = 'parse.empty_content'
S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_MISSING = (
    'parse.section_headers_missing'
)
S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_AMBIGUOUS = (
    'parse.section_headers_ambiguous'
)
S1_EXPEDITION_PARSE_FAILURE_STATUS_OBJECT_MISSING = (
    'parse.status_object_missing'
)
S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID = (
    'parse.status_json_invalid'
)
S1_EXPEDITION_PARSE_FAILURE_RESULT_NOT_OBJECT = 'parse.result_not_object'
S1_EXPEDITION_PARSE_FAILURE_SUMMARY_INVALID = 'parse.summary_invalid'
S1_EXPEDITION_PARSE_FAILURE_EVENTS_INVALID = 'parse.events_invalid'
S1_EXPEDITION_PARSE_FAILURE_MEMBER_UPDATES_INVALID = (
    'parse.member_updates_invalid'
)
S1_EXPEDITION_PARSE_FAILURE_NO_SUPPORTED_CONTAINER = (
    'parse.no_supported_container'
)
S1_EXPEDITION_PARSE_FAILURE_UNCLASSIFIED = 'parse.unclassified'
_S1_EXPEDITION_SECTION_LABELS = ('剧情描述', '状态更新')
_S1_EXPEDITION_LEGACY_SECTION_LABELS = ('规则判定', *_S1_EXPEDITION_SECTION_LABELS)
_S1_SECTION_HEADER_LINE_PATTERN = re.compile(
    r'^[ \t]*###[ \t]*(规则判定|剧情描述|状态更新)(?=[ \t\r\n{]|$)',
    re.MULTILINE,
)
_S1_COMPAT_SECTION_HEADER_LINE_PATTERN = re.compile(
    r'^(?P<indent>[ \t]*)'
    r'(?P<prefix>\*\*(?:#{1,6}[ \t]*)?|#{1,6}[ \t]*(?:\*\*)?)'
    r'(?P<label>规则判定|剧情描述|状态更新)[ \t]*'
    r'(?P<suffix>\*\*)?[ \t]*(?=\r?$)',
    re.MULTILINE,
)
_S1_STATUS_HEADER_TOKEN_PATTERN = re.compile(
    r'###[ \t]*状态更新(?=[ \t\r\n{]|$)',
)
_S1_EXPEDITION_PARSE_RETRY_FEEDBACK = {
    S1_EXPEDITION_PARSE_FAILURE_EMPTY_CONTENT:
        '请完整输出剧情描述和状态更新两部分。',
    S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_MISSING:
        '请完整输出且各仅输出一次“### 剧情描述”“### 状态更新”。',
    S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_AMBIGUOUS:
        '两个规定标题各出现一次，按剧情描述、状态更新的顺序输出。',
    S1_EXPEDITION_PARSE_FAILURE_STATUS_OBJECT_MISSING:
        '请在“### 状态更新”后输出一份完整 JSON 对象。',
    S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID:
        '状态更新必须是一份语法完整、可解析且无额外尾随内容的 JSON 对象。',
    S1_EXPEDITION_PARSE_FAILURE_RESULT_NOT_OBJECT:
        '状态更新顶层必须是 JSON 对象，不能是数组、字符串或其它类型。',
    S1_EXPEDITION_PARSE_FAILURE_SUMMARY_INVALID:
        '状态更新必须包含字符串类型的 summary。',
    S1_EXPEDITION_PARSE_FAILURE_EVENTS_INVALID:
        '请在“### 剧情描述”中完整输出可解析的玩家可见正文。',
    S1_EXPEDITION_PARSE_FAILURE_MEMBER_UPDATES_INVALID:
        '状态更新必须包含数组类型的 member_updates。',
    S1_EXPEDITION_PARSE_FAILURE_NO_SUPPORTED_CONTAINER:
        '请按规定完整输出“### 剧情描述”“### 状态更新”两部分。',
    S1_EXPEDITION_PARSE_FAILURE_UNCLASSIFIED:
        '请重新完整输出剧情描述与合法状态 JSON，包含全部必填内容。',
}


def validate_s1_expedition_codename_anchors(
    result_data: dict | None,
    member_name_alias: dict[int, str] | None,
    *,
    include_narrative: bool = True,
    history_alias_scope: set[str] | None = None,
) -> list[str]:
    """在真名还原前机械校验请求内成员代号的身份锚点。

    只比较后端冻结的 ``member_id -> 完整代号``；已被还原器支持的独立 M1
    与对应的【M1】等价，不从自然语言推断别名归属。summary/events 不能把
    本次全部成员代号整体替换成自造称呼；非空个人履历由该条 ``id`` 绑定归属，
    省略本人主语不补写姓名。这里不要求每名成员在正文轮流具名。履历错误单独标记，
    交由既有 Observer 局部修复；只在代号空间校验，不用真名子串猜测身份。
    """
    if not isinstance(result_data, dict) or not member_name_alias:
        return []

    expected_alias_by_id: dict[int, str] = {}
    for raw_member_id, raw_alias in member_name_alias.items():
        member_id, _ = normalize_ascii_integer(
            raw_member_id,
            minimum=1,
        )
        if (
            type(member_id) is int
            and member_id > 0
            and isinstance(raw_alias, str)
            and raw_alias
        ):
            expected_alias_by_id[member_id] = raw_alias
    if not expected_alias_by_id:
        return []

    # Normalize only known request-local tokens for the anchor check. The
    # existing restoration consumer still owns the final player-visible text.
    bare_to_alias = {
        alias[1:-1]: alias
        for alias in expected_alias_by_id.values()
        if re.fullmatch(r'【M[1-9][0-9]*】', alias)
    }

    def anchor_text(value: str) -> str:
        return re.sub(
            r'(?<![A-Za-z0-9_【])M[1-9][0-9]*(?![A-Za-z0-9_】])',
            lambda match: bare_to_alias.get(match.group(0), match.group(0)),
            value,
        )

    narrative_parts: list[str] = []
    summary = result_data.get('summary')
    if isinstance(summary, str):
        narrative_parts.append(summary)
    for raw_event in result_data.get('events') or []:
        if isinstance(raw_event, str):
            narrative_parts.append(raw_event)
        elif isinstance(raw_event, dict):
            for field_name in ('text', 'description', 'content'):
                field_value = raw_event.get(field_name)
                if isinstance(field_value, str):
                    narrative_parts.append(field_value)
    narrative_text = anchor_text('\n'.join(narrative_parts))

    errors: list[str] = []
    if include_narrative and not any(
        alias in narrative_text for alias in expected_alias_by_id.values()
    ):
        errors.append(
            f'{S1_MEMBER_CODENAME_ANCHOR_ERROR_MARKER}: '
            'summary/events 未保留任何本次完整成员代号'
        )

    updates_by_id: dict[int, dict] = {}
    raw_updates = result_data.get('member_updates')
    if isinstance(raw_updates, list):
        update_candidates = list(enumerate(raw_updates))
    elif isinstance(raw_updates, dict):
        update_candidates = list(raw_updates.items())
    else:
        update_candidates = []
    for raw_key, raw_update in update_candidates:
        if not isinstance(raw_update, dict):
            continue
        raw_member_id = raw_update.get('id', raw_key)
        member_id, _ = normalize_ascii_integer(
            raw_member_id,
            minimum=1,
        )
        if type(member_id) is int and member_id > 0:
            updates_by_id.setdefault(member_id, raw_update)

    for member_id, alias in sorted(expected_alias_by_id.items()):
        history_story = (updates_by_id.get(member_id) or {}).get(
            'history_story_append'
        )
        if (
            not isinstance(history_story, str)
            or not history_story.strip()
            or any(token not in (history_alias_scope if history_alias_scope is not None
                                 else expected_alias_by_id.values()) for token in re.findall(
                r'【(?:M|人物)[1-9][0-9]*】', anchor_text(history_story),
            ))
        ):
            errors.append(
                f'{S1_MEMBER_HISTORY_ANCHOR_ERROR_MARKER}: '
                f'member_updates[{member_id}].history_story_append '
                f'须为非空履历且所用成员代号属于本次冻结身份；所属成员 {alias}'
            )
    return errors


def matching_s1_history_anchor_repairs(result_data: dict, repairs: dict) -> dict[str, str]:
    """Retain only server-verified histories still identical to the candidate."""
    updates = (result_data or {}).get('member_updates') or []
    if not isinstance(updates, list) or not isinstance(repairs, dict):
        return {}
    histories: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        member_id, _ = normalize_ascii_integer(update.get('id'), minimum=1)
        if type(member_id) is not int:
            continue
        key = str(member_id)
        if key in histories:
            duplicate_ids.add(key)
        histories[key] = update.get('history_story_append')
    return {
        key: history for key, history in repairs.items()
        if key not in duplicate_ids and isinstance(history, str) and history.strip()
        and histories.get(key) == history
    }


def unresolved_s1_history_anchor_errors(errors: list[str], repairs: dict) -> list[str]:
    """Resolve backend-issued history errors only with a matching verified ID."""
    unresolved: list[str] = []
    for error in errors:
        match = re.match(
            rf'^{S1_MEMBER_HISTORY_ANCHOR_ERROR_MARKER}: '
            r'member_updates\[([1-9][0-9]*)\]\.history_story_append ',
            str(error),
        )
        if match is None or match.group(1) not in repairs:
            unresolved.append(error)
    return unresolved


_S1_MEMBER_SCORE_ERROR_MARKER = '成员表现评分无效'


def _classify_s1_validation_error(
    error: str,
    *,
    allow_observer_equipment_repairs: bool = False,
    allow_observer_death_reason_repairs: bool = False,
    allow_observer_gold_settlement_repairs: bool = False,
    allow_observer_commission_repairs: bool = False,
    allow_observer_score_repairs: bool = False,
) -> str:
    """Return 'soft' if the error can be fixed by the observer AI, else 'hard'."""
    if allow_observer_score_repairs and _S1_MEMBER_SCORE_ERROR_MARKER in error:
        return 'soft'
    if allow_observer_commission_repairs and _S1_COMMISSION_RESULT_ERROR_MARKER in error:
        return 'soft'
    if allow_observer_equipment_repairs and (
        _S1_OWNED_EQUIPMENT_ERROR_MARKER in error
        or _S1_CONSUMED_QUANTITY_ERROR_MARKER in error
        or _S1_CONSUMED_OVER_AVAILABLE_ERROR_MARKER in error
    ):
        return 'soft'
    if allow_observer_equipment_repairs and _S1_NEW_EQUIPMENT_QUANTITY_ERROR_MARKER in error:
        return 'soft'
    if allow_observer_equipment_repairs and _S1_VISIBLE_NAME_POLLUTION_MARKER in error:
        return 'soft'
    if (
        allow_observer_equipment_repairs
        and _S1_EXISTING_ASSET_UPDATE_ERROR_MARKER in error
    ):
        return 'soft'
    if (
        allow_observer_equipment_repairs
        and _S1_TRAIT_ASSET_DEPENDENCY_ERROR_MARKER in error
    ):
        return 'soft'
    if allow_observer_death_reason_repairs and _S1_DEATH_REASON_REQUIRED_MARKER in error:
        return 'soft'
    if (
        allow_observer_gold_settlement_repairs
        and S1_GOLD_SETTLEMENT_PERSONAL_REPAIR_MARKER in error
    ):
        return 'soft'
    for pattern in _SOFT_ERROR_PATTERNS:
        if pattern in error:
            return 'soft'
    return 'hard'


def get_s1_observer_repair_errors(errors: list[str]) -> list[str]:
    """Return structured validation errors that require semantic Observer review.

    The backend only identifies contract-specific, locally repairable mismatches here.
    It must not infer story semantics; that decision belongs to the Observer AI.
    """
    return [
        error for error in errors
        if _S1_OWNED_EQUIPMENT_ERROR_MARKER in error
        or _S1_CONSUMED_QUANTITY_ERROR_MARKER in error
        or _S1_CONSUMED_OVER_AVAILABLE_ERROR_MARKER in error
        or _S1_NEW_EQUIPMENT_QUANTITY_ERROR_MARKER in error
        or _S1_VISIBLE_NAME_POLLUTION_MARKER in error
        or _S1_DEATH_REASON_REQUIRED_MARKER in error
        or _S1_LIFE_ADJUSTMENT_MISSING_MARKER in error
        or _S1_COUNTERMEASURE_LEARNING_ERROR_MARKER in error
        or _S1_WAITING_NPC_ERROR_MARKER in error
        or _S1_EXISTING_ASSET_UPDATE_ERROR_MARKER in error
        or _S1_TRAIT_ASSET_DEPENDENCY_ERROR_MARKER in error
        or S1_GOLD_SETTLEMENT_PERSONAL_REPAIR_MARKER in error
        or _S1_COMMISSION_RESULT_ERROR_MARKER in error
        or S1_MEMBER_HISTORY_ANCHOR_ERROR_MARKER in error
        or _S1_MEMBER_SCORE_ERROR_MARKER in error
    ]


def get_s1_new_equipment_quantity_repair_errors(
    errors: list[str],
) -> list[str]:
    """Return only exact structural errors for new-equipment quantities."""
    return [
        error for error in errors
        if _S1_NEW_EQUIPMENT_QUANTITY_ERROR_MARKER in error
    ]


def get_s1_life_adjustment_repair_errors(errors: list[str]) -> list[str]:
    """Return only the exact backend marker for a missing main-story life chain."""
    return [
        error for error in errors
        if _S1_LIFE_ADJUSTMENT_MISSING_MARKER in error
    ]


def _visible_name_pollution_reason(value: object) -> str | None:
    """Return a mechanical protocol-pollution reason; never infer natural-language semantics."""
    return visible_name_pollution_reason(value)


def _iter_new_visible_names(raw_update: dict) -> list[tuple[str, object]]:
    """Collect explicit structured names that will become player-visible state."""
    rows: list[tuple[str, object]] = []
    for field_name in ('new_equipments', 'new_traits'):
        value = raw_update.get(field_name)
        if isinstance(value, dict):
            rows.extend((field_name, name) for name in value)
        elif isinstance(value, list):
            rows.extend(
                (field_name, item.get('name'))
                for item in value
                if isinstance(item, dict) and 'name' in item
            )
    skills = raw_update.get('new_skills')
    if isinstance(skills, list):
        rows.extend(
            ('new_skills', item.get('name'))
            for item in skills
            if isinstance(item, dict) and 'name' in item
        )
    for field_name in ('skill_updates', 'equipment_updates'):
        value = raw_update.get(field_name)
        if isinstance(value, list):
            rows.extend(
                (field_name, item.get('name'))
                for item in value
                if isinstance(item, dict) and 'name' in item
            )
    return rows


# 非法 rarity → 最接近合法值的确定性映射，与审核 AI 的 Rarity auto-fix 规则口径一致
# （uncommon→rare、mythic→legendary 等）。审核 AI 漏修 / 故障未修时由后端兜底，未知值落 common。
_RARITY_REPAIR_MAP = {
    'uncommon': 'rare',
    'unusual': 'rare',
    'superior': 'epic',
    'mythic': 'legendary',
    'mythical': 'legendary',
    'unique': 'legendary',
    'artifact': 'legendary',
    'divine': 'legendary',
    'normal': 'common',
    'ordinary': 'common',
    'poor': 'common',
    'trash': 'common',
}


def repair_illegal_rarity(value) -> str:
    """把非法 rarity 值映射到最接近的合法值（与审核 AI 的 Rarity auto-fix 规则一致）。"""
    text = str(value or '').strip().lower()
    if text in VALID_RARITIES:
        return text
    return _RARITY_REPAIR_MAP.get(text, 'common')


_ITEM_TYPE_REPAIR_MAP = {
    'item': 'special',
    'items': 'special',
    '材料': 'material',
    '素材': 'material',
    '道具': 'special',
    '装备': 'special',
    'skill': 'special',
}


def repair_illegal_item_type(value) -> str:
    """把 AI 常见 type 别名归一化；未知非法值安全回落为 special。"""
    text = str(value or '').strip().lower()
    mapped = _ITEM_TYPE_REPAIR_MAP.get(text, text)
    return normalize_item_type(mapped, allowed=S1_ITEM_TYPE_SET | S1_INTERNAL_ITEM_TYPE_SET)


def clamp_s1_soft_fields(result_data: dict) -> dict:
    """对探险结果里可机械修正的 new_equipments 字段做确定性兜底。

    用于审核 AI 漏修 / 故障未修时的后端最后一道兜底，保证非法 rarity/type 不落库。
    原地修改并返回同一对象。
    """
    if not isinstance(result_data, dict):
        return result_data
    updates = result_data.get('member_updates')
    if not isinstance(updates, list):
        return result_data
    for update in updates:
        if not isinstance(update, dict):
            continue
        new_eqs = update.get('new_equipments')
        if not isinstance(new_eqs, dict):
            continue
        for data in new_eqs.values():
            if not isinstance(data, dict):
                continue
            data['rarity'] = repair_illegal_rarity(data.get('rarity'))
            data['type'] = repair_illegal_item_type(data.get('type'))
    return result_data

# 归属类校验错误的标记短语：供 build_s1_retry_validation_feedback 脱敏识别
# （不回显被拒物品名，改给白名单正向引导）。
# - EQUIPMENT 标记仍由 _validate_member_owned_names 产出（新 consumed / 旧 used 越界），改文案须两处一致。
# - SKILL 标记保留作防御：used_skills 已降级为软处理（不再阻断、结算前剥离，见 expedition_runner
#   `_strip_unknown_used_skills`），当前不再产出该 error；若未来恢复 used_skills 阻断，反馈脱敏可直接复用。
_S1_OWNED_EQUIPMENT_ERROR_MARKER = '包含未随身携带的装备/道具'
_S1_OWNED_SKILL_ERROR_MARKER = '包含未携带的技能'
_S1_CONSUMED_QUANTITY_ERROR_MARKER = 'consumed_equipments 数量协议错误'
_S1_CONSUMED_OVER_AVAILABLE_ERROR_MARKER = 'consumed_equipments 扣减数量超过本人可用量'
_S1_NEW_EQUIPMENT_QUANTITY_ERROR_MARKER = 'new_equipments 数量协议错误'
_S1_NEW_EQUIPMENT_CONTAINER_ERROR_MARKER = 'new_equipments 容器协议错误'
S1_NEW_EQUIPMENT_LIST_REPAIR_CODE = 'new_equipments_list_container_normalized'


def _normalize_member_used_name_list(value: Any) -> list[str]:
    """Normalize AI-provided use lists such as ["x"] or [{"name": "x"}]."""
    if value is None:
        return []
    if isinstance(value, str):
        name = value.strip()
        return [name] if name else []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get('name') or '').strip()
        else:
            continue
        if name:
            names.append(name)
    return names


_RE_DICT_REPR_NAME = re.compile(r"""['"]name['"]\s*:\s*['"]([^'"]+)['"]""")
_RE_DICT_REPR_QTY = re.compile(r"""['"]quantity['"]\s*:\s*(\d+)""")


def _extract_name_qty_from_dict_repr(text: str) -> tuple[str, int] | None:
    """如果字符串是 Python/JSON dict 或 list repr（如 ``{'name': '药水'}``），
    提取 (name, quantity)。返回 None 表示不是 dict repr 字符串。"""
    stripped = text.strip()
    if not stripped or stripped[0] not in ('{', '['):
        return None
    name_m = _RE_DICT_REPR_NAME.search(stripped)
    if not name_m:
        return None
    name = name_m.group(1).strip()
    if not name:
        return None
    qty_m = _RE_DICT_REPR_QTY.search(stripped)
    quantity = int(qty_m.group(1)) if qty_m else 1
    return name, quantity


def normalize_s1_consumed_equipment_payload(
    value: Any,
    member_equipment: dict | None = None,
) -> list[dict]:
    """Normalize S1 asset deductions to ``[{name, quantity}]``.

    New AI output uses explicit quantity objects. String lists, repeated names,
    and strict legacy suffixes such as ``name×3`` remain readable so historical
    logs and audit patches keep working during the protocol migration.
    """
    from plugins.adventurer.services.adventurer_prompt_service import (
        _normalize_equipment_name_list_payload,
    )
    from plugins.adventurer_s1.services.s1_item_quantity_service import (
        coerce_item_quantity,
        split_stack_suffix,
    )

    if value is None:
        return []
    raw_entries = value if isinstance(value, list) else [value]
    quantities: dict[str, int] = {}
    for raw_entry in raw_entries[:100]:
        if isinstance(raw_entry, str):
            extracted = _extract_name_qty_from_dict_repr(raw_entry)
            if extracted:
                raw_name, raw_quantity = extracted
            else:
                # 如果字符串看起来像字典的开头（以 { 或 [ 开头），但不是有效的字典格式，跳过它
                stripped = raw_entry.strip()
                if stripped and stripped[0] in ('{', '['):
                    continue
                raw_name = stripped
                raw_quantity = 1
        elif isinstance(raw_entry, dict):
            raw_name = str(raw_entry.get('name') or '').strip()
            raw_quantity = raw_entry.get('quantity', 1)
        else:
            continue
        if not raw_name:
            continue
        base_name, quantity = split_stack_suffix(
            raw_name,
            coerce_item_quantity(raw_quantity, 1),
        )
        normalized = _normalize_equipment_name_list_payload(
            [base_name],
            member_equipment,
        )
        stable_name = normalized[0] if normalized else base_name
        if stable_name:
            quantities[stable_name] = min(
                999999,
                quantities.get(stable_name, 0) + quantity,
            )
    return [
        {'name': name, 'quantity': quantity}
        for name, quantity in quantities.items()
    ]


def normalize_s1_new_equipment_payload(value: Any) -> dict:
    """复用旧结构清洗，并保留 quantity 与 uses_left 的显式性。"""
    from plugins.adventurer.services.adventurer_parser_service import (
        _normalize_new_equipment_payload,
    )
    from plugins.adventurer_s1.services.s1_item_quantity_service import (
        coerce_item_quantity,
    )

    normalized = _normalize_new_equipment_payload(value) or {}
    if not isinstance(value, dict) or not normalized:
        return normalized
    for raw_name, raw_data in value.items():
        if not isinstance(raw_data, dict):
            continue
        single = _normalize_new_equipment_payload({raw_name: raw_data}) or {}
        for clean_name in single:
            if clean_name not in normalized:
                continue
            if 'uses_left' not in raw_data:
                # 旧共用 parser 会把缺失字段压成 None；S1 必须保留“省略”与
                # “显式 null”的差别，供发放边界决定是否应用图鉴/类型默认值。
                normalized[clean_name].pop('uses_left', None)
            if 'quantity' not in raw_data:
                continue
            normalized[clean_name]['quantity'] = coerce_item_quantity(
                raw_data.get('quantity'),
                1,
            )
            minutes, _ = normalize_ascii_integer(
                raw_data.get('recovery_minutes'), minimum=0,
            )
            normalized[clean_name]['recovery_minutes'] = (
                minutes if type(minutes) is int and minutes >= 0 else 0
            )
    return normalized


def _validate_s1_new_equipment_quantities(value: Any) -> list[str]:
    """Require every new item to declare an exact positive integer quantity."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return ['new_equipments 必须是“自然物品名称 -> 字段对象”的对象']

    errors: list[str] = []
    for raw_name, raw_data in value.items():
        item_path = f'new_equipments[{str(raw_name)!r}]'
        if not isinstance(raw_data, dict):
            errors.append(f'{item_path} 必须是字段对象并显式包含 quantity')
            continue
        if 'quantity' not in raw_data:
            errors.append(
                f'{item_path}.quantity 缺少，必须显式写 1~999999 的正整数'
            )
            continue
        quantity, _ = normalize_ascii_integer(
            raw_data.get('quantity'),
            minimum=1,
            maximum=999999,
        )
        if type(quantity) is not int or not 1 <= quantity <= 999999:
            errors.append(f'{item_path}.quantity 必须是 1~999999 的正整数')
            continue
        # 仅兼容无歧义的纯 ASCII 整数字符串；最终消费者始终接收规范整数。
        raw_data['quantity'] = quantity
    return errors


def _normalize_s1_new_asset_recovery_minutes(
    raw_update: dict,
    *,
    path: str,
) -> list[str]:
    """Validate the defaulted or explicitly proposed unified minute field."""
    errors: list[str] = []
    new_equipments = raw_update.get('new_equipments')
    if isinstance(new_equipments, dict):
        equipment_rows = list(new_equipments.items())
    else:
        equipment_rows = []
    for name, data in equipment_rows:
        if not isinstance(data, dict):
            continue
        field_path = f'{path}.new_equipments.{name}.recovery_minutes'
        if 'recovery_minutes' not in data:
            errors.append(f'{field_path}: 缺少必填字段')
            continue
        minutes, _ = normalize_ascii_integer(data.get('recovery_minutes'), minimum=0)
        if type(minutes) is not int or minutes < 0:
            errors.append(f'{field_path}: 必须是非负整数分钟')
            continue
        data['recovery_minutes'] = minutes

    new_skills = raw_update.get('new_skills')
    if isinstance(new_skills, list):
        for index, data in enumerate(new_skills):
            if not isinstance(data, dict):
                continue
            field_path = f'{path}.new_skills.{index}.recovery_minutes'
            if 'recovery_minutes' not in data:
                errors.append(f'{field_path}: 缺少必填字段')
                continue
            minutes, _ = normalize_ascii_integer(data.get('recovery_minutes'), minimum=0)
            if type(minutes) is not int or minutes < 0:
                errors.append(f'{field_path}: 必须是非负整数分钟')
                continue
            data['recovery_minutes'] = minutes
    return errors


def _default_omitted_asset_recovery_fields(
    raw_update: dict,
    member_asset_context: dict,
) -> None:
    """Keep recovery fields optional unless this story explicitly uses them."""
    raw_new_equipments = raw_update.get('new_equipments')
    equipment_rows = (
        raw_new_equipments.values()
        if isinstance(raw_new_equipments, dict) else ()
    )
    for data in equipment_rows:
        if isinstance(data, dict):
            data.setdefault('recovery_minutes', 0)
    for data in raw_update.get('new_skills') or []:
        if isinstance(data, dict):
            data.setdefault('recovery_minutes', 0)

    source_fields = (
        ('skill_updates', 'skill_id', 'skill_assets_by_id'),
        ('equipment_updates', 'equipment_id', 'equipment_assets_by_id'),
    )
    for update_field, id_field, context_field in source_fields:
        source_by_id = member_asset_context.get(context_field) or {}
        for entry in raw_update.get(update_field) or []:
            if not isinstance(entry, dict) or 'recovery_minutes' in entry:
                continue
            asset_id, _ = normalize_ascii_integer(entry.get(id_field), minimum=1)
            source = source_by_id.get(asset_id) if type(asset_id) is int else None
            entry['recovery_minutes'] = (
                source.get('recovery_minutes', 0)
                if isinstance(source, dict) else 0
            )
    raw_update.setdefault('recovery_triggers', [])


def _story_asset_protocol_pollution_reason(
    text: str,
    member_context: dict[int, dict] | None,
) -> str | None:
    """只识别精确协议标记，不把普通叙事数字猜成资产 ID。"""
    if not isinstance(text, str) or not text:
        return None

    kind_context_fields = (
        ('技能', 'skill_assets_by_id'),
        ('装备', 'equipment_assets_by_id'),
        ('特质', 'trait_assets_by_id'),
    )
    for context in (member_context or {}).values():
        if not isinstance(context, dict):
            continue
        for kind_label, context_field in kind_context_fields:
            for raw_id in (context.get(context_field) or {}):
                try:
                    asset_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                id_text = str(asset_id)
                # 所有既有模式都必须包含这个精确字符串。普通正文没有该 ID
                # 时不生成/编译逐资产正则，避免冲刷 re 缓存并阻塞 gevent。
                if id_text not in text:
                    continue
                explicit_patterns = (
                    rf'\[[^\]\n]{{0,80}}·\s*{kind_label}\s*{re.escape(id_text)}\]',
                    rf'{kind_label}(?:ID|id|编号|#)\s*[:：=#-]?\s*{re.escape(id_text)}(?!\d)',
                    rf'(?:skill_id|equipment_id|asset_id)\s*[:：=]\s*["\']?{re.escape(id_text)}(?!\d)',
                )
                if any(re.search(pattern, text) for pattern in explicit_patterns):
                    return f'出现{kind_label}数据库 ID 标记'
                if len(id_text) >= 3 and re.search(
                    rf'{kind_label}\s*{re.escape(id_text)}(?!\d)', text,
                ):
                    return f'出现{kind_label}数据库 ID 标记'

        reference_fields = (
            ('技能', 'skill_asset_ids_by_ref', 'skill_ref'),
            ('装备', 'equipment_asset_ids_by_ref', 'equipment_ref'),
        )
        for kind_label, context_field, ref_field in reference_fields:
            for raw_ref in (context.get(context_field) or {}):
                asset_ref = str(raw_ref or '')
                if not asset_ref or asset_ref not in text:
                    continue
                escaped_ref = re.escape(asset_ref)
                if re.search(
                    rf'(?:{ref_field}|asset_ref)\s*[:：=]\s*["\']?{escaped_ref}(?![A-Za-z0-9])',
                    text,
                ) or re.search(
                    rf'{kind_label}(?:引用|编号|#)?\s*{escaped_ref}(?![A-Za-z0-9])',
                    text,
                ):
                    return f'出现{kind_label}请求内引用 {asset_ref}'
    return None


def _validate_s1_consumed_equipment_quantities(value: Any) -> list[str]:
    """Return mechanical quantity protocol errors without reading narrative text."""
    if value is None:
        return []
    raw_entries = value if isinstance(value, list) else [value]
    errors: list[str] = []
    for index, raw_entry in enumerate(raw_entries[:100]):
        # 检查条目格式：必须是字典，不能是字符串
        if isinstance(raw_entry, str):
            stripped = raw_entry.strip()
            # 如果字符串看起来像字典的开头（以 { 或 [ 开头），报告格式错误
            if stripped and stripped[0] in ('{', '['):
                errors.append(f'consumed_equipments[{index}] 格式错误：必须是对象 {{"name":"稳定原名","quantity":正整数}}，不能是字符串')
            continue
        if not isinstance(raw_entry, dict):
            errors.append(f'consumed_equipments[{index}] 格式错误：必须是对象 {{"name":"稳定原名","quantity":正整数}}')
            continue
        # 检查 name 字段
        name = raw_entry.get('name')
        if not name or not isinstance(name, str) or not name.strip():
            errors.append(f'consumed_equipments[{index}].name 必须是非空字符串')
        elif name.strip()[0] in ('{', '['):
            errors.append(f'consumed_equipments[{index}].name 格式错误：不能是字典/列表字符串')
        # 检查 quantity 字段
        if 'quantity' not in raw_entry:
            errors.append(f'consumed_equipments[{index}].quantity 缺失')
            continue
        quantity, _ = normalize_ascii_integer(
            raw_entry.get('quantity'),
            minimum=1,
            maximum=999999,
        )
        if type(quantity) is not int or not 1 <= quantity <= 999999:
            errors.append(f'consumed_equipments[{index}].quantity 必须是 1~999999 的正整数')
    return errors


def _build_s1_member_known_equipment_names(
    member_context_entry: dict,
    raw_update: dict,
) -> set[str]:
    """成员本次结算可引用的装备名：出发随身 + 同条 update 的 new_equipments。"""
    from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix

    known = set(member_context_entry.get('equipment_names') or [])
    new_eqs = normalize_s1_new_equipment_payload(raw_update.get('new_equipments')) or {}
    for raw_name in new_eqs.keys():
        clean_name, _ = split_stack_suffix(str(raw_name), 1)
        if clean_name:
            known.add(clean_name)
    return known


def _build_s1_member_available_equipment_quantities(
    member_context_entry: dict,
    raw_update: dict,
) -> dict[str, int]:
    """Build exact departure + same-update quantities for mechanical validation."""
    from plugins.adventurer_s1.services.s1_item_quantity_service import (
        coerce_item_quantity,
        split_stack_suffix,
    )

    quantities = {
        str(name): max(0, int(quantity))
        for name, quantity in (
            member_context_entry.get('equipment_available_quantities') or {}
        ).items()
        if str(name).strip()
    }
    new_eqs = normalize_s1_new_equipment_payload(raw_update.get('new_equipments')) or {}
    for raw_name, raw_data in new_eqs.items():
        data = raw_data if isinstance(raw_data, dict) else {}
        clean_name, quantity = split_stack_suffix(
            str(raw_name),
            coerce_item_quantity(data.get('quantity', 1), 1),
        )
        if clean_name:
            available = (
                coerce_item_quantity(data.get('uses_left'), 1)
                if quantity <= 1 and data.get('uses_left') is not None
                else quantity
            )
            quantities[clean_name] = quantities.get(clean_name, 0) + available
    return quantities


def _build_s1_member_known_skill_names(
    member_context_entry: dict,
    raw_update: dict,
) -> set[str]:
    """成员本次结算可引用的技能名：出发已携带 + 同条 update 的 new_skills。"""
    from plugins.adventurer.services.adventurer_parser_service import _normalize_new_skills_payload

    known = set(member_context_entry.get('skill_names') or [])
    new_skills = _normalize_new_skills_payload(
        raw_update.get('new_skills'),
        split_semantic_name_desc=False,
    ) or []
    for skill in new_skills:
        if isinstance(skill, dict):
            name = str(skill.get('name') or '').strip()
            if name:
                known.add(name)
    return known


def _s1_equipment_name_known(name: str, equipment_names: set[str]) -> bool:
    """校验装备引用名是否可映射到成员已知装备（仅做结构化名称归一化）。"""
    if not name:
        return False
    from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix

    stable_name, _ = split_stack_suffix(name, 1)
    if stable_name in equipment_names:
        return True
    from plugins.adventurer.services.adventurer_prompt_service import (
        _normalize_equipment_name_list_payload,
    )

    fake_equipment = {item_name: {} for item_name in equipment_names}
    normalized = _normalize_equipment_name_list_payload([stable_name], fake_equipment)
    return bool(normalized and normalized[0] in equipment_names)


def _validate_member_owned_names(
    raw_update: dict,
    member_context: dict[int, dict] | None,
) -> list[str]:
    """Reject structured consume fields that reference another member's assets."""
    member_id, _ = normalize_ascii_integer(raw_update.get('id'), minimum=1)
    if type(member_id) is not int or member_id <= 0:
        return []

    ctx = (member_context.get(member_id) or {}) if member_context else {}
    member_name = ctx.get('name') or f'成员{member_id}'
    errors: list[str] = []
    errors.extend(
        f'{member_name}(#{member_id}) {_S1_CONSUMED_QUANTITY_ERROR_MARKER}: {error}'
        for error in _validate_s1_consumed_equipment_quantities(
            raw_update.get('consumed_equipments')
        )
    )
    if not member_context:
        return errors

    equipment_names = _build_s1_member_known_equipment_names(ctx, raw_update)
    skill_names = _build_s1_member_known_skill_names(ctx, raw_update)
    available_quantities = _build_s1_member_available_equipment_quantities(ctx, raw_update)
    if available_quantities:
        equipment_payload = {name: {} for name in equipment_names}
        for entry in normalize_s1_consumed_equipment_payload(
            raw_update.get('consumed_equipments'),
            equipment_payload,
        ):
            available = available_quantities.get(entry['name'])
            if available is not None and entry['quantity'] > available:
                errors.append(
                    f'{member_name} 的 {_S1_CONSUMED_OVER_AVAILABLE_ERROR_MARKER}: '
                    f'{entry["name"]} 申请扣减 {entry["quantity"]}，实际仅有 {available}'
                )
    for name in _normalize_member_used_name_list(raw_update.get('consumed_equipments')):
        if name in skill_names:
            continue
        if not _s1_equipment_name_known(name, equipment_names):
            owned_hint = '、'.join(sorted(equipment_names)) if equipment_names else '无'
            errors.append(
                f'{member_name} 的 consumed_equipments {_S1_OWNED_EQUIPMENT_ERROR_MARKER}: {name}'
                f'（该成员随身仅有: {owned_hint}）'
            )

    # used_skills 不再做阻断式归属校验：技能引用在结算里根本不落账
    # （_apply_skills_to_s1 只认 new_skills/removed_skills），且这类「未携带技能」绝大多数是
    # AI 从装备描述里捞出的能力名（如某杖描述写「可施展X / 产生X冲击波」）。为它把整条探险打回
    # 重试纯属浪费，改为软处理：结算前剥离未携带项 + 记 warning（见 _prepare_s1_member_updates_for_log）。
    return errors


class S1MemberUpdate(BaseModel):
    """S1 探险结算的单个成员更新。

    校验链硬约束：
    - id 在进入 pydantic 前已被限制为正整数或精确 ASCII 十进制字符串
    - remaining_hp 必须存在且非 None

    其他字段（new_equipments / new_traits / consumed_equipments 等）容忍声明的兼容格式，
    交由 normalize 与后续业务校验处理。
    """
    model_config = ConfigDict(extra='allow')

    id: int
    remaining_hp: int
    new_equipments: Optional[Dict[str, Any]] = None


class S1ExpeditionResult(BaseModel):
    """S1 探险 AI 输出的顶层 schema。"""
    model_config = ConfigDict(extra='allow')

    summary: str
    hidden_boss_completed: int = 0
    hidden_boss_commission_completed: int = 0
    events: list
    member_updates: List[S1MemberUpdate]


def _merge_s1_member_update_value(current, incoming, path: str):
    """机械合并重复成员更新字段；返回 (merged, conflict_path)。"""
    if type(current) is type(incoming) and current == incoming:
        return copy.deepcopy(current), None
    if current is None:
        return copy.deepcopy(incoming), None
    if incoming is None:
        return copy.deepcopy(current), None
    if isinstance(current, dict) and isinstance(incoming, dict):
        merged = copy.deepcopy(current)
        for key, value in incoming.items():
            child_path = f'{path}.{key}'
            if key not in merged:
                merged[key] = copy.deepcopy(value)
                continue
            merged_value, conflict = _merge_s1_member_update_value(
                merged[key], value, child_path,
            )
            if conflict:
                return None, conflict
            merged[key] = merged_value
        return merged, None
    if isinstance(current, list) and isinstance(incoming, list):
        if not current:
            return copy.deepcopy(incoming), None
        if not incoming:
            return copy.deepcopy(current), None
    return None, path


def normalize_s1_new_equipment_list_containers(
    result_data: dict,
    *,
    repair_diagnostics: list[str] | None = None,
) -> list[str]:
    """把无歧义的 ``[{name, ...}]`` 机械投影为规范名称映射。

    只移动显式非空 ``name`` 到对象键；独立条目全部保留且不合并。任一条目
    缺名、非对象或规范化后重名时保持原输入不变，让现有主生成重试链处理。
    """
    if not isinstance(result_data, dict):
        return []
    raw_updates = result_data.get('member_updates')
    if not isinstance(raw_updates, list):
        return []

    replacements: list[tuple[dict, dict, str]] = []
    errors: list[str] = []
    for update_index, raw_update in enumerate(raw_updates):
        if not isinstance(raw_update, dict):
            continue
        raw_equipments = raw_update.get('new_equipments')
        if not isinstance(raw_equipments, list):
            continue

        path = f'member_updates.{update_index}.new_equipments'
        normalized_equipments: dict[str, dict] = {}
        row_errors: list[str] = []
        for item_index, raw_item in enumerate(raw_equipments):
            item_path = f'{path}.{item_index}'
            if not isinstance(raw_item, dict):
                row_errors.append(
                    f'{_S1_NEW_EQUIPMENT_CONTAINER_ERROR_MARKER}: '
                    f'{item_path} 必须是字段对象'
                )
                continue
            raw_name = raw_item.get('name')
            if not isinstance(raw_name, str) or not raw_name.strip():
                row_errors.append(
                    f'{_S1_NEW_EQUIPMENT_CONTAINER_ERROR_MARKER}: '
                    f'{item_path}.name 必须是非空字符串'
                )
                continue
            stable_name = raw_name.strip()
            if stable_name in normalized_equipments:
                row_errors.append(
                    f'{_S1_NEW_EQUIPMENT_CONTAINER_ERROR_MARKER}: '
                    f'{path} 的 name 规范化后不得重复'
                )
                continue
            item_payload = copy.deepcopy(raw_item)
            item_payload.pop('name', None)
            normalized_equipments[stable_name] = item_payload

        if row_errors:
            errors.extend(row_errors)
            continue
        replacements.append((raw_update, normalized_equipments, path))

    if errors:
        return errors

    repaired_paths: list[str] = []
    for raw_update, normalized_equipments, path in replacements:
        raw_update['new_equipments'] = normalized_equipments
        repaired_paths.append(path)
    if repaired_paths:
        if repair_diagnostics is not None:
            for path in repaired_paths:
                if path not in repair_diagnostics:
                    repair_diagnostics.append(path)
        logger.info(
            'S1 探险结算已机械归一 new_equipments 列表容器: %s',
            repaired_paths,
        )
    return []


def normalize_s1_member_updates_by_id(
    result_data: dict,
    member_ids=None,
    repair_diagnostics: list[str] | None = None,
) -> list[str]:
    """按成员 id 保守合并重复更新，并可校验与出发成员集合完全一致。

    完全相同或机械上互补的字段会合并；两个非空列表不同、标量不同、同名字典键
    内容不同均视为冲突。冲突时不修改原始 member_updates，交由现有 AI 重试流程重写。
    """
    if not isinstance(result_data, dict):
        return ['结果不是合法对象']
    raw_updates = result_data.get('member_updates')
    if not isinstance(raw_updates, list):
        return []

    from plugins.adventurer_s1.services.s1_asset_transfer_service import normalize_received_assets
    transfer_errors = normalize_received_assets(result_data, member_ids)
    if transfer_errors:
        return transfer_errors

    container_errors = normalize_s1_new_equipment_list_containers(
        result_data,
        repair_diagnostics=repair_diagnostics,
    )
    if container_errors:
        return container_errors

    merged_rows: list = []
    merged_by_id: dict[int, dict] = {}
    duplicate_ids: set[int] = set()
    errors: list[str] = []
    for index, raw in enumerate(raw_updates):
        if not isinstance(raw, dict):
            errors.append(f'member_updates.{index}: 必须是对象')
            continue
        member_id, _ = normalize_ascii_integer(raw.get('id'), minimum=1)
        if type(member_id) is not int or member_id <= 0:
            errors.append(f'member_updates.{index}.id: 必须是整数')
            continue

        row = copy.deepcopy(raw)
        row['id'] = member_id
        if 'is_dead' in row:
            normalized_is_dead, _ = normalize_exact_boolean(row.get('is_dead'))
            if type(normalized_is_dead) is not bool:
                errors.append(f'member_updates.{index}.is_dead: 必须是布尔值')
                continue
            row['is_dead'] = normalized_is_dead
        scalar_invalid = False
        for field_name in _S1_MEMBER_INTEGER_FIELDS:
            if field_name not in row:
                continue
            normalized_integer, _ = normalize_ascii_integer(row.get(field_name))
            if type(normalized_integer) is not int:
                errors.append(
                    f'member_updates.{index}.{field_name}: 必须是精确整数'
                )
                scalar_invalid = True
                continue
            row[field_name] = normalized_integer
        for field_name in _S1_MEMBER_FINITE_NUMBER_FIELDS:
            if field_name not in row:
                continue
            # An explicit null score has the same missing-value semantics as an
            # omitted score. Current generation delegates both to field review.
            if field_name == 'score' and row[field_name] is None:
                continue
            normalized_number, _ = normalize_ascii_finite_number(
                row.get(field_name),
            )
            if (
                isinstance(normalized_number, bool)
                or not isinstance(normalized_number, (int, float))
                or not math.isfinite(normalized_number)
            ):
                errors.append(
                    f'member_updates.{index}.{field_name}: 必须是有限数值'
                )
                scalar_invalid = True
                continue
            row[field_name] = normalized_number
        if scalar_invalid:
            continue

        if member_id not in merged_by_id:
            merged_by_id[member_id] = row
            merged_rows.append(row)
            continue

        current = merged_by_id[member_id]
        duplicate_ids.add(member_id)
        candidate = copy.deepcopy(current)
        row_conflicts: list[str] = []
        for field_name, incoming in row.items():
            if field_name == 'id':
                continue
            if field_name not in candidate:
                candidate[field_name] = copy.deepcopy(incoming)
                continue
            merged_value, conflict = _merge_s1_member_update_value(
                candidate[field_name], incoming, field_name,
            )
            if conflict:
                row_conflicts.append(conflict)
            else:
                candidate[field_name] = merged_value
        if row_conflicts:
            errors.append(
                f'成员 {member_id} 存在冲突的重复结算字段: '
                f'{sorted(set(row_conflicts))}'
            )
        else:
            current.clear()
            current.update(candidate)

    if errors:
        return errors

    if member_ids is not None:
        try:
            expected_ids = {int(member_id) for member_id in member_ids}
        except (TypeError, ValueError):
            return ['出发成员 id 集合非法']
        actual_ids = set(merged_by_id)
        missing = expected_ids - actual_ids
        unexpected = actual_ids - expected_ids
        if missing:
            errors.append(f'结算缺少成员更新: {sorted(missing)}')
        if unexpected:
            errors.append(f'结算包含非本队成员更新: {sorted(unexpected)}')
        if errors:
            return errors

    result_data['member_updates'] = merged_rows
    if duplicate_ids:
        logger.warning(
            'S1 探险成员结算已机械合并重复 id: %s',
            sorted(duplicate_ids),
        )
    return []


def _default_s1_sparse_member_update_fields(result_data: dict) -> None:
    """把生成侧省略的无内容集合还原为下游沿用的稳定空值。"""
    for row in result_data.get('member_updates') or []:
        if not isinstance(row, dict):
            continue
        for field_name, empty_value in _S1_MEMBER_UPDATE_SPARSE_EMPTY_DEFAULTS.items():
            if field_name not in row:
                row[field_name] = copy.deepcopy(empty_value)


def _normalize_s1_exact_top_level_scalars(result_data: dict) -> list[str]:
    """Normalize only unambiguous scalar spellings before Pydantic coercion."""
    errors: list[str] = []
    for field_name in (
        HIDDEN_BOSS_COMPLETION_FIELD,
        LEGACY_HIDDEN_BOSS_COMPLETION_FIELD,
    ):
        if field_name not in result_data:
            continue
        normalized_value, _ = normalize_ascii_integer(
            result_data.get(field_name),
            minimum=0,
            maximum=1,
        )
        if type(normalized_value) is not int or normalized_value not in (0, 1):
            errors.append(f'{field_name} 必须是 0 或 1')
            continue
        result_data[field_name] = normalized_value

    return errors


def _validate_s1_completion_check(completion_check: object) -> list[str]:
    """Validate closure; member coverage and asset deltas have their own owners."""
    if not isinstance(completion_check, dict):
        return ['缺少 completion_check 或 completion_check 不是对象']
    errors = []
    for field_name in ('story_finished', 'core_resolved', 'exit_resolved'):
        value = completion_check.get(field_name)
        if not isinstance(value, bool):
            errors.append(f'completion_check.{field_name} 缺少或不是布尔值')
        elif field_name != 'core_resolved' and value is not True:
            errors.append(f'completion_check.{field_name} 必须为 true（故事须完整收束）')
    ending_type = completion_check.get('ending_type')
    if not isinstance(ending_type, str) or not ending_type.strip():
        errors.append('completion_check 缺少 ending_type')
    return errors


def _validate_s1_existing_asset_update_overlaps(
    raw_update: dict,
    member_context_entry: dict,
) -> list[str]:
    """拒绝同一原实例同时进入修改与删除/消耗协议。"""
    errors: list[str] = []
    removed_skill_names = set(
        _normalize_member_used_name_list(raw_update.get('removed_skills'))
    )
    skill_assets = member_context_entry.get('skill_assets_by_id') or {}
    for entry in raw_update.get('skill_updates') or []:
        if not isinstance(entry, dict):
            continue
        source = skill_assets.get(entry.get('skill_id')) or {}
        source_name = str(source.get('name') or '').strip()
        final_name = str(entry.get('name') or '').strip()
        if source_name in removed_skill_names or final_name in removed_skill_names:
            errors.append(
                f'skill_updates 中技能 ID {entry.get("skill_id")} '
                '不得同时进入 removed_skills'
            )

    consumed_quantities: dict[str, int] = {}
    for entry in normalize_s1_consumed_equipment_payload(
        raw_update.get('consumed_equipments'),
    ):
        name = str(entry.get('name') or '').strip()
        quantity, _ = normalize_ascii_integer(entry.get('quantity'), minimum=1)
        if name and type(quantity) is int and quantity > 0:
            consumed_quantities[name] = consumed_quantities.get(name, 0) + quantity
    equipment_assets = member_context_entry.get('equipment_assets_by_id') or {}
    source_quantities: dict[str, int] = {}
    for source in equipment_assets.values():
        if not isinstance(source, dict):
            continue
        source_name = str(source.get('name') or '').strip()
        source_quantity, _ = normalize_ascii_integer(
            source.get('quantity', 1), minimum=1,
        )
        if source_name and type(source_quantity) is int and source_quantity > 0:
            source_quantities[source_name] = (
                source_quantities.get(source_name, 0) + source_quantity
            )
    renamed_away_quantities: dict[str, int] = {}
    for entry in raw_update.get('equipment_updates') or []:
        if not isinstance(entry, dict):
            continue
        source = equipment_assets.get(entry.get('equipment_id')) or {}
        source_name = str(source.get('name') or '').strip()
        final_name = str(entry.get('name') or '').strip()
        source_quantity, _ = normalize_ascii_integer(
            source.get('quantity', 1), minimum=1,
        )
        if (
            source_name
            and final_name
            and source_name != final_name
            and type(source_quantity) is int
            and source_quantity > 0
        ):
            renamed_away_quantities[source_name] = (
                renamed_away_quantities.get(source_name, 0) + source_quantity
            )

    # 落库会先按 stable ID 原位改名，再把 consumed_equipments 的简称解析到
    # “改名后随身装备 + 本轮新增装备”快照。这里必须使用同一投影，否则原始
    # 简称可能绕过判重，随后命中并删除刚完成 equipment_updates 的同一实例。
    post_update_equipment_names = _build_s1_member_known_equipment_names(
        {}, raw_update,
    )
    for source_name, source_quantity in source_quantities.items():
        if source_quantity > renamed_away_quantities.get(source_name, 0):
            post_update_equipment_names.add(source_name)
    for entry in raw_update.get('equipment_updates') or []:
        if not isinstance(entry, dict):
            continue
        final_name = str(entry.get('name') or '').strip()
        if final_name:
            post_update_equipment_names.add(final_name)
    post_update_consumed_quantities: dict[str, int] = {}
    for entry in normalize_s1_consumed_equipment_payload(
        raw_update.get('consumed_equipments'),
        {name: {} for name in post_update_equipment_names},
    ):
        name = str(entry.get('name') or '').strip()
        quantity, _ = normalize_ascii_integer(entry.get('quantity'), minimum=1)
        if name and type(quantity) is int and quantity > 0:
            post_update_consumed_quantities[name] = (
                post_update_consumed_quantities.get(name, 0) + quantity
            )
    for entry in raw_update.get('equipment_updates') or []:
        if not isinstance(entry, dict):
            continue
        source = equipment_assets.get(entry.get('equipment_id')) or {}
        source_name = str(source.get('name') or '').strip()
        final_name = str(entry.get('name') or '').strip()
        remaining_source_quantity = max(
            0,
            source_quantities.get(source_name, 0)
            - renamed_away_quantities.get(source_name, 0),
        )
        conflicts_with_consumption = (
            final_name in post_update_consumed_quantities
            or consumed_quantities.get(source_name, 0) > remaining_source_quantity
        )
        if conflicts_with_consumption:
            errors.append(
                f'equipment_updates 中装备 ID {entry.get("equipment_id")} '
                '不得同时进入 consumed_equipments'
            )
    return errors


def _project_waiting_npc_profile(value):
    if not isinstance(value, dict):
        return value
    projected = {}
    if 'character_class' in value:
        projected['character_class'] = value.get('character_class')
    for field in ('equipments', 'skills', 'traits'):
        if field not in value:
            continue
        rows = value.get(field)
        if not isinstance(rows, list):
            projected[field] = rows
            continue
        allowed = (
            {'name', 'desc', 'type', 'rarity', 'quantity'}
            if field == 'equipments'
            else {'name', 'desc'}
        )
        projected[field] = [
            {
                key: item_value
                for key, item_value in row.items()
                if key in allowed
            }
            if isinstance(row, dict) else row
            for row in rows
        ]
    return projected


def _project_s1_expedition_consumer_fields(result: dict | None) -> dict | None:
    """Keep compatibility parsing while discarding fields no S1 consumer reads."""
    if not isinstance(result, dict):
        return None
    uses_current_contract = has_current_s1_gold_settlement_contract(result)
    projected = {
        key: value
        for key, value in result.items()
        if key in _S1_EXPEDITION_CONSUMER_FIELDS
    }
    if S1_GOLD_SETTLEMENT_FIELD in projected:
        projected[S1_GOLD_SETTLEMENT_FIELD] = project_s1_gold_settlement(
            projected[S1_GOLD_SETTLEMENT_FIELD]
        )
    updates = projected.get('member_updates')
    if isinstance(updates, list):
        member_update_fields = (
            _S1_CURRENT_MEMBER_UPDATE_FIELDS
            if uses_current_contract
            else _S1_MEMBER_UPDATE_CONSUMER_FIELDS
        )
        projected['member_updates'] = [
            {
                key: value
                for key, value in update.items()
                if key in member_update_fields
            }
            if isinstance(update, dict) else update
            for update in updates
        ]
        for update in projected['member_updates']:
            if not isinstance(update, dict):
                continue
            triggers = update.get('recovery_triggers')
            if isinstance(triggers, list):
                update['recovery_triggers'] = [
                    {
                        key: value
                        for key, value in trigger.items()
                        if key in {
                            'asset_kind', 'asset_ref', 'asset_id',
                            'evidence_event_indexes',
                        }
                    }
                    if isinstance(trigger, dict) else trigger
                    for trigger in triggers
                ]
    waiting_updates = projected.get('waiting_npc_updates')
    if isinstance(waiting_updates, list):
        projected['waiting_npc_updates'] = [
            {
                key: (
                    [
                        {
                            record_key: record_value
                            for record_key, record_value in record.items()
                            if record_key in {'kind', 'event_index'}
                        }
                        if isinstance(record, dict) else record
                        for record in value
                    ]
                    if key == 'records' and isinstance(value, list)
                    else _project_waiting_npc_profile(value)
                    if key == 'profile'
                    else value
                )
                for key, value in update.items()
                if key in {
                    'actor_id', 'incarnation', 'is_dead',
                    'faction_key', 'faction_role', 'records', 'profile',
                }
            }
            if isinstance(update, dict) else update
            for update in waiting_updates
        ]
    projected = project_s1_top_settlement_fields(projected)
    if isinstance(projected.get('member_updates'), list):
        projected['member_updates'] = [
            project_s1_member_settlement_fields(row)
            for row in projected['member_updates']
        ]
    return projected


def _get_s1_section_header_occurrences(text: str) -> list[tuple[int, str]]:
    """按位置合并规范标题与配对完整的 Markdown 标题，保留边界与重复证据。"""
    headers = {
        match.start(): match.group(1)
        for match in _S1_SECTION_HEADER_LINE_PATTERN.finditer(text)
    }
    for match in _S1_COMPAT_SECTION_HEADER_LINE_PATTERN.finditer(text):
        if ('**' in match.group('prefix')) == bool(match.group('suffix')):
            headers[match.start()] = match.group('label')
    return sorted(headers.items())


def _has_complete_s1_section_labels(labels: list[str]) -> bool:
    """正文和状态各一份；兼容旧规则段，按精确标题绑定而不猜测内容。"""
    return (
        len(labels) == len(set(labels))
        and set(labels) in (
            set(_S1_EXPEDITION_SECTION_LABELS),
            set(_S1_EXPEDITION_LEGACY_SECTION_LABELS),
        )
    )


def _split_s1_expedition_sections(text: str) -> dict[str, str]:
    labels = [label for _position, label in _get_s1_section_header_occurrences(text)]
    if not _has_complete_s1_section_labels(labels):
        return {}
    headers = list(_S1_SECTION_HEADER_LINE_PATTERN.finditer(text))
    if [match.group(1) for match in headers] != labels:
        return {}
    # 只消费标题本身，空正文不会把下一行状态标题吞作正文内容。
    return {
        match.group(1): text[
            match.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)
        ].strip()
        for index, match in enumerate(headers)
    }


def _normalize_exact_section_headings(text: str) -> tuple[str, str | None]:
    """只统一独占一行、唯一且完整的精确标题的 Markdown 层级/加粗。"""
    headers = _get_s1_section_header_occurrences(text)
    if not _has_complete_s1_section_labels([label for _position, label in headers]):
        return text, None

    repair_code = None

    def replace_heading(match: re.Match[str]) -> str:
        nonlocal repair_code
        prefix = match.group('prefix')
        if ('**' in prefix) != bool(match.group('suffix')):
            return match.group(0)
        if prefix.strip() == '###' and not match.group('suffix'):
            return match.group(0)
        if prefix.strip() == '**###' and repair_code is None:
            repair_code = S1_EXPEDITION_FORMAT_REPAIR_BOLD_SECTION_HEADINGS
        elif prefix.strip() != '**###':
            repair_code = S1_EXPEDITION_FORMAT_REPAIR_MARKDOWN_SECTION_HEADINGS
        return f"{match.group('indent')}### {match.group('label')}"

    normalized = _S1_COMPAT_SECTION_HEADER_LINE_PATTERN.sub(replace_heading, text)
    return normalized, repair_code


def _normalize_joined_status_heading(text: str) -> tuple[str, bool]:
    """在唯一、完整状态对象前的行内状态标题前插入换行。"""
    if not isinstance(text, str) or not text:
        return text, False

    anchored_headers = list(_S1_SECTION_HEADER_LINE_PATTERN.finditer(text))
    if any(match.group(1) == '状态更新' for match in anchored_headers):
        return text, False

    leading_labels = [match.group(1) for match in anchored_headers]
    if not _has_complete_s1_section_labels([*leading_labels, '状态更新']):
        return text, False

    viable_boundaries: list[re.Match[str]] = []
    decoder = json.JSONDecoder()
    for match in _S1_STATUS_HEADER_TOKEN_PATTERN.finditer(text):
        if match.start() <= anchored_headers[-1].end():
            continue
        line_start = max(
            text.rfind('\n', 0, match.start()),
            text.rfind('\r', 0, match.start()),
        ) + 1
        if not text[line_start:match.start()].strip():
            continue

        suffix = text[match.end():].strip()
        if not suffix:
            continue
        try:
            status_payload, consumed = decoder.raw_decode(suffix)
        except (TypeError, ValueError):
            continue
        if suffix[consumed:].strip() or not isinstance(status_payload, dict):
            continue
        if not (
            isinstance(status_payload.get('summary'), str)
            and isinstance(status_payload.get('member_updates'), list)
            and isinstance(status_payload.get('completion_check'), dict)
        ):
            continue
        viable_boundaries.append(match)

    if len(viable_boundaries) != 1:
        return text, False

    boundary = viable_boundaries[0]
    newline = '\r\n' if '\r\n' in text else '\n'
    return (
        f'{text[:boundary.start()]}{newline}{text[boundary.start():]}',
        True,
    )


def materialize_s1_narrative_repair_response(
    text: str, base_events: list[str],
) -> tuple[str, str | None]:
    """Apply an explicitly typed event patch; never infer scope from prose."""
    from .s1_ai_json_response_service import parse_s1_ai_json_content
    # Heading representation is lossless. Legacy bracket repair projects only
    # normal settlement fields and cannot prove this private mode's position.
    normalized, _ = _normalize_exact_section_headings(text)
    normalized, _ = _normalize_joined_status_heading(normalized)
    sections = _split_s1_expedition_sections(normalized)
    status_text = sections.get('状态更新') if sections else None
    parsed = parse_s1_ai_json_content(
        _strip_markdown_code_fence(status_text) if isinstance(status_text, str) else normalized,
        repair_mode='lossless_syntax_only',
    )
    if not parsed.ok:
        return text, 'parse.narrative_repair_json_invalid'
    mode = parsed.data.get('_narrative_events_mode')
    if mode not in ('append', 'full'):
        return text, 'parse.narrative_repair_events_mode_missing'
    # Keep the same visible-story authority as the normal parser when the
    # supported story/status representation repeats events inside status JSON.
    data = parse_s1_expedition_response(normalized) if status_text is not None else dict(parsed.data)
    if not isinstance(data, dict):
        return text, 'parse.narrative_repair_result_invalid'
    data.pop('_narrative_events_mode', None)
    events = data.get('events')
    if not isinstance(events, list) or not events or not all(isinstance(row, str) for row in events):
        return text, 'parse.narrative_repair_events_invalid'
    if mode == 'append':
        data['events'] = [*base_events, *events]
    return json.dumps(data, ensure_ascii=False, separators=(',', ':')), None


def normalize_s1_expedition_response_format(
    text: str,
) -> tuple[str, str | None]:
    """修复唯一、可证明的分段标题和单个 JSON 括号表示差异。

    正文和状态精确标题各自唯一，旧规则段存在时同样唯一；统一独占一行的 Markdown
    标题层级（1至6）与配对加粗。状态标题粘在正文末尾且其后恰好是一份完整状态对象时插入换行。
    正文及 JSON 字段和值不改写，不猜测标题同义词或补齐缺失内容。
    """
    if not isinstance(text, str) or not text:
        return text, None

    normalized, heading_repair_code = _normalize_exact_section_headings(text)
    normalized, inline_status_applied = _normalize_joined_status_heading(
        normalized,
    )
    occurrences = _get_s1_section_header_occurrences(normalized)
    sections = _split_s1_expedition_sections(normalized)
    if sections:
        status = _strip_markdown_code_fence(sections.get('状态更新', ''))
        fixed, _handled = _repair_single_s1_status_json_bracket(status)
        if fixed is not None:
            status_position = next(position for position, label in occurrences if label == '状态更新')
            start = normalized.find(status, status_position)
            if start >= 0:
                normalized = normalized[:start] + fixed + normalized[start + len(status):]
                return normalized, S1_EXPEDITION_FORMAT_REPAIR_STATUS_BRACKET
    elif not occurrences:
        # 历史纯 JSON 返回与分段状态段使用同一个候选生成器和成员合同。
        raw_json = _strip_markdown_code_fence(normalized)
        fixed, _handled = _repair_single_s1_status_json_bracket(raw_json)
        if fixed is not None:
            start = normalized.find(raw_json)
            if start >= 0:
                normalized = normalized[:start] + fixed + normalized[start + len(raw_json):]
                return normalized, S1_EXPEDITION_FORMAT_REPAIR_STATUS_BRACKET
    if heading_repair_code:
        return normalized, heading_repair_code
    if inline_status_applied:
        return normalized, S1_EXPEDITION_FORMAT_REPAIR_INLINE_STATUS_HEADING
    return normalized, None


def _diagnose_s1_expedition_payload(
    payload: object, *, diagnostics_out: dict | None = None,
) -> str:
    if diagnostics_out is not None:
        diagnostics_out.clear()
    if not isinstance(payload, dict):
        if diagnostics_out is not None:
            diagnostics_out.update(
                path='$', expected='object', actual_type=type(payload).__name__,
            )
        return S1_EXPEDITION_PARSE_FAILURE_RESULT_NOT_OBJECT
    if not isinstance(payload.get('summary'), str):
        if diagnostics_out is not None:
            diagnostics_out.update(
                path='summary', expected='string',
                actual_type=type(payload.get('summary')).__name__,
            )
        return S1_EXPEDITION_PARSE_FAILURE_SUMMARY_INVALID
    if not isinstance(payload.get('events'), list):
        if diagnostics_out is not None:
            diagnostics_out.update(
                path='events', expected='array',
                actual_type=type(payload.get('events')).__name__,
            )
        return S1_EXPEDITION_PARSE_FAILURE_EVENTS_INVALID
    if not isinstance(payload.get('member_updates'), list):
        if diagnostics_out is not None:
            diagnostics_out.update(
                path='member_updates', expected='array',
                actual_type=type(payload.get('member_updates')).__name__,
            )
        return S1_EXPEDITION_PARSE_FAILURE_MEMBER_UPDATES_INVALID
    return S1_EXPEDITION_PARSE_FAILURE_UNCLASSIFIED


def _repair_single_s1_status_json_bracket(text: str) -> tuple[str | None, bool]:
    """只提供探险身份投影；括号搜索、预算和唯一性判定由公共解析层拥有。"""
    def project_candidate(candidate):
        if (
            not isinstance(candidate.get('summary'), str)
            or not isinstance(candidate.get('member_updates'), list)
        ):
            return None
        projected = _project_s1_expedition_consumer_fields(copy.deepcopy(candidate))
        return None if normalize_s1_member_updates_by_id(projected) else projected

    return repair_s1_ai_json_brackets(text, candidate_projector=project_candidate)


def _load_s1_expedition_status_payload(
    status_section: str, *, diagnostics_out: dict | None = None,
) -> tuple[object, str | None]:
    """解析完整状态段，不能用提前配平的右括号截掉后续成员。"""
    cleaned = _strip_markdown_code_fence(status_section)
    fixed, bracket_repair_handled = _repair_single_s1_status_json_bracket(cleaned)
    if bracket_repair_handled:
        if fixed is None:
            return None, S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID
        return json.loads(fixed), None
    start_idx = cleaned.find('{')
    if start_idx == -1:
        return None, S1_EXPEDITION_PARSE_FAILURE_STATUS_OBJECT_MISSING
    status_json_str = cleaned[start_idx:]
    try:
        return json.loads(status_json_str), None
    except (TypeError, ValueError):
        pass
    try:
        repaired = json_repair.loads(status_json_str)
    except Exception:
        return None, S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID

    # 容错器可能把错位字段包装成新成员对象；无身份片段不能按邻近位置分配奖励。
    # 有效 JSON 的字段/类型错误仍由原 validator 处理，数字字符串兼容不变。
    updates = repaired.get('member_updates') if isinstance(repaired, dict) else None
    for index, row in enumerate(updates if isinstance(updates, list) else []):
        if isinstance(row, dict) and row.get('id') is None:
            if diagnostics_out is not None:
                diagnostics_out.update(
                    path=f'member_updates[{index}].id',
                    expected='member identity preserved after JSON repair',
                    actual_type='missing' if 'id' not in row else 'NoneType',
                )
            return None, S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID
    return repaired, None


def diagnose_s1_expedition_response_parse_failure(
    text: str, *, diagnostics_out: dict | None = None,
) -> str:
    """将主探险解析失败归入稳定、纯结构原因码，不猜测任何剧情语义。"""
    if diagnostics_out is not None:
        diagnostics_out.clear()
        diagnostics_out.update(
            path='$', expected='structured narrative or object',
            actual_type=type(text).__name__,
        )
    if not isinstance(text, str) or not text.strip():
        return S1_EXPEDITION_PARSE_FAILURE_EMPTY_CONTENT

    normalized, _repair_code = normalize_s1_expedition_response_format(text)
    cleaned = _strip_markdown_code_fence(normalized)
    header_labels = [
        label for _position, label in _get_s1_section_header_occurrences(cleaned)
    ]

    if header_labels:
        if diagnostics_out is not None:
            diagnostics_out.update(
                path='section_headers', expected=list(_S1_EXPEDITION_SECTION_LABELS),
                actual_type='list',
            )
        if any(
            header_labels.count(label) > 1
            for label in _S1_EXPEDITION_LEGACY_SECTION_LABELS
        ):
            return S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_AMBIGUOUS
        if any(
            label not in header_labels
            for label in _S1_EXPEDITION_SECTION_LABELS
        ):
            return S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_MISSING
        if not _has_complete_s1_section_labels(header_labels):
            return S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_AMBIGUOUS

        sections = _split_s1_expedition_sections(cleaned)
        if not sections:
            return S1_EXPEDITION_PARSE_FAILURE_SECTION_HEADERS_MISSING
        status_section = sections.get('状态更新', '')
        if diagnostics_out is not None:
            diagnostics_out.update(path='状态更新', expected='JSON object', actual_type='str')
        payload, parse_error = _load_s1_expedition_status_payload(
            status_section, diagnostics_out=diagnostics_out,
        )
        if parse_error:
            return parse_error

        if isinstance(payload, dict):
            story_events = _parse_story_events(sections.get('剧情描述', ''))
            if not story_events:
                if diagnostics_out is not None:
                    diagnostics_out.update(path='events', expected='non-empty narrative', actual_type='list')
                return S1_EXPEDITION_PARSE_FAILURE_EVENTS_INVALID
            payload['events'] = story_events
        return _diagnose_s1_expedition_payload(payload, diagnostics_out=diagnostics_out)

    _fixed, bracket_repair_handled = _repair_single_s1_status_json_bracket(cleaned)
    if bracket_repair_handled and _fixed is None:
        return S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID
    legacy_json_str = _extract_json_object_from_text(cleaned)
    if legacy_json_str is None:
        if cleaned.lstrip().startswith('['):
            return S1_EXPEDITION_PARSE_FAILURE_RESULT_NOT_OBJECT
        if '{' in cleaned:
            return S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID
        return S1_EXPEDITION_PARSE_FAILURE_NO_SUPPORTED_CONTAINER
    try:
        legacy_payload = json.loads(legacy_json_str)
    except (TypeError, ValueError):
        return S1_EXPEDITION_PARSE_FAILURE_STATUS_JSON_INVALID
    return _diagnose_s1_expedition_payload(legacy_payload, diagnostics_out=diagnostics_out)


def get_s1_expedition_parse_retry_feedback(reason_code: str) -> str:
    """返回与稳定解析原因码对应的固定正向输出合同。"""
    return _S1_EXPEDITION_PARSE_RETRY_FEEDBACK.get(
        reason_code,
        _S1_EXPEDITION_PARSE_RETRY_FEEDBACK[
            S1_EXPEDITION_PARSE_FAILURE_UNCLASSIFIED
        ],
    )


def parse_s1_expedition_response(text: str) -> dict | None:
    """解析主 AI 返回文本为 {summary, events, member_updates} 结构。

    分段输出先严格解析完整状态段，再沿既有 json-repair 路径尝试恢复结构；
    纯 JSON 先做相同的单括号修复，再走原版入口。结果仍须完整业务校验。
    finish_reason=length 由 owner 提前拒绝，
    不通过本解析器将截断输出当作完整候选。
    """
    normalized_text, _repair_code = normalize_s1_expedition_response_format(
        text,
    )
    cleaned = _strip_markdown_code_fence(normalized_text)
    if _get_s1_section_header_occurrences(cleaned):
        return _project_s1_expedition_consumer_fields(
            _parse_with_json_repair_fallback(normalized_text),
        )
    _fixed, bracket_repair_handled = _repair_single_s1_status_json_bracket(
        _strip_markdown_code_fence(normalized_text),
    )
    if bracket_repair_handled and _fixed is None:
        return None
    return _project_s1_expedition_consumer_fields(
        parse_deepseek_expedition_response(normalized_text),
    )


def _parse_with_json_repair_fallback(text: str) -> dict | None:
    """严格解析完整状态段，失败时沿既有 json-repair 路径恢复结构。

    保持分段与事件解析；从首个左括号读到状态段末尾，避免错位右括号导致成员丢失。
    无法保留成员身份时返回 None，由原解析失败预算处理，不构造部分可消费候选。
    """
    cleaned = _strip_markdown_code_fence(text)
    sections = _split_s1_expedition_sections(cleaned)
    if not sections:
        return None

    status_section = sections.get('状态更新', '')
    if not status_section:
        return None

    repaired, parse_error = _load_s1_expedition_status_payload(status_section)
    if parse_error or not isinstance(repaired, dict):
        return None

    story_events = _parse_story_events(sections.get('剧情描述', ''))
    if not story_events:
        return None
    repaired['events'] = story_events

    if _is_valid_expedition_result(repaired):
        return repaired
    return None


def _format_pydantic_error(err: dict) -> str:
    """把 pydantic ValidationError 单条 errors() 输出转成可读的中文路径描述。"""
    loc = err.get('loc', ())
    msg = err.get('msg', '')
    if not loc:
        return msg
    path = '.'.join(str(part) for part in loc)
    return f'{path}: {msg}'


def _normalize_s1_commission_results(
    result_data: dict,
    commission_context: list | None,
) -> list[str]:
    """Check complete formal-commission deltas against the frozen encounter set."""
    if commission_context is None:
        # Historical callers without a frozen encounter set cannot invent one.
        return []
    expected = {}
    for context in commission_context:
        if not isinstance(context, dict):
            return [f'{_S1_COMMISSION_RESULT_ERROR_MARKER}: 冻结委托身份无效']
        cid, _ = normalize_ascii_integer(context.get('commission_id'), minimum=1)
        if type(cid) is not int or cid <= 0 or cid in expected:
            return [f'{_S1_COMMISSION_RESULT_ERROR_MARKER}: 冻结委托身份无效']
        expected[cid] = context
    raw = result_data.get('commission_results')
    if raw is None and not expected:
        result_data['commission_results'] = []
        return []
    if not isinstance(raw, list):
        return [f'{_S1_COMMISSION_RESULT_ERROR_MARKER}: 必须完整覆盖冻结委托列表']

    errors = []
    normalized = {}
    projected = []
    for index, row in enumerate(raw):
        path = f'{_S1_COMMISSION_RESULT_ERROR_MARKER}: commission_results[{index}]'
        if not isinstance(row, dict):
            errors.append(f'{path} 必须为对象')
            projected.append(row)
            continue
        # Match the existing consumer's precedence. When completed_count is
        # present, legacy aliases are inert extras, even if they disagree.
        values = {key: row[key] for key in (
            'commission_id', 'completed',
        ) if key in row}
        for count_field in ('completed_count', 'count', 'progress_count'):
            if count_field in row:
                values['completed_count'] = row[count_field]
                break
        projected.append(values)
        cid, _ = normalize_ascii_integer(values.get('commission_id'), minimum=1)
        completed, _ = normalize_ascii_integer(values.get('completed'), minimum=0)
        count, _ = normalize_ascii_integer(values.get('completed_count'), minimum=0)
        if (
            type(cid) is not int or cid not in expected
            or type(completed) is not int or completed not in (0, 1)
            or type(count) is not int or count < 0
        ):
            errors.append(f'{path} 身份、完成标记或本次新增数无效')
            continue
        context = expected[cid]
        required, _ = normalize_ascii_integer(context.get('required_count'), minimum=1)
        progress, _ = normalize_ascii_integer(context.get('progress_count', 0), minimum=0)
        if type(required) is int and required > 0 and type(progress) is int and progress >= 0:
            if completed != int(progress + count >= required):
                errors.append(f'{path} 完成标记与冻结累计进度不一致')
                continue
        value = {'commission_id': cid, 'completed_count': count, 'completed': completed}
        if cid in normalized and normalized[cid] != value:
            errors.append(f'{path} 同一委托存在冲突结果')
            continue
        normalized[cid] = value
    missing = sorted(set(expected) - set(normalized))
    if missing:
        errors.append(f'{_S1_COMMISSION_RESULT_ERROR_MARKER}: 缺少委托结果 {missing}')
    result_data['commission_results'] = (
        projected if errors else [normalized[cid] for cid in expected]
    )
    return errors


def validate_s1_expedition_result(
    result_data: dict | None,
    member_ids,
    member_context: dict[int, dict] | None = None,
    raw_paragraph_count: int | None = None,
    raw_story_char_count: int | None = None,
    skip_length_checks: bool = False,
    allow_observer_equipment_repairs: bool = False,
    allow_observer_death_reason_repairs: bool = False,
    allow_observer_gold_settlement_repairs: bool = False,
    countermeasure_learning_enabled: bool | None = None,
    require_gold_settlement: bool = False,
    repair_diagnostics: list[str] | None = None,
    require_member_scores: bool = False,
    allow_observer_score_repairs: bool = False,
    commission_context: list | None = None,
    allow_observer_commission_repairs: bool = False,
) -> tuple[bool, list[str]]:
    """轻量校验探险结果。返回 (is_valid, errors)。

    member_ids: 本次出发成员的 id 集合，用于确认结算覆盖全员。
    raw_paragraph_count: 三段式“剧情描述”按空行分割后的自然段数；纯 JSON 兼容输出
        传入非空 events 数量。低于配置值会使分批推送缺少足够单元，因此直接拒绝。
    raw_story_char_count: 玩家可见正文去除空白后的有效字符数。常规探险低于异常短硬底线时
        直接拒绝；当前硬底线已在本轮主剧情 Prompt 中明确，Observer 不接收该独立硬底线。
    skip_length_checks: 门票制特殊机缘副本为 True，仅跳过正文异常短硬底线；
        分批推送段落契约仍适用。
    """
    if not isinstance(result_data, dict):
        return False, ['结果不是合法对象'], []

    # Observer/Guard 覆盖和旧恢复点也经过同一无损嵌套投影；服务器回执保留。
    result_data.update(project_s1_top_settlement_fields(result_data))
    if isinstance(result_data.get('member_updates'), list):
        result_data['member_updates'] = [
            project_s1_member_settlement_fields(row)
            for row in result_data['member_updates']
        ]

    # 当前合同只消费顶层金币包与现行成员字段。模型偶尔复述部署前字段时，
    # 将其视为未消费的兼容附加字段；没有当前合同标记的在途旧结果仍完整保留。
    if has_current_s1_gold_settlement_contract(result_data):
        for update in result_data.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            for field in _S1_LEGACY_MEMBER_UPDATE_FIELDS:
                update.pop(field, None)

    # 隐藏 Boss 线索 / 特殊副本门票由后端托管；AI 误写 used/consumed 时先剥离再校验归属，
    # 避免误报重试导致整队解散（胜利扣物走 settle_clue_victory_rewards / 发车扣票）。
    from plugins.adventurer_s1.services.s1_item_use_condition_service import (
        strip_backend_managed_equipment_refs,
    )
    # Validation owns no writes: return the catalog connection before the
    # remaining in-memory checks, route fallback, or next prompt build.
    strip_backend_managed_equipment_refs(result_data, independent_read=True)

    if isinstance(result_data.get('events'), list):
        result_data['events'] = _sanitize_expedition_story_events(result_data['events'])

    if 'unassigned_items' in result_data:
        from .s1_team_loot_contract import normalize_team_loot
        pool_items, pool_errors = normalize_team_loot(
            result_data['unassigned_items'], events=result_data.get('events') or [],
        )
        if pool_errors:
            return False, pool_errors, []
        result_data['unassigned_items'] = pool_items

    member_update_errors = normalize_s1_member_updates_by_id(
        result_data,
        member_ids,
        repair_diagnostics=repair_diagnostics,
    )
    if member_update_errors:
        return False, member_update_errors, []
    _default_s1_sparse_member_update_fields(result_data)

    new_equipment_quantity_errors: list[str] = []
    for index, raw_update in enumerate(result_data.get('member_updates') or []):
        if not isinstance(raw_update, dict):
            continue
        new_equipment_quantity_errors.extend(
            f'{_S1_NEW_EQUIPMENT_QUANTITY_ERROR_MARKER}: '
            f'member_updates.{index}.{error}'
            for error in _validate_s1_new_equipment_quantities(
                raw_update.get('new_equipments')
            )
        )

    asset_update_errors: list[str] = []
    commission_errors = _normalize_s1_commission_results(result_data, commission_context)
    event_count = len(result_data.get('events') or [])
    gold_settlement_errors = normalize_s1_gold_settlement(
        result_data,
        member_ids=member_ids,
        event_count=event_count,
        require_current_contract=require_gold_settlement,
    )
    if gold_settlement_errors and (
        not allow_observer_gold_settlement_repairs
        or any(
            S1_GOLD_SETTLEMENT_PERSONAL_REPAIR_MARKER not in error
            for error in gold_settlement_errors
        )
    ):
        return False, gold_settlement_errors, []
    for index, raw_update in enumerate(result_data.get('member_updates') or []):
        if not isinstance(raw_update, dict):
            continue
        member_id, _ = normalize_ascii_integer(raw_update.get('id'), minimum=1)
        member_context_entry = (
            (member_context or {}).get(member_id) or {}
            if type(member_id) is int
            else {}
        )
        reference_errors = resolve_s1_ai_asset_references(
            raw_update,
            member_asset_context=member_context_entry,
            path=f'member_updates.{index}',
        )
        if reference_errors:
            asset_update_errors.extend(
                f'{_S1_ASSET_REFERENCE_ERROR_MARKER}: {error}'
                for error in reference_errors
            )
            continue
        _default_omitted_asset_recovery_fields(
            raw_update,
            member_context_entry,
        )
        normalized_errors = normalize_s1_existing_asset_updates(
            raw_update,
            member_asset_context=member_context_entry,
            event_count=event_count,
            path=f'member_updates.{index}',
            require_asset_context=True,
        )
        new_asset_recovery_errors = _normalize_s1_new_asset_recovery_minutes(
            raw_update,
            path=f'member_updates.{index}',
        )
        recovery_trigger_errors = normalize_s1_recovery_triggers(
            raw_update,
            member_asset_context=member_context_entry,
            event_count=event_count,
            path=f'member_updates.{index}',
            require_asset_context=True,
        )
        trait_dependency_errors = normalize_member_new_trait_asset_dependencies(
            raw_update,
            member_asset_context=member_context_entry,
            path=f'member_updates.{index}',
        )
        overlap_errors = [
            f'member_updates.{index}: {error}'
            for error in _validate_s1_existing_asset_update_overlaps(
                raw_update,
                member_context_entry,
            )
        ]
        # 本场原实例变化先通过同一身份、证据和重叠校验并进入结算回执，
        # 随后才按成员终态没收装备；死亡本身不使此前合法变化成为合同错误。
        asset_update_errors.extend(
            f'{_S1_EXISTING_ASSET_UPDATE_ERROR_MARKER}: {error}'
            for error in [
                *normalized_errors,
                *new_asset_recovery_errors,
                *(
                    f'{S1_RECOVERY_TRIGGER_ERROR_MARKER}: {error}'
                    for error in recovery_trigger_errors
                ),
                *overlap_errors,
            ]
        )
        asset_update_errors.extend(trait_dependency_errors)
    exact_scalar_errors = _normalize_s1_exact_top_level_scalars(result_data)
    if exact_scalar_errors:
        return False, exact_scalar_errors, []

    auxiliary_diagnostics: list[str] = []
    location_state_diagnostics: list[str] = []
    if 'world_fact_updates' in result_data:
        from plugins.adventurer_s1.services.s1_world_outline_contract import (
            normalize_world_fact_updates,
        )
        normalized_facts, fact_diagnostics = normalize_world_fact_updates(
            result_data.get('world_fact_updates'),
        )
        result_data['world_fact_updates'] = normalized_facts
        auxiliary_diagnostics.extend(fact_diagnostics)
    if 'dm_handoff_note' in result_data:
        from plugins.adventurer_s1.services.s1_world_outline_contract import (
            normalize_dm_handoff_note,
        )
        normalized_note, note_diagnostics = normalize_dm_handoff_note(
            result_data.get('dm_handoff_note'),
        )
        result_data['dm_handoff_note'] = normalized_note
        auxiliary_diagnostics.extend(note_diagnostics)
    if 'location_state_changes' in result_data:
        from plugins.adventurer_s1.services.s1_world_location_state_contract import (
            normalize_location_state_changes,
        )
        max_changes = max(0, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_LOCATION_STATE_MAX_CHANGES', 4,
        ) or 4))
        normalized_changes, location_diagnostics = normalize_location_state_changes(
            result_data.get('location_state_changes'),
            max_changes=max_changes,
        )
        result_data['location_state_changes'] = normalized_changes
        location_state_diagnostics.extend(location_diagnostics)
    if 'dungeon_explorability_change' in result_data:
        from plugins.adventurer_s1.services.s1_world_location_state_contract import (
            normalize_dungeon_explorability_change,
        )
        normalized_explorability, explorability_diagnostics = (
            normalize_dungeon_explorability_change(
                result_data.get('dungeon_explorability_change'),
            )
        )
        result_data['dungeon_explorability_change'] = normalized_explorability
        location_state_diagnostics.extend(explorability_diagnostics)
    from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import (
        normalize_for_runtime as normalize_countermeasure_learning_for_runtime,
    )
    if countermeasure_learning_enabled is False:
        normalized_learning, learning_diagnostics = [], []
    else:
        normalized_learning, learning_diagnostics = (
            normalize_countermeasure_learning_for_runtime(
                result_data.get('countermeasure_learning', []),
                final_events=result_data.get('events'),
            )
        )
    result_data['countermeasure_learning'] = normalized_learning
    auxiliary_diagnostics.extend(
        f'countermeasure_learning{diagnostic}'
        for diagnostic in learning_diagnostics
    )

    try:
        parsed = S1ExpeditionResult.model_validate(result_data)
    except ValidationError as exc:
        errors = [_format_pydantic_error(e) for e in exc.errors()]
        return False, errors, []

    business_errors: list[str] = [
        f'公共世界辅助字段已局部丢弃: {diagnostic}'
        for diagnostic in auxiliary_diagnostics
    ]
    business_errors.extend(asset_update_errors)
    business_errors.extend(new_equipment_quantity_errors)
    business_errors.extend(gold_settlement_errors)
    business_errors.extend(commission_errors)
    business_errors.extend(
        f'地点状态变更字段非法: {diagnostic}'
        for diagnostic in location_state_diagnostics
    )

    for update in result_data.get('member_updates', []):
        score = update.get('score')
        if score is None:
            if require_member_scores:
                business_errors.append(
                    f"{_S1_MEMBER_SCORE_ERROR_MARKER}: 成员 {update.get('id')} 缺少 score；按本场表现填写 0–10 分"
                )
        elif not 0 <= float(score) <= 10:
            business_errors.append(
                f"{_S1_MEMBER_SCORE_ERROR_MARKER}: 成员 {update.get('id')} 的 score={score} 超出 0–10；须依据正文重新评分"
            )

    for index, event_text in enumerate(parsed.events):
        if not isinstance(event_text, str):
            continue
        if _EXPEDITION_EVENT_FIELDNAME_PREFIX_PATTERN.match(event_text):
            business_errors.append(
                f'events[{index}] 以字段名前缀（如 `summary:` / `总结:` / `**summary**:`）开头，'
                f'不允许把 JSON 字段名或 markdown 标题当作剧情正文写进 events'
            )
        pollution_reason = _story_asset_protocol_pollution_reason(
            event_text,
            member_context,
        )
        if pollution_reason:
            business_errors.append(
                f'{_S1_STORY_ASSET_ID_POLLUTION_MARKER}: '
                f'events[{index}] {pollution_reason}'
            )

    summary_pollution_reason = _story_asset_protocol_pollution_reason(
        parsed.summary,
        member_context,
    )
    if summary_pollution_reason:
        business_errors.append(
            f'{_S1_STORY_ASSET_ID_POLLUTION_MARKER}: '
            f'summary {summary_pollution_reason}'
        )

    # 业务校验 1：new_equipments[*].rarity 合法（仅当值为 dict 且声明了 rarity 时检查）
    for update in parsed.member_updates:
        if update.new_equipments:
            for name, data in update.new_equipments.items():
                if isinstance(data, dict):
                    rarity = data.get('rarity')
                    if rarity and rarity not in VALID_RARITIES:
                        business_errors.append(f'装备 {name} 的 rarity 非法: {rarity}')

    # 业务校验 1b：只拦截无需语义推断即可确认的协议污染。首次校验交给 observer 修复，
    # observer 后复校验仍存在则按 hard error 打回，保证审核故障时也不会污染玩家数据。
    for raw_update in result_data.get('member_updates', []):
        if not isinstance(raw_update, dict):
            continue
        for field_name, raw_name in _iter_new_visible_names(raw_update):
            reason = _visible_name_pollution_reason(raw_name)
            if reason:
                business_errors.append(
                    f'{_S1_VISIBLE_NAME_POLLUTION_MARKER}: {field_name} 中的「{raw_name}」{reason}'
                )

    # 业务校验 2：成员覆盖与唯一性已由 normalize_s1_member_updates_by_id 保证。

    # 业务校验 3：HP / 死亡一致性。remaining_hp 是最终剩余 HP，不允许为负；
    # 死亡/失踪必须由 remaining_hp=0 + is_dead=true + death_reason 共同成立。
    # 不从剧情正文做关键词猜测，避免误判；death_reason 是结构化、玩家可见的交代。
    for update in parsed.member_updates:
        hp_value = float(update.remaining_hp)
        if hp_value < 0:
            business_errors.append(
                f'成员 {update.id} remaining_hp 不能为负数；若死亡请返回 0 并设置 is_dead=true'
            )
            continue
        raw_update = next(
            (u for u in result_data.get('member_updates', [])
             if isinstance(u, dict) and str(u.get('id')) == str(update.id)),
            {},
        )
        business_errors.extend(_validate_member_owned_names(raw_update, member_context))
        is_dead = raw_update.get('is_dead') is True
        if hp_value <= 0 and not is_dead:
            business_errors.append(f'成员 {update.id} remaining_hp 为 0 时必须设置 is_dead=true')
            continue
        if is_dead and hp_value > 0:
            business_errors.append(f'成员 {update.id} is_dead=true 时 remaining_hp 必须为 0')
            continue
        if is_dead:
            death_reason = raw_update.get('death_reason')
            if not isinstance(death_reason, str) or not death_reason.strip():
                business_errors.append(f'成员 {update.id} 被标记死亡/失踪时必须提供 death_reason')

    # 业务校验 4：completion_check 只检查收束；成员覆盖与实际资产沿各自合同校验。
    # S1 允许故事与队伍离场已经收束，但核心威胁仍留存在世界中；因此 core_resolved
    # 必须是布尔值，却不要求为 true。story_finished / exit_resolved 仍须为 true。
    completion_check = _normalize_completion_check_payload(result_data.get('completion_check'))
    business_errors.extend(_validate_s1_completion_check(completion_check))

    for field_name in (
        HIDDEN_BOSS_COMPLETION_FIELD,
        LEGACY_HIDDEN_BOSS_COMPLETION_FIELD,
    ):
        completed_value = getattr(parsed, field_name, 0)
        if completed_value not in (0, 1):
            business_errors.append(f'{field_name} 必须是 0 或 1')

    # 正文长度只以玩家可见正文硬底线拦截明显残缺输出。
    # events≥10 与 events 总字符目标已取消；自然段下限只服务分批推送协议。
    if not skip_length_checks:
        try:
            hard_min_chars = max(0, int(current_app.config.get(
                'ADVENTURER_S1_EXPEDITION_STORY_HARD_MIN_CHARS', 1000,
            )))
        except (RuntimeError, TypeError, ValueError):
            # 无 app context（测试直接调用）时关闭校验
            hard_min_chars = 0
        if (
            raw_story_char_count is not None
            and hard_min_chars > 0
            and raw_story_char_count < hard_min_chars
        ):
            business_errors.append(
                f'{_S1_STORY_HARD_MIN_ERROR_MARKER}（有效字符 {raw_story_char_count}，'
                f'异常短底线 {hard_min_chars}）'
            )
    try:
        min_raw_paragraphs = max(0, int(current_app.config.get(
            'ADVENTURER_S1_EXPEDITION_RAW_PARAGRAPHS_MIN', 6,
        )))
    except (RuntimeError, TypeError, ValueError):
        min_raw_paragraphs = 0
    if (
        raw_paragraph_count is not None
        and min_raw_paragraphs > 0
        and raw_paragraph_count < min_raw_paragraphs
    ):
        business_errors.append(
            f'玩家可见剧情可推送段落过少（{raw_paragraph_count} 段），'
            f'硬下限为 {min_raw_paragraphs} 段'
        )

    # 分级：soft errors 不阻断主 AI，交给剧情审核 AI 补正
    hard_errors = [
        e for e in business_errors
        if _classify_s1_validation_error(
            e,
            allow_observer_equipment_repairs=allow_observer_equipment_repairs,
            allow_observer_death_reason_repairs=allow_observer_death_reason_repairs,
            allow_observer_gold_settlement_repairs=(
                allow_observer_gold_settlement_repairs
            ),
            allow_observer_commission_repairs=allow_observer_commission_repairs,
            allow_observer_score_repairs=allow_observer_score_repairs,
        ) == 'hard'
    ]
    soft_errors = [
        e for e in business_errors
        if _classify_s1_validation_error(
            e,
            allow_observer_equipment_repairs=allow_observer_equipment_repairs,
            allow_observer_death_reason_repairs=allow_observer_death_reason_repairs,
            allow_observer_gold_settlement_repairs=(
                allow_observer_gold_settlement_repairs
            ),
            allow_observer_commission_repairs=allow_observer_commission_repairs,
            allow_observer_score_repairs=allow_observer_score_repairs,
        ) == 'soft'
    ]
    return (len(hard_errors) == 0), hard_errors, soft_errors


def _build_s1_owned_whitelist_text(member_context: dict[int, dict] | None) -> str:
    """逐成员列出可用装备道具 / 可携带技能白名单（正向引导，不含任何被拒物品名）。"""
    if not member_context:
        return '（本轮未取得独立清单；权威数据为各成员信息块中的装备道具与技能。）'
    lines: list[str] = []
    for member_id, ctx in member_context.items():
        ctx = ctx or {}
        name = ctx.get('name') or f'#{member_id}'
        equipment = '、'.join(sorted(ctx.get('equipment_names') or set())) or '无'
        skills = '、'.join(sorted(ctx.get('skill_names') or set())) or '无'
        lines.append(
            f'- [{member_id}] {name}：可用装备道具=[{equipment}]；可携带技能=[{skills}]'
        )
    return '\n'.join(lines)


def build_s1_retry_validation_feedback(
    errors: list[str],
    member_context: dict[int, dict] | None = None,
) -> str:
    """把校验 errors 转成喂回主探险 AI 的重试反馈文本。

    归属类错误（成员引用了自己没有的装备/道具/技能）做**脱敏**：绝不把被拒的物品/技能名
    回显进提示词。否则等于在重试 prompt 里反复点名那件幻觉物品，低温采样下 AI 会因「否定式
    提示」（pink elephant 效应）反复复读同一个不存在的物品，导致整条探险重试耗尽后兜底。
    脱敏后只提供本轮随身白名单（来自后端权威 member_context），不重复 system prompt 中的
    输出协议、字段规则或历史兼容字段。
    资产引用、既有资产修改、特质依赖与正文编号污染错误同样只回传固定语义提示，
    恢复触发错误保留机器字段位置与对应条件，不回显原始资产身份或候选文本，
    不把后端已经还原的数据库 ID 或旧字段名重新注入模型。其余结构/数值类错误
    （rarity / 成员覆盖 / HP / completion_check 等）不含敏感名，照常回显。

    注意：日志侧仍打印含名的完整 errors 便于排查，本函数只影响 AI 看到的反馈。
    """
    ownership_hit = False
    story_hard_min_hit = False
    asset_protocol_hit = False
    recovery_errors: list[str] = []
    codename_anchor_hit = False
    other_errors: list[str] = []
    for err in errors or []:
        if (
            _S1_OWNED_EQUIPMENT_ERROR_MARKER in err
            or _S1_OWNED_SKILL_ERROR_MARKER in err
        ):
            ownership_hit = True
        elif _S1_STORY_HARD_MIN_ERROR_MARKER in err:
            story_hard_min_hit = True
        elif S1_MEMBER_CODENAME_ANCHOR_ERROR_MARKER in err:
            codename_anchor_hit = True
        elif S1_RECOVERY_TRIGGER_ERROR_MARKER in err:
            # These are validator diagnostics, not narrative classification.
            # Preserve the failed condition without reinjecting resolved DB IDs.
            location = re.search(
                r'member_updates\.\d+\.recovery_triggers(?:\.\d+)?', err,
            )
            prefix = (location.group(0) + '：') if location else 'recovery_triggers：'
            if '该资产没有可触发的 recovery_minutes' in err:
                detail = (
                    '该条资产缺少正整数 recovery_minutes。实际使用不等于启动恢复计时；'
                    '只为输入或本次合法资产更新已给出正整数恢复时长、'
                    '且本次 events 已记录发动并进入恢复期的资产提交触发。'
                    '未满足时省略该触发，保留实际使用事实与其它合法结算。'
                )
            elif '该资产出发时仍在恢复中，不能再次触发' in err:
                detail = '该资产出发时仍在恢复中；按本次可用状态校正发动事实和触发记录。'
            else:
                detail = (
                    '触发记录未满足恢复合同。按本成员输入的准确 asset_ref、'
                    'asset_kind 和实际发动的 evidence_event_indexes 记录；'
                    '同一资产最多一条，且须满足既定恢复时长及可用状态。'
                )
            feedback = prefix + detail
            if feedback not in recovery_errors:
                recovery_errors.append(feedback)
        elif (
            any(marker in err for marker in (
                _S1_ASSET_REFERENCE_ERROR_MARKER,
                _S1_STORY_ASSET_ID_POLLUTION_MARKER,
                _S1_EXISTING_ASSET_UPDATE_ERROR_MARKER,
                S1_RECOVERY_TRIGGER_ERROR_MARKER,
                TRAIT_ASSET_DEPENDENCY_ERROR_MARKER,
            ))
            or re.search(
                r'(?<![A-Za-z0-9_])(?:skill_id|equipment_id|asset_id)'
                r'(?![A-Za-z0-9_])',
                err,
            )
        ):
            asset_protocol_hit = True
        else:
            other_errors.append(err)

    segments: list[str] = ['上轮输出未通过后端校验，请根据以下本轮校验结果重新生成完整响应。']

    if ownership_hit:
        segments.append(
            '本轮归属校验发现部分成员引用的资源不在其权威可用清单中。'
            '请依据以下本轮清单校正对应的正文事实与结构化状态：'
        )
        segments.append(_build_s1_owned_whitelist_text(member_context))

    if story_hard_min_hit:
        segments.append(
            '上轮玩家可见正文尚未形成可发布的完整冒险叙事。'
            '请完整呈现起因、推进、决定性转折与代价、结果以及离场收束，'
            '并让这些内容在玩家可见 events 中成立。'
        )

    if codename_anchor_hit:
        segments.append(
            '上轮没有按本次成员信息中的完整代号建立身份锚点。'
            '请让 summary 或 events 中的成员指代保留本次提供的完整代号，'
            '每条非空 history_story_append 记录该条 id 对应成员的经历；'
            '其它成员称呼同样只使用本次提供的完整代号。'
        )

    if asset_protocol_hit:
        segments.append(
            '上轮结构化资产结算未按本次请求内引用合同闭合，或玩家可见正文出现了内部编号。'
            '请只把当前请求列出的 S/E 引用复制到对应结构化结算字段；'
            'events、summary、履历、名称和描述中不得出现请求引用、数据库 ID 或协议字段名。'
            '无法唯一定位原资产时，省略对应修改并保留其它合法结果。'
        )

    segments.extend(recovery_errors)

    if other_errors:
        segments.append('本轮其他校验结果：' + '；'.join(other_errors[:3]))

    return '\n'.join(segments)
