import json
import random
import re
import time
from copy import deepcopy
from dataclasses import dataclass
import requests
from flask import current_app, has_app_context

__all__ = [
    'ExpeditionMemberSnapshot',
    '_build_expedition_member_runtime_snapshots',
    '_strip_trailing_equipment_note',
    '_normalize_equipment_name_list_payload',
    '_normalize_used_equipments_payload',
    '_normalize_consumed_equipments_payload',
    '_coerce_equipment_detail_dict',
    'NEGATIVE_TRAIT_KEYWORDS',
    'POSITIVE_TRAIT_KEYWORDS',
    '_DEFAULT_HEALING_KEYWORDS',
    '_DEFAULT_RESCUE_KEYWORDS',
    '_extract_negative_trait_keywords',
    '_extract_positive_trait_keywords',
    '_MERGE_MAX_SOURCE_TRAITS',
    '_merged_trait_preserves_polarity_signals',
    '_get_prepare_ai_max_carry_items',
    '_is_healing_potion_item_name',
    '_find_first_available_prepare_reserved_item',
    '_ensure_prepared_member_keeps_reserved_item',
    '_ensure_prepared_member_keeps_healing_potion',
    '_build_member_prompt_context',
    '_build_prepare_context_raw_member_entry',
    '_build_prepare_context_raw_member_map',
    '_MAX_TRAIT_RECOVERY_PLANS_PER_MEMBER',
    '_VALID_TRAIT_RECOVERY_ACTIONS',
    '_sanitize_prepare_trait_recovery_plans',
    '_sanitize_prepare_equipment_upgrade_plans',
    '_get_prepare_context_value',
    '_compact_prepare_context_dict',
    '_sanitize_context_preparation_result',
    '_build_prepared_context_member_map',
    '_build_raw_member_info_text',
    '_build_expedition_member_prompt_inputs',
    '_merge_post_prepare_purchased_items',
    '_compose_member_info_for_expedition',
    '_ask_deepseek_prepare_expedition_context',
    '_build_special_event_prompt_context',
    '_build_special_event_warning_event',
    '_build_expedition_ai_system_prompt',
    '_build_expedition_danger_assessment',
    '_build_expedition_ai_user_prompt',
    '_build_pre_expedition_log_events',
    '_extract_cached_prepared_member_context',
    '_build_generating_log_result_summary',
    '_build_expedition_departure_msg',
    '_normalize_cached_shopping_purchased_items',
    '_flatten_shopping_events',
    '_build_pre_expedition_storage_events',
    '_build_pre_expedition_trait_recovery_events',
    '_build_pre_expedition_upgrade_events',
    '_build_generating_stage_log_payload',
    '_build_in_progress_expedition_log_payload',
    '_run_post_prepare_shopping_stage',
    '_persist_prepare_stage_checkpoint',
    '_request_prepare_expedition_context_with_model',
    '_build_prepare_ai_context_debug_summary',
    '_build_prepare_ai_model_comparison_summary',
    '_build_prepare_ai_single_model_log',
    '_load_json_summary_text',
]

from db_repository.manager import db_manager
from plugins.adventurer.models import (
    Adventurer,
    AdventurerParty,
)
from plugins.adventurer.utils import (
    sanitize_adventurer_name,
    split_adventurer_traits,
    split_name_desc_colon_suffix,
)
from plugins.adventurer.services.adventurer_config_service import (
    VALID_EQUIPMENT_RARITIES,
    _build_ai_output_preview,
    _build_ai_response_summary,
    _clean_compact_text,
    _clean_persisted_text,
    _coerce_config_bool,
    _coerce_int,
    _extract_ai_message_text,
    _extract_json_object_from_text,
    _get_adventurer_ai_request_timeout,
    _to_clean_string_list,
)




# ── 成员运行时快照 ──────────────────────────────────────────────────────────────

@dataclass
class ExpeditionMemberSnapshot:
    """切断 ORM session 关联后的成员运行时快照。"""
    id: int
    name: str
    gender: str
    character_class: str
    level: int
    status: str
    hp: int
    max_hp: int
    strength: int
    agility: int
    intelligence: int
    luck: int
    personality: str
    gold: float
    traits: list
    skills: list
    equipment: dict
    investors: dict
    warehouse: list
    exp: int
    history_story: str


def _build_expedition_member_runtime_snapshots(members: list[Adventurer]) -> list[ExpeditionMemberSnapshot]:
    """把 ORM 成员对象转换成只含运行时所需字段的纯快照。"""
    snapshots: list[ExpeditionMemberSnapshot] = []
    for member in members or []:
        member_id = _coerce_int(getattr(member, 'id', 0), 0)
        snapshots.append(ExpeditionMemberSnapshot(id=member_id, name=sanitize_adventurer_name(getattr(member, 'name', ''), fallback=f'成员{member_id}'), gender=str(getattr(member, 'gender', '') or ''), character_class=str(getattr(member, 'character_class', '') or ''), level=_coerce_int(getattr(member, 'level', 1), 1), status=str(getattr(member, 'status', '') or ''), hp=_coerce_int(getattr(member, 'hp', 0), 0), max_hp=_coerce_int(getattr(member, 'max_hp', 1), 1), strength=_coerce_int(getattr(member, 'strength', 0), 0), agility=_coerce_int(getattr(member, 'agility', 0), 0), intelligence=_coerce_int(getattr(member, 'intelligence', 0), 0), luck=_coerce_int(getattr(member, 'luck', 0), 0), personality=str(getattr(member, 'personality', '') or ''), gold=float(getattr(member, 'gold', 0) or 0), traits=deepcopy(getattr(member, 'traits', []) or []), skills=deepcopy(getattr(member, 'skills', []) or []), equipment=deepcopy(getattr(member, 'equipment', {}) or {}), investors=deepcopy(getattr(member, 'investors', {}) or {}), warehouse=deepcopy(getattr(member, 'warehouse', []) or []), exp=_coerce_int(getattr(member, 'exp', 0), 0), history_story=str(getattr(member, 'history_story', '') or '')))
    return snapshots


# ── 装备辅助 ──────────────────────────────────────────────────────────────────

# 复合装备名常见分隔符（探险升级/融合后 AI 可能只写其中一段）
_EQUIPMENT_NAME_SEGMENT_SPLIT_RE = re.compile(r'[?·•/／|+＋、；;]')
_EQUIPMENT_REFERENCE_MIN_SUBSTR_LEN = 3


def _split_equipment_alias_segments(name: str) -> list[str]:
    """把复合装备名拆成可独立引用的别名段。"""
    parts = [part.strip() for part in _EQUIPMENT_NAME_SEGMENT_SPLIT_RE.split(name or '')]
    return [part for part in parts if part]


def _resolve_equipment_reference_name(stripped_name: str, available_names: list[str]) -> str | None:
    """把 AI 写的装备引用尽量映射回成员真实装备名。

    顺序：精确 → 前缀 → 分段精确 → 唯一子串包含（防「护符」误匹配多物）。
    """
    if not stripped_name or not available_names:
        return None

    available_name_set = set(available_names)
    if stripped_name in available_name_set:
        return stripped_name

    prefix_match = None
    for candidate in available_names:
        if candidate.startswith(stripped_name) and len(candidate) > len(stripped_name):
            if prefix_match is None or len(candidate) < len(prefix_match):
                prefix_match = candidate
    if prefix_match:
        return prefix_match

    segment_matches: list[str] = []
    for candidate in available_names:
        if stripped_name in _split_equipment_alias_segments(candidate):
            segment_matches.append(candidate)
    if segment_matches:
        return min(segment_matches, key=len)

    if len(stripped_name) < _EQUIPMENT_REFERENCE_MIN_SUBSTR_LEN:
        return None

    substr_matches = [candidate for candidate in available_names if stripped_name in candidate]
    if len(substr_matches) == 1:
        return substr_matches[0]
    return None


def _strip_trailing_equipment_note(name: str) -> str:
    '""去掉装备名末尾的括号备注或冒号后说明，兼容 AI 返回"装备名（原因）"/"装备名: 描述"的写法，也处理截断导致的不完整括号。""'
    stripped_name = _clean_compact_text(name, 120)
    while stripped_name:
        normalized_name = re.sub(
            r'\s*[\(（\[【][^\(\)\[\]（）【】]{1,80}[\)）\]】]\s*$',
            '',
            stripped_name
        ).strip()
        if not normalized_name or normalized_name == stripped_name:
            break
        stripped_name = normalized_name
    if stripped_name:
        truncated_bracket = re.sub(
            r'\s*[\(（\[【][^\(\)\[\]（）【】]*$',
            '',
            stripped_name
        ).strip()
        if truncated_bracket and truncated_bracket != stripped_name:
            stripped_name = truncated_bracket
    if stripped_name:
        colon_base, _colon_suffix = split_name_desc_colon_suffix(stripped_name)
        if colon_base and colon_base != stripped_name:
            stripped_name = colon_base
    return stripped_name


def _normalize_equipment_name_list_payload(
    equipment_names: object,
    member_equipment: dict | None = None
) -> list[str]:
    '""兼容装备名列表里的括号备注，并尽量映射回成员真实装备名。""'
    if not isinstance(equipment_names, list):
        return []

    normalized_names = []
    available_names = list((member_equipment or {}).keys()) if isinstance(member_equipment, dict) else []
    available_name_set = set(available_names)

    for raw_value in equipment_names[:100]:
        raw_name = _clean_compact_text(raw_value, 120)
        if not raw_name:
            continue

        normalized_name = raw_name
        if raw_name not in available_name_set:
            stripped_name = _strip_trailing_equipment_note(raw_name)
            resolved_name = _resolve_equipment_reference_name(stripped_name, available_names)
            if resolved_name:
                normalized_name = resolved_name
            elif stripped_name:
                normalized_name = stripped_name

        if not normalized_name:
            continue
        normalized_names.append(normalized_name)

    return normalized_names


def _normalize_used_equipments_payload(
    used_equipments: object,
    member_equipment: dict | None = None
) -> list[str]:
    '""兼容 used_equipments 里的括号备注，并尽量映射回成员真实装备名。""'
    return _normalize_equipment_name_list_payload(used_equipments, member_equipment)


def _normalize_consumed_equipments_payload(
    consumed_equipments: object,
    member_equipment: dict | None = None
) -> list[str]:
    '""兼容 consumed_equipments 里的括号备注，并尽量映射回成员真实装备名。""'
    return _normalize_equipment_name_list_payload(consumed_equipments, member_equipment)


def _coerce_equipment_detail_dict(equipment_data: object) -> dict:
    '""把装备详情统一转成 dict，兼容历史字符串描述。""'
    if isinstance(equipment_data, dict):
        return dict(equipment_data)

    desc = _clean_compact_text(equipment_data, 240)
    return {'desc': desc} if desc else {}


# ── 特质合并保真校验 ────────────────────────────────────────────────────────────

NEGATIVE_TRAIT_KEYWORDS = (
    '脆弱', '幻痛', '反噬', '旧伤', '灼痛', '灼伤', '创伤', '诅咒', '失控', '后遗症',
    '代价', '透支', '衰老', '侵蚀', '腐化', '污染', '痛苦', '剧痛', '咳血', '骨折',
    '脱臼', '流血', '崩坏', '裂伤', '恶化', '濒死', '虚弱', '副作用', '迷失', '疯狂',
    '噩梦', '耗命', '伤口', '疼痛', '异化', '压迫', '抽搐',
)

POSITIVE_TRAIT_KEYWORDS = (
    # 增益 / 强化
    '增益', '增强', '加成', '提升', '强化', '精进', '额外', '提高', '提速',
    # 抗性 / 免疫
    '抗性', '减免', '免疫', '抵抗', '抗', '免疫',
    # 稳定 / 持久
    '稳定', '持久', '恒定', '坚定', '坚毅', '坚韧',
    # 心智 / 意志
    '沉稳', '专注', '清醒', '冷静', '果决', '果敢', '镇定', '无畏', '勇气',
    # 速度 / 灵巧
    '敏捷', '迅捷', '敏锐', '灵巧', '灵敏', '矫健',
    # 力量 / 战斗
    '勇猛', '威猛', '刚强', '强健', '凶悍', '锋锐',
    # 治疗 / 恢复
    '恢复', '治愈', '再生', '复原', '回复',
    # 感知 / 视野
    '夜视', '洞察', '侦察', '警觉', '直觉', '预感',
    # 幸运 / 奇迹
    '幸运', '吉运', '奇遇', '庇护', '加持',
)

_DEFAULT_HEALING_KEYWORDS = (
    '治疗药水', '高级治疗药水', '绷带', '急救', '圣水', '回复', '疗伤',
)

_DEFAULT_RESCUE_KEYWORDS = (
    '护符', '怀表', '回溯', '圣杯', '庇护', '护心', '替身', '逆转', '复生', '护命',
)


def _extract_negative_trait_keywords(text: str) -> set[str]:
    '""提取文本中的负面特质关键词。""'
    compact_text = _clean_compact_text(text, 400)
    return {keyword for keyword in NEGATIVE_TRAIT_KEYWORDS if keyword in compact_text}


def _extract_positive_trait_keywords(text: str) -> set[str]:
    '""提取文本中的正面特质关键词。""'
    compact_text = _clean_compact_text(text, 400)
    return {keyword for keyword in POSITIVE_TRAIT_KEYWORDS if keyword in compact_text}


# 单个合并方案允许的最大来源特质数。超过这个数量的"大批量合并"语义损失风险太大，直接拒绝。
_MERGE_MAX_SOURCE_TRAITS = 2


def _merged_trait_preserves_polarity_signals(
    source_traits: list[str],
    merged_trait_name: str,
    merged_trait_desc: str,
    raw_member: dict,
) -> bool:
    """对称极性兜底：源含正面/负面语义但合并后该极性的关键词彻底消失，则拒绝该合并。"""
    trait_desc_map = raw_member.get('trait_desc_map') or {}
    source_text = " ".join(
        f"{trait_name} {trait_desc_map.get(trait_name, '')}".strip()
        for trait_name in source_traits
    )
    merged_text = f"{merged_trait_name} {merged_trait_desc}"

    source_negative = _extract_negative_trait_keywords(source_text)
    if source_negative and not _extract_negative_trait_keywords(merged_text):
        return False

    source_positive = _extract_positive_trait_keywords(source_text)
    if source_positive and not _extract_positive_trait_keywords(merged_text):
        return False

    return True


# 旧名保留为兼容别名，避免外部 import 失败。
_merged_trait_preserves_negative_signals = _merged_trait_preserves_polarity_signals


# ── 治疗药水 / 保底随身携带 ─────────────────────────────────────────────────────

def _get_prepare_ai_max_carry_items(default: int = 20) -> int:
    '""获取前置整理器允许保留的最大随身物品数。""'
    from flask import has_app_context
    raw_value = default
    if has_app_context():
        raw_value = current_app.config.get('ADVENTURER_PREPARE_AI_MAX_CARRY_ITEMS', default)

    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return default


def _is_healing_potion_item_name(item_name: str) -> bool:
    '""判断物品名是否属于治疗药水类道具。""'
    return '治疗药水' in str(item_name or '').strip()


def _find_first_available_prepare_reserved_item(
    equipment_entries: list[dict],
    matcher,
) -> str | None:
    '""从成员现有装备中找出一个应保底随身携带的物品。""'
    locked_candidate = None
    for item in equipment_entries or []:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get('name') or '').strip()
        if not item_name or not matcher(item):
            continue

        if not bool(item.get('locked')):
            return item_name

        if locked_candidate is None:
            locked_candidate = item_name

    return locked_candidate


def _ensure_prepared_member_keeps_reserved_item(
    prepared_member: dict,
    raw_member: dict,
    *,
    item_matcher,
    carry_matcher,
) -> None:
    """成员原本拥有指定保底物品时，把它从 pre_expedition_store_items 里剔除（确保不被提前存仓）。"""
    store_items = list(prepared_member.get('pre_expedition_store_items') or [])
    if not store_items:
        return

    reserved_item_name = _find_first_available_prepare_reserved_item(
        raw_member.get('equipment_entries') or [],
        item_matcher,
    )
    if not reserved_item_name:
        return

    # 如果保底物品名出现在存仓清单里，剔除它（保留在背包里带下本）。
    # carry_matcher 参数保留是为了兼容旧调用签名，新语义下不再使用。
    if reserved_item_name in store_items:
        prepared_member['pre_expedition_store_items'] = [
            item_name for item_name in store_items if item_name != reserved_item_name
        ]


def _ensure_prepared_member_keeps_healing_potion(prepared_member: dict, raw_member: dict) -> None:
    """在成员原本拥有治疗药水时，确保它不被提前存仓。"""
    _ensure_prepared_member_keeps_reserved_item(
        prepared_member,
        raw_member,
        item_matcher=lambda item: _is_healing_potion_item_name(item.get('name')),
        carry_matcher=_is_healing_potion_item_name,
    )


# ── 成员上下文构建 ────────────────────────────────────────────────────────────

def _build_member_prompt_context(member: Adventurer, item_dict: dict, trait_dict: dict) -> dict:
    '""构建成员原始信息与供小 AI 整理的结构化上下文。""'
    member_name = sanitize_adventurer_name(member.name, fallback=f"成员{member.id}")
    trait_lines = []
    trait_entries = []
    trait_names = []
    visible_traits, _archived_traits = split_adventurer_traits(member.traits or [], trait_dict)
    for trait in visible_traits:
        trait_name = trait.get('name', '未知特质')
        trait_desc = trait.get('desc') or "未知效果的特质"

        trait_lines.append(f"{trait_name}({trait_desc})")
        trait_entries.append({
            'name': trait_name,
            'desc': trait_desc,
        })
        trait_names.append(trait_name)

    equip_lines = []
    equipment_entries = []
    equipment_names = []
    for eq_name, eq_data in (member.equipment or {}).items():
        if isinstance(eq_data, dict):
            desc = item_dict.get(eq_name, eq_data.get('desc', ''))
            uses_left = eq_data.get('uses_left')
            uses = f"剩余{uses_left}次" if uses_left is not None else "永久"
            locked = bool(eq_data.get('locked', False))
            equip_lines.append(f"{eq_name}({desc}, {uses})")
            equipment_entries.append({
                'name': eq_name,
                'type': eq_data.get('type', 'unknown'),
                'desc': desc,
                'uses_left': uses_left,
                'locked': locked,
            })
        else:
            desc = item_dict.get(eq_name, '')
            equip_lines.append(f"{eq_name}({desc})")
            equipment_entries.append({
                'name': eq_name,
                'type': 'unknown',
                'desc': desc,
                'uses_left': None,
                'locked': False,
            })
        equipment_names.append(eq_name)

    history_entries = []
    if member.history_story:
        history_list = [h.strip() for h in member.history_story.split('\n\n') if h.strip()]
        history_entries = history_list[-3:]
    history_str = "无" if not history_entries else " | ".join(history_entries)

    skills_lines = []
    skill_entries = []
    skill_names = []
    for skill in (member.skills or []):
        if not isinstance(skill, dict):
            continue
        skill_name = skill.get('name')
        if not skill_name:
            continue
        skill_desc = skill.get('desc', '')
        if skill_desc:
            skills_lines.append(f"{skill_name}({skill_desc})")
        else:
            skills_lines.append(skill_name)
        skill_entries.append({
            'name': skill_name,
            'desc': skill_desc,
        })
        skill_names.append(skill_name)

    traits_str = "无" if not trait_lines else ", ".join(trait_lines)
    equip_str = "无" if not equip_lines else ", ".join(equip_lines)
    skills_str = "无" if not skills_lines else ", ".join(skills_lines)

    raw_block = (
        f"- [{member.id}] {member_name} ({member.gender}, {member.character_class}, 等级{member.level}, "
        f"HP:{member.hp}/{member.max_hp}, 力量:{member.strength}, 敏捷:{member.agility}, "
        f"智力:{member.intelligence}, 幸运:{member.luck})\n"
        f"  性格: {member.personality}, 特质: {traits_str}, 技能: {skills_str}\n"
        f"  金币: {member.gold:.1f}, 装备道具: {equip_str}\n"
        f"  历史背景（之前发生的事情，仅供连续性参考，不是本次已发生事件）: {history_str}"
    )

    payload = {'id': member.id, 'name': member_name, 'gender': member.gender, 'character_class': member.character_class, 'level': member.level, 'hp': member.hp, 'max_hp': member.max_hp, 'strength': member.strength, 'agility': member.agility, 'intelligence': member.intelligence, 'luck': member.luck, 'personality': member.personality, 'gold': round(member.gold or 0, 1), 'historical_background': history_entries, 'traits': trait_entries, 'skills': skill_entries, 'equipment': equipment_entries, 'all_trait_names': trait_names, 'all_skill_names': skill_names, 'all_equipment_names': equipment_names}
    return {
        'raw_block': raw_block,
        'payload': payload,
    }


def _build_prepare_context_raw_member_entry(payload: dict) -> tuple[int, dict] | None:
    """把原始成员 payload 折叠成前置整理阶段使用的校验映射。"""
    if not isinstance(payload, dict):
        return None

    member_id = _coerce_int(payload.get('id'), -1)
    if member_id < 0:
        return None

    equipment_entries = list(payload.get('equipment') or [])
    return member_id, {
        'name': payload.get('name', ''),
        'traits': set(payload.get('all_trait_names') or []),
        'trait_desc_map': {
            str(trait.get('name')).strip(): _clean_persisted_text(trait.get('desc'))
            for trait in (payload.get('traits') or [])
            if isinstance(trait, dict) and str(trait.get('name') or '').strip()
        },
        'skills': set(payload.get('all_skill_names') or []),
        'equipment': set(payload.get('all_equipment_names') or []),
        'equipment_entries': equipment_entries,
        'storable_equipment': {
            item.get('name')
            for item in equipment_entries
            if isinstance(item, dict) and item.get('name') and not bool(item.get('locked'))
        },
    }


def _build_prepare_context_raw_member_map(raw_member_payloads: list[dict]) -> dict[int, dict]:
    """构建前置整理结果校验所需的成员索引。"""
    raw_member_map: dict[int, dict] = {}
    for payload in raw_member_payloads or []:
        member_entry = _build_prepare_context_raw_member_entry(payload)
        if member_entry is None:
            continue
        member_id, member_context = member_entry
        raw_member_map[member_id] = member_context
    return raw_member_map


_MAX_TRAIT_RECOVERY_PLANS_PER_MEMBER = 4
_VALID_TRAIT_RECOVERY_ACTIONS = {'remove', 'shorten'}


def _sanitize_prepare_trait_recovery_plans(
    member: dict,
    raw_member: dict,
    used_source_traits: set[str],
) -> list[dict]:
    """清洗前置整理器返回的静养修复计划，只保留可真实执行的条目。

    - source_trait 必须在成员当前 traits 中真实存在
    - 不能与 trait_merge_plans 的 source_traits 冲突
    - action 必须是 remove / shorten
    - shorten 必须提供 recovered_desc，且不能为空
    - 同一 source_trait 在本次方案里只处理一次
    - 一名成员最多 _MAX_TRAIT_RECOVERY_PLANS_PER_MEMBER 条
    """
    raw_traits = raw_member.get('traits') or set()
    if isinstance(raw_traits, (list, tuple)):
        raw_traits = set(raw_traits)
    if not raw_traits:
        return []

    sanitized_plans = []
    seen_sources = set()

    raw_plans = _get_prepare_context_value(member, 'trait_recovery_plans', 'tr') or []
    if not isinstance(raw_plans, list):
        return []

    for plan in raw_plans:
        if not isinstance(plan, dict):
            continue

        source_trait = _clean_compact_text(
            _get_prepare_context_value(plan, 'source_trait', 'src'),
            120,
        )
        if (
            not source_trait
            or source_trait not in raw_traits
            or source_trait in used_source_traits
            or source_trait in seen_sources
        ):
            continue

        action = _clean_compact_text(
            _get_prepare_context_value(plan, 'action', 'act'),
            20,
        ).lower()
        if action not in _VALID_TRAIT_RECOVERY_ACTIONS:
            continue

        recovered_desc = ''
        if action == 'shorten':
            recovered_desc = _clean_persisted_text(
                _get_prepare_context_value(plan, 'recovered_desc', 'd')
            )
            if not recovered_desc:
                continue

        sanitized_plans.append({
            'source_trait': source_trait,
            'action': action,
            'recovered_desc': recovered_desc,
        })
        seen_sources.add(source_trait)

        if len(sanitized_plans) >= _MAX_TRAIT_RECOVERY_PLANS_PER_MEMBER:
            break

    return sanitized_plans


def _sanitize_prepare_equipment_upgrade_plans(member: dict, raw_member: dict) -> list[dict]:
    """清洗前置整理器返回的装备升级计划，只保留可真实执行的计划。"""
    equipment_map = {
        str(item.get('name')).strip(): dict(item)
        for item in (raw_member.get('equipment_entries') or [])
        if isinstance(item, dict) and str(item.get('name') or '').strip()
    }
    if not equipment_map:
        return []

    sanitized_plans = []
    consumed_item_names = set()
    for plan in (_get_prepare_context_value(member, 'equipment_upgrade_plans', 'eu') or []):
        if not isinstance(plan, dict):
            continue

        base_equipment = _clean_compact_text(
            _get_prepare_context_value(plan, 'base_equipment', 'b'),
            120,
        )
        if (
            not base_equipment
            or base_equipment in consumed_item_names
            or base_equipment not in equipment_map
        ):
            continue

        material_items = [
            item_name for item_name in _to_clean_string_list(
                _get_prepare_context_value(plan, 'material_items', 'materials', 'm'),
                6,
                120,
            )
            if item_name != base_equipment and item_name not in consumed_item_names and item_name in equipment_map
        ]
        if not material_items:
            continue

        upgraded_equipment = _get_prepare_context_value(plan, 'upgraded_equipment', 'u') or {}
        if not isinstance(upgraded_equipment, dict):
            continue

        upgraded_name = _clean_compact_text(
            _get_prepare_context_value(upgraded_equipment, 'name', 'n'),
            120,
        )
        upgraded_desc = _clean_persisted_text(
            _get_prepare_context_value(upgraded_equipment, 'desc', 'd')
        )
        upgraded_type = _clean_compact_text(
            _get_prepare_context_value(upgraded_equipment, 'type', 't'),
            40,
        )
        upgraded_rarity = _clean_compact_text(
            _get_prepare_context_value(upgraded_equipment, 'rarity', 'r'),
            20,
        ).lower()
        if (
            not upgraded_name
            or not upgraded_desc
            or not upgraded_rarity
            or upgraded_rarity not in VALID_EQUIPMENT_RARITIES
        ):
            continue

        surviving_names = set(equipment_map.keys()) - {base_equipment, *material_items}
        if upgraded_name in surviving_names:
            continue

        base_item_data = equipment_map.get(base_equipment) or {}
        sanitized_plans.append({
            'base_equipment': base_equipment,
            'material_items': material_items,
            'upgraded_equipment': {
                'name': upgraded_name,
                'type': upgraded_type or str(base_item_data.get('type') or 'unknown'),
                'desc': upgraded_desc,
                'rarity': upgraded_rarity,
            }
        })
        consumed_item_names.add(base_equipment)
        consumed_item_names.update(material_items)

    return sanitized_plans


def _get_prepare_context_value(payload: dict, *keys: str):
    """按顺序读取前置整理结果字段，兼容短键与旧长键。"""
    if not isinstance(payload, dict):
        return None

    for key in keys:
        if key in payload:
            return payload.get(key)
    return None


def _compact_prepare_context_dict(payload: dict, *, preserve_keys: set[str] | None = None) -> dict:
    """移除前置整理结果中的空字段，减少无意义体积。"""
    preserve_keys = preserve_keys or set()
    compacted = {}
    for key, value in (payload or {}).items():
        if key in preserve_keys:
            compacted[key] = value
            continue

        if isinstance(value, str):
            if value.strip():
                compacted[key] = value
            continue

        if isinstance(value, (list, dict)) and not value:
            continue

        if value is None:
            continue

        compacted[key] = value
    return compacted


def _sanitize_context_preparation_result(prepared_context: dict, raw_member_payloads: list[dict]) -> dict | None:
    '""过滤小 AI 整理结果，只保留真实存在的成员与条目。""'
    if not isinstance(prepared_context, dict):
        return None

    raw_member_map = _build_prepare_context_raw_member_map(raw_member_payloads)

    sanitized_members = []
    for member in (_get_prepare_context_value(prepared_context, 'members', 'm') or []):
        if not isinstance(member, dict):
            continue

        member_id = _coerce_int(member.get('id'), -1)
        raw_member = raw_member_map.get(member_id)
        if raw_member is None:
            continue

        carry_item_limit = max(len(raw_member['equipment_entries']), _get_prepare_ai_max_carry_items())
        store_item_limit = max(len(raw_member['storable_equipment']), carry_item_limit)
        pre_expedition_store_items = [
            item_name for item_name in _to_clean_string_list(
                _get_prepare_context_value(member, 'pre_expedition_store_items', 'st')
                or member.get('warehouse_candidates'),
                store_item_limit
            )
            if item_name in raw_member['storable_equipment']
        ]

        trait_merge_plans = []
        equipment_upgrade_plans = _sanitize_prepare_equipment_upgrade_plans(member, raw_member)
        upgrade_consumed_names = {
            plan['base_equipment']
            for plan in equipment_upgrade_plans
        }
        for plan in equipment_upgrade_plans:
            upgrade_consumed_names.update(plan.get('material_items') or [])
        used_source_traits = set()
        for merge_plan in (_get_prepare_context_value(member, 'trait_merge_plans', 'tm') or []):
            if not isinstance(merge_plan, dict):
                continue

            source_traits = [
                trait_name for trait_name in _to_clean_string_list(
                    _get_prepare_context_value(merge_plan, 'source_traits', 'src'),
                    6,
                )
                if trait_name in raw_member['traits'] and trait_name not in used_source_traits
            ]
            if len(source_traits) < 2:
                continue
            # 源数量硬上限：一次合并最多 2 条源，超出说明 AI 在大批量合并，语义损失风险太大。
            if len(source_traits) > _MERGE_MAX_SOURCE_TRAITS:
                continue

            merged_trait = _get_prepare_context_value(merge_plan, 'merged_trait', 'mt') or {}
            if not isinstance(merged_trait, dict):
                continue

            merged_trait_name = _clean_compact_text(
                _get_prepare_context_value(merged_trait, 'name', 'n'),
                60,
            )
            merged_trait_desc = _clean_persisted_text(
                _get_prepare_context_value(merged_trait, 'desc', 'd')
            )
            if not merged_trait_name or not merged_trait_desc:
                continue
            if not _merged_trait_preserves_polarity_signals(
                source_traits,
                merged_trait_name,
                merged_trait_desc,
                raw_member,
            ):
                continue

            used_source_traits.update(source_traits)
            trait_merge_plans.append({
                'source_traits': source_traits,
                'merged_trait': {
                    'name': merged_trait_name,
                    'desc': merged_trait_desc,
                }
            })

        trait_recovery_plans = _sanitize_prepare_trait_recovery_plans(
            member, raw_member, used_source_traits
        )

        if upgrade_consumed_names:
            # 升级吃掉的原装备/材料,从存仓清单里去掉(它们已经被升级流程消耗了)
            pre_expedition_store_items = [
                item_name for item_name in pre_expedition_store_items
                if item_name not in upgrade_consumed_names
            ]

        sanitized_member = {
            'id': member_id,
            'name': raw_member['name'],
            'historical_focus': _clean_compact_text(
                _get_prepare_context_value(member, 'historical_focus', 'hf'),
                180,
            ),
            'trait_focus_summary': _clean_compact_text(
                _get_prepare_context_value(member, 'trait_focus_summary', 'tf'),
                180,
            ),
            'trait_merge_plans': trait_merge_plans,
            'trait_recovery_plans': trait_recovery_plans,
            'equipment_upgrade_plans': equipment_upgrade_plans,
            'low_relevance_summary': _clean_compact_text(
                _get_prepare_context_value(member, 'low_relevance_summary', 'lr'),
                180,
            ),
            'pre_expedition_store_items': pre_expedition_store_items,
            'main_risks_for_this_dungeon': _to_clean_string_list(
                _get_prepare_context_value(member, 'main_risks_for_this_dungeon', 'rk'),
                4,
                100,
            ),
        }
        _ensure_prepared_member_keeps_healing_potion(sanitized_member, raw_member)
        sanitized_members.append(_compact_prepare_context_dict(sanitized_member, preserve_keys={'id', 'name'}))

    party_summary = _clean_compact_text(_get_prepare_context_value(prepared_context, 'party_summary', 'ps'), 240)
    if not sanitized_members and not party_summary:
        return None

    return _compact_prepare_context_dict({
        'party_summary': party_summary,
        'members': sanitized_members,
    })


def _build_prepared_context_member_map(prepared_context: dict | None) -> dict[int, dict]:
    '""把前置整理结果按成员 ID 建立索引。""'
    if not isinstance(prepared_context, dict):
        return {}

    member_map = {}
    for member in prepared_context.get('members') or []:
        if not isinstance(member, dict):
            continue
        member_id = _coerce_int(member.get('id'), -1)
        if member_id < 0:
            continue
        member_map[member_id] = member
    return member_map


# ── 探险 prompt 组装 ──────────────────────────────────────────────────────────

def _build_raw_member_info_text(raw_member_blocks: list[str]) -> str:
    '""构建原始完整成员信息文本。""'
    return (
        "【原始完整成员信息（权威数据，历史背景都是之前发生的事情，不是本次已发生事件）】\n"
        + "\n".join(raw_member_blocks)
    )


def _build_expedition_member_prompt_inputs(members: list[Adventurer]) -> tuple[list[str], list[dict]]:
    '""构建探险提示词所需的原始成员文本与结构化 payload。""'
    from utils.tavern_cache import get_cached_item_dict, get_cached_trait_dict

    item_dict = get_cached_item_dict()
    trait_dict = get_cached_trait_dict()
    raw_member_blocks = []
    raw_member_payloads = []
    for member in members:
        member_context = _build_member_prompt_context(member, item_dict, trait_dict)
        raw_member_blocks.append(member_context['raw_block'])
        raw_member_payloads.append(member_context['payload'])
    return raw_member_blocks, raw_member_payloads


def _merge_post_prepare_purchased_items(
    prepared_context: dict | None,
    purchased_items_by_member: dict[int, list[str]] | None
) -> dict | None:
    '""把前置整理完成后新买的物品并入实际带下本清单，避免刚买就被视为已存仓。""'
    if not isinstance(prepared_context, dict):
        return prepared_context

    purchased_items_by_member = purchased_items_by_member or {}
    if not purchased_items_by_member:
        return prepared_context

    for member in prepared_context.get('members') or []:
        if not isinstance(member, dict):
            continue
        member_id = _coerce_int(member.get('id'), -1)
        if member_id < 0:
            continue
        purchased_items = [
            item_name for item_name in purchased_items_by_member.get(member_id, [])
            if item_name
        ]
        if not purchased_items:
            continue

        carry_items = list(member.get('core_items_to_carry') or [])
        store_items = list(member.get('pre_expedition_store_items') or [])
        for item_name in purchased_items:
            if item_name not in carry_items:
                carry_items.append(item_name)
            store_items = [stored_name for stored_name in store_items if stored_name != item_name]
        member['core_items_to_carry'] = carry_items
        member['pre_expedition_store_items'] = store_items
        compact_member = _compact_prepare_context_dict(member, preserve_keys={'id', 'name'})
        member.clear()
        member.update(compact_member)

    return prepared_context


def _compose_member_info_for_expedition(
    raw_member_blocks: list[str],
    raw_member_payloads: list[dict],
    prepared_context: dict | None
) -> str:
    """合成给主 AI 的成员信息：后端始终展示完整权威数据，小 AI 的输出只作 highlight 叠加，不会替换原始数据。"""
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    party_summary = prepared_context.get('party_summary') if isinstance(prepared_context, dict) else None

    lines = [
        "【小队成员信息（完整数据，历史背景都是之前发生的事情，不是本次已发生事件）】",
        "下面每位成员的特质 / 技能 / 装备 / 历史背景都是后端给出的权威完整数据。",
        "若同名成员段落下方出现【前置整理器提示】，那只是辅助参考（重点 highlight、存仓决策、风险提示、可选合并方案），它不会删除或替换上面的完整数据。",
    ]
    if party_summary:
        lines.append(f"小队整体重点（前置整理器）: {party_summary}")
    lines.append("")

    for payload in raw_member_payloads:
        member_id = _coerce_int(payload.get('id'), -1)
        if member_id < 0:
            continue

        prepared = prepared_member_map.get(member_id) or {}
        store_items = [str(name).strip() for name in (prepared.get('pre_expedition_store_items') or []) if str(name).strip()]
        store_items_set = set(store_items)

        lines.append(
            f"- [{member_id}] {payload.get('name')} ({payload.get('gender')}, {payload.get('character_class')}, "
            f"等级{payload.get('level')}, HP:{payload.get('hp')}/{payload.get('max_hp')}, 力量:{payload.get('strength')}, "
            f"敏捷:{payload.get('agility')}, 智力:{payload.get('intelligence')}, 幸运:{payload.get('luck')})"
        )
        lines.append(f"  性格: {payload.get('personality')}")

        # 全部特质（权威完整数据）
        trait_entries = [t for t in (payload.get('traits') or []) if isinstance(t, dict) and t.get('name')]
        if trait_entries:
            traits_str = ", ".join(f"{t['name']}({t.get('desc', '')})" for t in trait_entries)
        else:
            traits_str = "无"
        lines.append(f"  全部特质: {traits_str}")

        # 全部技能（权威完整数据）
        skill_entries = [s for s in (payload.get('skills') or []) if isinstance(s, dict) and s.get('name')]
        if skill_entries:
            skills_str = ", ".join(
                f"{s['name']}({s['desc']})" if s.get('desc') else s['name']
                for s in skill_entries
            )
        else:
            skills_str = "无"
        lines.append(f"  全部技能: {skills_str}")

        # 金币
        gold_value = payload.get('gold')
        if isinstance(gold_value, (int, float)):
            lines.append(f"  金币: {gold_value:.1f}")

        # 本次实际带下本物品 = 原始 equipment - pre_expedition_store_items
        equipment_entries = [eq for eq in (payload.get('equipment') or []) if isinstance(eq, dict) and eq.get('name')]
        carry_lines = []
        store_lines = []
        for eq in equipment_entries:
            eq_name = eq.get('name', '')
            uses_left = eq.get('uses_left')
            uses_text = f"剩余{uses_left}次" if uses_left is not None else "永久"
            entry_text = f"{eq_name}({eq.get('desc', '')}, {uses_text})"
            if eq_name in store_items_set:
                store_lines.append(entry_text)
            else:
                carry_lines.append(entry_text)
        lines.append(f"  本次实际带下本物品: {', '.join(carry_lines) if carry_lines else '无'}")
        if store_lines:
            lines.append(f"  出发前已先存仓、不带下本: {', '.join(store_lines)}")
        else:
            # store_items 引用了 equipment 里没有的名字（小 AI 写错），仍按原文展示以便排查
            leftover_store_names = [name for name in store_items if name not in {eq.get('name') for eq in equipment_entries}]
            if leftover_store_names:
                lines.append(f"  出发前已先存仓、不带下本: {', '.join(leftover_store_names)}")

        # 历史背景（权威完整数据）
        history_entries = payload.get('historical_background') or []
        if isinstance(history_entries, list) and history_entries:
            history_str = " | ".join(str(h) for h in history_entries if str(h).strip())
        else:
            history_str = "无"
        lines.append(f"  历史背景（之前发生的事情，仅供连续性参考）: {history_str}")


        # 前置整理器提示部分（仅 highlight + 辅助建议，不替换上面完整数据）
        if prepared:
            hint_lines = []
            if prepared.get('historical_focus'):
                hint_lines.append(f"    历史背景重点: {prepared['historical_focus']}")
            if prepared.get('trait_focus_summary'):
                hint_lines.append(f"    特质压缩摘要: {prepared['trait_focus_summary']}")

            if prepared.get('main_risks_for_this_dungeon'):
                hint_lines.append(f"    本次主要风险: {', '.join(prepared['main_risks_for_this_dungeon'])}")

            for plan in (prepared.get('trait_merge_plans') or []):
                if not isinstance(plan, dict):
                    continue
                source_traits = plan.get('source_traits') or []
                merged_trait = plan.get('merged_trait') or {}
                if not isinstance(merged_trait, dict):
                    continue
                merged_name = merged_trait.get('name')
                merged_desc = merged_trait.get('desc')
                if source_traits and merged_name:
                    src_text = ' + '.join(source_traits)
                    desc_suffix = f"({merged_desc})" if merged_desc else ''
                    hint_lines.append(
                        f"    建议特质合并: {src_text} → {merged_name}{desc_suffix}"
                    )

            for plan in (prepared.get('equipment_upgrade_plans') or []):
                if not isinstance(plan, dict):
                    continue
                base_equipment = str(plan.get('base_equipment') or '').strip()
                material_items = [
                    str(item_name).strip()
                    for item_name in (plan.get('material_items') or [])
                    if str(item_name).strip()
                ]
                upgraded_equipment = plan.get('upgraded_equipment') or {}
                upgraded_name = ''
                upgraded_desc = ''
                if isinstance(upgraded_equipment, dict):
                    upgraded_name = str(upgraded_equipment.get('name') or '').strip()
                    upgraded_desc = str(upgraded_equipment.get('desc') or '').strip()
                if base_equipment and material_items and upgraded_name:
                    material_text = ' + '.join(material_items)
                    desc_suffix = f"({upgraded_desc})" if upgraded_desc else ''
                    hint_lines.append(
                        f"    建议整理时先升级: {base_equipment} + {material_text} → {upgraded_name}{desc_suffix}"
                    )

            if hint_lines:
                lines.append("  ── 前置整理器提示（仅 highlight 与建议，不替换上方完整数据） ──")
                lines.extend(hint_lines)

        lines.append("")  # 成员间空行分隔

    return "\n".join(lines).rstrip()


# ── 前置小 AI 请求 ────────────────────────────────────────────────────────────

def _ask_deepseek_prepare_expedition_context(
    api_key: str,
    api_base: str,
    target_dungeon: str,
    dungeon_level_desc: str,
    raw_member_payloads: list[dict],
    extra_context_hint: str | None = None,
    request_timeout_override: int | None = None,
) -> dict | None:
    '""调用前置小 AI，对成员信息做保守整理，减轻主 AI 注意力压力。extra_context_hint：可选，调用方追加的特殊上下文（如闭馆模式『最后一战放宽治愈/升级』），原样追加到 system prompt 末尾，不会覆盖原有规则；普通探险传 None 不受影响。""'
    if not api_key or not api_base or not raw_member_payloads:
        return None

    carry_item_limit = _get_prepare_ai_max_carry_items()
    system_prompt = f"""
你是一个"探险输入整理器"，只负责在探险开始前整理上下文，绝对不要生成剧情、绝对不要决定最终结算。你的工作是让主 AI 看到的输入更短、更聚焦、只突出真正需要决策或提醒的部分；当你拿不准时，主 AI 仍能从后端权威的完整列表里看到原始数据，所以宁可少做也不要乱做。

【事实边界原则】（最高优先，覆盖以下所有任务）
你只能：归纳 / 压缩 / 取舍 / 重组 输入里已经存在的信息。
绝对禁止：
- 发明不存在的物品 / 技能 / 特质 / 经历
- 补全未提供的设定或隐藏能力
- 改写原始含义、洗掉负面、洗掉正面
所有具体任务（存仓 / 合并 / 升级 / 归纳 / 风险评估）都必须严格落在输入已有项上。

【数据流定位】
- 成员的完整背包、完整特质、完整技能、完整历史背景由**后端权威发给主 AI**，不依赖你返回。
- 你的职责只是产出"变化与决策"：归纳（`historical_focus` / `trait_focus_summary` / `low_relevance_summary` / `party_summary`）、决策（`pre_expedition_store_items` 存仓 / `trait_merge_plans` 合并 / `equipment_upgrade_plans` 升级）、评估（`main_risks_for_this_dungeon`）。
- **不要重复列出原始数据**——主 AI 看得到完整列表。
- `historical_background` 全部都是之前发生的事情，只能作为连续性背景，绝不是本次已发生事件。

【任务优先级】
首要任务（必须做）：
1. 存仓降噪 → `pre_expedition_store_items`
2. 风险评估 → `main_risks_for_this_dungeon`
可选任务（只有在高度确信、收益明显大于误判风险时才做）：
3. 特质合并 → `trait_merge_plans`
4. 装备升级 → `equipment_upgrade_plans`
5. 静养修复 → `trait_recovery_plans`（把两次副本之间默认经过的静养时间反映到旧负面特质上）
拿不准时优先保持原样，宁可少做不要乱合并、乱升级、乱修复。

【输出节俭原则】
- 只返回相对原始输入真正有变化、被取舍、被压缩或确实值得提醒主 AI 的那部分；没变化、没取舍、没新增重点的字段尽量省略。
- `members` 里不必把每个成员都写满，也不必把每个字段都返回一遍。若某名成员这次没有需要额外提醒主 AI 的内容，就只写 `id`，甚至可以不返回该成员。
- 输出必须是一个合法 JSON 对象，不要 markdown，不要解释。

【高危特质提醒】
如果某名成员已经带着 `【临界】` 告警类负面特质，或某条高危负面特质的描述已经明显表达出"再继续使用对应力量/装备/能力就会爆发"，请在 `trait_focus_summary` 或 `main_risks_for_this_dungeon` 里点明：
这不是普通背景负面，而是这次必须慎重权衡要不要继续动用的高风险开关。

──────────────────────────────────────────
【存仓判断原则】（`pre_expedition_store_items`）

**核心目标**：减少主 AI 看到的物品噪音 + 在物品过多（> {carry_item_limit} 件）时**必须**减负到合理量级。

**存仓两轮**：
① 第一轮（永远要做）：清掉明显**职业不匹配 / 纯收藏摆设 / 重复弱化 / 明显无用**的旧物。例如盗贼背着重盾、法师背着粗制铁剑、已有更高档治疗药水时背着低级草药、装饰用首饰等。
② 第二轮（**当带下本物品仍 > {carry_item_limit} 件时强制启动**）：把"功能上没那么必要、还在背包里占位"的旧物按下面优先级继续存仓，直到带下本物品**不超过 {carry_item_limit} 件**为止。这是**硬要求**，不是可选——超出 {carry_item_limit} 件还不存仓会让主 AI 注意力被淹没，必须减负。

**第二轮存仓优先级（从高到低，先存上面的）**：
- 同效装备的次优版本（已经带着更高稀有度/更对口/已升级的同类时，把旧版本存仓）
- 已经过时的低稀有度装备（已有同类高稀有度时存掉低稀有度）
- 当前地牢主题用不上的元素属性装备（如纯火属性进冰本、纯光明属性进深渊本）
- 多余的非核心消耗品（保留 2-3 件主力，其余存仓）
- 暂时用不上的素材/材料/特殊道具（除非本次明显能用到）

**少存原则的边界**：当第一轮筛过之后，成员实际要带下本的物品**已经 ≤ {carry_item_limit} 件**，并且留下的大多是本职业常用、当前地牢用得上的合理备用件——这种情况下允许少存甚至不存，不必凑存仓数量。
**但是**：一旦实际带下本物品仍 > {carry_item_limit} 件，**不允许选择"不存"**，必须按第二轮优先级继续存仓直到 ≤ {carry_item_limit} 件。

**自检**：在你最终输出 `pre_expedition_store_items` 之前，无声计算一下：该成员带下本物品数 = 原始 equipment 数 - 锁定不可存的件数 - `pre_expedition_store_items` 的件数。如果这个数仍 > {carry_item_limit}，回头继续追加 `pre_expedition_store_items` 直到 ≤ {carry_item_limit}。

**硬白名单（绝对不能进 `pre_expedition_store_items`，即使为了凑到 {carry_item_limit} 件也不行）**：
① 已锁定（`locked=true`）的物品；
② 保命底牌（名字或描述体现保命/免死/替死/续命/复活/复生/逃脱/脱身的道具，若成员原本拥有，至少保留一件）；
③ 治疗药水（若成员原本拥有可带下本的治疗药水，至少保留一瓶；如果根本没有就不要虚构）。
即使你判断它们"看起来不像本职业常用"，也不要把它们放进存仓。如果白名单豁免后还是无法降到 {carry_item_limit} 件以内，那就在白名单之外尽量降低，能降多少降多少。

──────────────────────────────────────────
【装备升级判断原则】（`equipment_upgrade_plans`，可选）

**升级必须满足**：
- 原装备真实存在于该成员当前背包（`base_equipment`）
- 材料真实存在于同一成员背包、且会被一起消耗（`material_items`）——不要把没带在身上的东西、仓库外设想中的材料，或队友身上的道具拿来凑升级
- 继承关系明确——能清楚说出"原装备继承了什么 + 材料又带来了什么增强"
- 升级方向 + 升级收益清晰
- `upgraded_equipment.desc` 必须先保留原装备描述里的核心效果或核心含义，再在后面追加这次强化、附魔、重铸、改造后新增的增强、材质变化、限制或代价；不要把旧描述整段丢掉重写

**默认保守**：升级不是默认行为，只有升级收益明显大于"保持原样"的风险时才升级。具体表现：
- 但“可以升级”绝不等于“必须升级”
- 只要保持原样比强行升级更稳妥、更自然、更不容易误判，就直接保持原样
- 不是看到材料齐了就必须强行升级

**同名识别**：如果同一成员身上同时出现明显属于"升级后新件 + 旧原型件"的两把装备，而且名称或描述里已经有很明确的继承证据，请把它们当成同一件装备的前后形态：优先保留升级后的那一件，不要继续把原型件当成另一把独立可用武器。

──────────────────────────────────────────
【特质合并判断原则】（`trait_merge_plans`）

**最高优先：拿不准就不合并**。合并出错会永久丢失原特质语义，代价比"没合并"大得多。所以：只有**所有"合并必须满足"条件都成立、并且不触发任何"不要合并"情况**时，才能合并；任何不确定、任何边缘判断都应当直接保留原特质。

但只要**确实出现了**满足全部条件的多条同源旧特质，**就应当**把它们合并写进 `trait_merge_plans`，不要因为"反正主 AI 看得见完整列表"或"少做点保险"就跳过明显应当合并的情况——明显该合并的不合并，会让主 AI 长期被冗余特质淹没。换句话说：能合的要合，不能合的绝不强合。

**合并必须满足**（任意一条不满足就不要合并）：
- 多条旧特质语义**高度重叠**、重复表达较多
- 这几条特质**同系列、同来源链**：来自同一装备/道具/仪式/力量延伸出来，或明显属于同一后遗症/同一印记/同一诅咒不同阶段表达
- 一次最多 2 条源；每个旧特质最多参与一次合并

**典型应合并场景（这些情况下不要因为"保险起见"而跳过）**：
- **同一伤情的多阶段表达**（玩家高频反馈的踩坑模式）：如『骨裂』+『恶化的骨裂』、『骨裂』+『缓解了的骨裂』、『火灼伤』+『火灼伤·感染』。这些是历史上主探险 AI 没有按『变化的统一处理』正确移除前一阶段累积下来的 bug，必须主动合并成最准确反映**当前真实状态**的那一条。
- 同一旧伤在同一人身上反复叠加出 2 条相近表述（如『火灼旧伤』+『火焰幻痛』、『骨裂』+『骨裂阴影』）
- 同一禁术 / 同一神器 / 同一仪式连续触发后挂出的 2 条印记类特质（如某禁咒的『血脉灼烧』+『血脉残余』）
- 同一诅咒/污染在同一人身上不同阶段的 2 条表达（如『暗影渗透』+『暗影侵蚀』、『深渊低语·初触』+『深渊低语·渗入』）
- 同一段历史经历分别留下的 2 条相近后遗症（如某场事故的『左膝旧伤』+『左膝积液』）

**不要合并的情况（任意一条命中就保留原特质，不合并）**：
- 拿不准两个特质是否真的同源 → 保留原特质，不合并
- 虽然都和同一元素相关，但来自不同装备/不同力量/不同代价链路 → 不合并
- 只是名字看起来有点像，或者只是都偏正面、都偏负面、都和同一元素有关，没有共同来源链 → 不合并
- 跨越正负极性（一条偏正面 + 一条偏负面）→ 不合并
- 来源链路只是"猜想可能同源"而没有正文/描述明确证据 → 不合并
**宁可不合并也不要强行合并**：错误合并会永久丢失语义，代价远大于"该合的没合"。

**语义保留**：
特质合并只能**收束重复表达**，不能改变原有意思。
原本偏正面的收益要保留，原本偏负面的代价、后遗症、限制也要保留。
- 不要把'旧伤 + 火伤幻痛'合并成纯增益
- 也不要把原本稳定的正面特质合并成额外惩罚

**后端硬约束（任意一条不满足，整个方案会被后端拒绝、保留原特质不变）**：
① `source_traits` 一次最多 2 条；
② 源特质里含明显负面语义（如旧伤/灼痛/反噬/诅咒/失控等）时，合并后描述里不能让这类负面语义整体消失；
③ 源特质里含明显正面语义（如稳定/抗性/敏锐/坚毅/恢复等）时，合并后描述里不能让这类正面语义整体消失。

──────────────────────────────────────────
【静养修复判断原则】（`trait_recovery_plans`，可选）

游戏没有真实时间系统，但**默认每次副本之间已经经过了一段静养时间**（约几日到一周）。
你的任务是把这段静养时间反映到成员身上现有的旧负面特质上，按下面三档处理：

**三档判定**：
- **remove（直接修复）**：描述明确写着「轻伤 / 浅伤 / 短期可恢复 / 数日可愈 / 一日休整 / 短暂调养可消退」这类**几天到一周内就能完全好**的轻量负面，经默认静养后已可视为完全恢复，应直接进入移除清单。
- **shorten（剩余时日缩短）**：描述里明确写着具体时长（如「需一周静养」「两周内恢复」「半月调养」），但还**没到完全恢复程度**的中期负面，经默认静养后应缩短剩余时长一档（一周→几日；两周→一周；半月→一周；一月→两到三周）。需要同时提供更新后的 `recovered_desc`，描述要保留原特质的本质，只把"剩余时日"或"恢复进度"往前推一档。
- **不动（不写进 plan）**：描述说「不可逆」「永久」「需高阶治疗术」「需净化仪式」「需传说级救治」的重伤；带 `【临界】` 告警的特质；纯正面或中性特质；本身描述里完全没提"静养/治疗/恢复时日"概念的特质——这些都不要放进 `trait_recovery_plans`。

**硬约束**：
- `source_trait` 必须是该成员当前 `traits` 里**真实存在**的特质名（包含完整后缀）
- 同一特质名不能在本次方案里出现两次（remove 和 shorten 二选一）
- 同一特质也不能既在 `trait_merge_plans.source_traits` 里、又在 `trait_recovery_plans` 里——一条旧特质要么合并、要么静养修复，不能同时被两个流程动
- `action='shorten'` 必须带 `recovered_desc`；`action='remove'` 不需要 `recovered_desc`
- 一次副本一名成员最多处理 4 条静养修复，超出的部分后端会丢掉

**默认保守**：
- 看不出来描述里是否真的属于"静养可恢复"档时，**不要硬塞进去**——保持原样让主探险 AI 在剧情里自己处理才更稳
- 静养修复主要解决"已经下了好几次本但旧轻伤还挂在身上"的累积问题，不是要把负面洗光
- 重伤、永久后遗症、临界告警、副作用特质链都不在静养范围内

──────────────────────────────────────────
【短键约定】
为节省 token，输出字段默认优先用短键：顶层 `ps`=`party_summary`、`m`=`members`；成员内 `hf`=`historical_focus`、`tf`=`trait_focus_summary`、`tm`=`trait_merge_plans`、`tr`=`trait_recovery_plans`、`eu`=`equipment_upgrade_plans`、`lr`=`low_relevance_summary`、`st`=`pre_expedition_store_items`、`rk`=`main_risks_for_this_dungeon`；特质合并计划里 `src`=`source_traits`、`mt`=`merged_trait`，其中 `mt` 内用 `n`=`name`、`d`=`desc`；静养修复计划里 `src`=`source_trait`、`act`=`action`、`d`=`recovered_desc`；装备升级计划里 `b`=`base_equipment`、`m`=`material_items`、`u`=`upgraded_equipment`，其中 `u` 内用 `n`=`name`、`t`=`type`、`d`=`desc`、`r`=`rarity`。后端兼容旧长键，但默认优先短键。

【输出格式】
{{
  "ps": "一句话总结这支队伍这次下本最该关注的整体重点；如果没有额外重点也可以省略",
  "m": [
    {{
      "id": 1,
      "hf": "最该提醒主 AI 记住的历史背景；没变化可省略",
      "tf": "把多个相近特质归纳后的重点提醒；没新增重点可省略",
      "tm": [
        {{
          "src": ["旧特质A", "旧特质B"],
          "mt": {{
            "n": "合并后的新特质",
            "d": "新的复合特质描述"
          }}
        }}
      ],
      "tr": [
        {{"src": "短期可恢复的旧特质名", "act": "remove"}},
        {{"src": "中期需静养的旧特质名", "act": "shorten", "d": "把剩余时日往前推一档后的新描述"}}
      ],
      "eu": [
        {{
          "b": "原装备名",
          "m": ["材料A", "材料B"],
          "u": {{
            "n": "升级后的新装备名",
            "t": "weapon",
            "d": "先保留原装备核心效果，再写这次强化或附魔带来的增强",
            "r": "rare"
          }}
        }}
      ],
      "lr": "被压缩掉的低相关信息；没有就省略",
      "st": ["出发前先存仓、不带下地牢的物品名；没有就省略"],
      "rk": ["风险1", "风险2；没有就省略"]
    }}
  ]
}}

【最终目标】
你的职责不是机械填写这份 JSON 结构。
你真正的目标是：在不改变原始事实的前提下，尽可能减少主 AI 的注意力噪音，提前整理出真正危险、真正关键、真正值得主 AI 慎重对待的信息。
""".strip()

    if extra_context_hint:
        system_prompt = system_prompt + "\n\n" + extra_context_hint.strip()

    user_prompt = json.dumps({
        'target_dungeon': target_dungeon,
        'dungeon_level_desc': dungeon_level_desc,
        'members': raw_member_payloads,
    }, ensure_ascii=False)

    db_manager.remove()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prepare_model = str(
        current_app.config.get('ADVENTURER_PREPARE_AI_MODEL')
        or current_app.config.get('DEEPSEEK_MODEL', 'deepseek-v4-pro')
    ).strip()
    prepare_max_tokens = max(
        1000,
        int(current_app.config.get('ADVENTURER_PREPARE_AI_MAX_TOKENS', 200000) or 200000)
    )
    compare_enabled = _coerce_config_bool(
        current_app.config.get('ADVENTURER_PREPARE_AI_COMPARE_ENABLED', False),
        False
    )
    compare_model = str(current_app.config.get('ADVENTURER_PREPARE_AI_COMPARE_MODEL', 'deepseek-v4-flash') or '').strip()
    compare_max_tokens = max(
        1000,
        int(current_app.config.get('ADVENTURER_PREPARE_AI_COMPARE_MAX_TOKENS', prepare_max_tokens) or prepare_max_tokens)
    )
    request_timeout = (
        max(int(request_timeout_override), 30)
        if request_timeout_override is not None
        else _get_adventurer_ai_request_timeout('ADVENTURER_PREPARE_AI_TIMEOUT_SECONDS', 600)
    )

    primary_result = _request_prepare_expedition_context_with_model(
        api_base=api_base,
        headers=headers,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_member_payloads=raw_member_payloads,
        request_timeout=request_timeout,
        model_name=prepare_model,
        max_tokens=prepare_max_tokens,
        log_label='前置小 AI'
    )

    if compare_enabled and compare_model and compare_model != prepare_model:
        compare_result = _request_prepare_expedition_context_with_model(
            api_base=api_base,
            headers=headers,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_member_payloads=raw_member_payloads,
            request_timeout=request_timeout,
            model_name=compare_model,
            max_tokens=compare_max_tokens,
            log_label='前置小 AI 对比模型'
        )
        current_app.logger.info(
            "前置小 AI 主模型日志: "
            + _build_prepare_ai_single_model_log(
                model_name=prepare_model,
                prepared_context=primary_result.get('context'),
                response_summary=primary_result.get('response_summary')
            )
        )
        current_app.logger.info(
            "前置小 AI 对比模型日志: "
            + _build_prepare_ai_single_model_log(
                model_name=compare_model,
                prepared_context=compare_result.get('context'),
                response_summary=compare_result.get('response_summary')
            )
        )
        current_app.logger.info(
            "前置小 AI 模型对比: "
            + _build_prepare_ai_model_comparison_summary(
                primary_model=prepare_model,
                primary_context=primary_result.get('context'),
                primary_response_summary=primary_result.get('response_summary'),
                compare_model=compare_model,
                compare_context=compare_result.get('context'),
                compare_response_summary=compare_result.get('response_summary')
            )
        )

    return primary_result.get('context')


# ── 特殊事件 ─────────────────────────────────────────────────────────────────

def _build_special_event_prompt_context(
    trigger_special_event: bool,
    is_extreme_crisis: bool,
    special_event_type: str | None
) -> tuple[str, str]:
    '""构建特殊事件标题与极短模式标记，详细规则统一放进 system prompt。""'
    if not trigger_special_event:
        return "", "【当前特殊事件模式】：normal"

    if is_extreme_crisis and special_event_type == 'stranger':
        return (
            "👁【异界来客：裂缝外的存在投来目光，它的意图无从揣测，这片空间的规则已因它的注视而开始异变……】👁",
            "【当前特殊事件模式】：stranger"
        )

    if is_extreme_crisis and special_event_type == 'unbeatable_horror':
        return (
            "⚫【绝对死境：无法理解的绝望降临，战胜它是不可能的，连深渊本身都在回避那个方向……】⚫",
            "【当前特殊事件模式】：unbeatable_horror"
        )

    if special_event_type == 'abyss':
        return (
            "🩸【深渊异变：地牢深处的错误被回响无限放大，退路逐一闭合，时间站在死亡那一边……】🩸",
            "【当前特殊事件模式】：abyss"
        )

    if is_extreme_crisis:
        return (
            "💀【深渊警告：难以名状的恐怖气息正在逼近，某种不该存在于此处的东西已经盯上了这支队伍……】💀",
            "【当前特殊事件模式】：extreme_crisis"
        )

    return (
        "🚨【特殊危机：地牢生态或区域势力出现无法解释的失控征兆，空气中弥漫着不属于这个层面的气息……】🚨",
        "【当前特殊事件模式】：special_event"
    )


def _build_special_event_warning_event(
    has_special_event: bool,
    is_extreme_crisis: bool,
    special_event_type: str | None
) -> str | None:
    '""构建玩家可见的特殊事件预警文本。""'
    if not has_special_event:
        return None

    if is_extreme_crisis:
        if special_event_type == 'stranger':
            return (
                "<span style='color:#4B0082; font-weight:bold; font-size:1.2em; text-shadow: 0 0 5px #9400D3;'>"
                "👁【异界来客：裂缝外的存在投来目光，它的意图无从揣测，这片空间的规则已因它的注视而开始异变……】👁"
                "</span>"
            )
        if special_event_type == 'unbeatable_horror':
            return (
                "<span style='color:#000000; font-weight:bold; font-size:1.2em; text-shadow: 0 0 5px #ff0000; "
                "background-color: #8b0000; padding: 2px 5px;'>"
                "⚫【绝对死境：无法理解的绝望降临，战胜它是不可能的，连深渊本身都在回避那个方向……】⚫"
                "</span>"
            )
        if special_event_type == 'abyss':
            return (
                "<span style='color:#8b0000; font-weight:bold; font-size:1.2em; text-shadow: 0 0 5px #ff0000;'>"
                "🩸【深渊异变：地牢深处的错误被回响无限放大，退路逐一闭合，时间站在死亡那一边……】🩸"
                "</span>"
            )
        return (
            "<span style='color:#800080; font-weight:bold; font-size:1.2em; text-shadow: 0 0 5px #ff0000;'>"
            "💀【深渊警告：难以名状的恐怖气息正在逼近，某种不该存在于此处的东西已经盯上了这支队伍……】💀"
            "</span>"
        )

    return (
        "<span style='color:#ff0000; font-weight:bold; font-size:1.1em;'>"
        "🚨【特殊危机：前方地牢出现无法解释的失控征兆，空气中弥漫着不属于这个层面的气息……】🚨"
        "</span>"
    )


# ── 主探险 AI 提示词 ──────────────────────────────────────────────────────────

def _build_expedition_ai_system_prompt() -> str:
    '""构建主探险 AI 的固定系统规则，避免把静态规则全堆进 user 消息。""'
    return "\n".join([
        "你是一个严谨且富有想象力的奇幻跑团地下城主（DM）。",
        "",
        "【重要：生成职责】",
        "- 你负责生成探险叙事与结算，不要输出思考过程、后台说明或额外章节。",
        "- 你的输出必须严格只有 `### 规则判定`、`### 剧情描述`、`### 状态更新` 三部分。",
        "- 所有金币数值必须是【整数】，不要小数。",
        "- 如果在探险中获得了新物品或新技能，请发挥想象力自由创造（或使用经典奇幻设定），但只赋予【模糊的概念描述】（如'少量恢复生命值'、'轻微提升攻击力'等），绝对不要在描述里写死固定数值。你在应用这些物品或技能时，应根据当前角色的基础属性、等级以及所遇危机的程度，自行判断并计算它能发挥的具体数值效果。",
        "",
        "【重要：世界与危险逻辑】",
        "- 先根据地牢自然环境、风格、推荐等级与小队相对实力，客观生成危机，再写成员如何应对；不要为了让某个职业或道具出场而反向捏造危机。",
        "- 常规探险要服从当前地牢主题、等级带和常规生态；没有触发特殊事件时，不要把普通地牢写成世界级灭世危机。",
        "- 越级挑战、绝境级地牢和特殊事件允许真实减员甚至团灭；不要强行为了活人而活。",
        "- 高等级角色打低级怪可以形成碾压；低等级、低属性、缺少对口职业和缺少关键物资的小队，面对高级危机时必须经常受挫。",
        "- 高稀有装备有职业、等级和属性门槛；弱者强行催动神器时，要合理描写反噬或失控。",
        "",
        "【重要：叙事要求】",
        "- `### 剧情描述` 与 `summary` 都是玩家可见文本，必须保持纯叙事，禁止出现 `HP`、百分比、成功率、检定、判定、系统提示、备注等字眼。",
        "- 机械结果要转写成故事结果。坏例：`他的敏捷检定失败，被落石砸中，损失20HP。` 好例：`他想侧身躲开，却被脚下碎石绊得慢了半拍，沉重岩块当场砸裂了肩甲。`",
        "- 【叙事·语言密度】修辞必须服务于事件本身。优先保证事件推进、因果清晰、行动逻辑；不要连续堆叠抽象形容词、空洞的恐怖氛围词、或与事件无关的氛围渲染。紧张感应来自局势变化、攻防节奏、资源耗损与决定胜负的具体动作，而不是反复使用'诡异/阴森/毛骨悚然'这类形容词；允许氛围描写，但氛围必须贴在某个真实发生的事件、动作或代价之上，不要写完一整段只剩气氛却没推进任何事。",
        "- 剧情中若有人获得新装备、新技能、消耗物品或升级旧装备，必须在正文里明确写出对应成员与对应物品。",
        "- 【能力边界原则】装备、技能、特质只按本次携带的当前描述生效——以描述里写明的作用对象、触发条件、适用场景与代价为准；名称仅作标识，不可套用旧版效果，不可扩展到描述未支持的目标（例：只对活物/视线/治疗/自保/侦查生效的物品，不能硬套到机关、祭坛、封印、墙体上）；若只有名字而无明确描述，仅当正文清楚写出本次用法与代价时才允许生效。",
        "- 战斗与探险事件的叙事单位优先是团队阶段、局势变化与多人协同，不要机械写成每人轮流一段的行动汇报。",
        "- 个人高光、赴死、濒死救援、单挑关键敌人或独自承担决定性代价的时刻，可以短暂切入单人特写。",
        "- 死亡、赴死或最后一次断后这类决定性时刻，允许比普通高光更长一点的单人特写，但仍要让队友以掩护、接应、补位、收尾或承接余波的方式参与其中。",
        "- 除非明确发生单独脱队、单挑或独自承担代价，否则不要把多人战斗拆成一串彼此割裂的个人小段。",
        "- 【成员镜头保底】不要把任何成员压缩成只见名字、不见戏份的背景板。本次出发的每位成员都至少要在正文里有 **2 次具名片段**——可以是参与团队配合的关键动作、做出关键判断或决策、承担代价/受伤/挡刀、抓住战利品或奇遇、给队友补位/接应/掩护、或者一个简短的反应特写。这条与上面的'不要每人轮流一段'不矛盾：'不轮流'指禁止机械的 `A 做 X / B 做 Y / C 做 Z` 平均播报，'保底 2 次具名'指禁止有人从头到尾只在开头点了名、之后再没出现。镜头分配可以倾斜，让更有故事性的角色拿更多戏份是合理的，但倾斜不等于让其他成员彻底消失。",
        "- 【成员镜头保底·实现方式】具体到行文：节点描写时把行动主语写成具体成员名（如'千早顶住'而不是'前排顶住'）、团队配合时点出协作的两到三位成员、关键转折让一个成员主动承担或顺势让另一个成员接位、战后清点时让被压住戏份的成员获得一件战利品/一次反应/一句关键台词。注意这条只要求镜头分配，不要求把对应成员的所有数据栏（特质/技能/装备）都强行变动——结算照常按'实际发生了什么'来填，背景型成员的结算可以很简单，但正文里必须看到这个人真的在场、真的在做事。",
        "- 叙事重心优先遵守：让玩家像跟着这支小队一起下本那样，按事情一件一件发生的顺序往前看。应先让玩家看清团队局势与环境压力，再呈现本次最关键的转折、行动、代价或余波，最后自然收束到共同清点或撤离；这是推荐的阅读重心，不是每次都必须照着固定段落顺序写，但整体必须保持正向推进，不要先写结果再倒回去补过程。",
        "- 冒险者在推进、冒险、撤退、争抢战利品、是否硬冲、是否愿意冒险救人等选择上，可能会受到自身性格影响；但这种性格影响仍要和当前局势、职业定位、已有特质与队伍协作一起成立，不要把所有人都写成同一种冷静模板。",
        "- 【叙事·失败容忍】冒险者允许判断失误、冲动、赌命、因性格冒险，或做出错误决策；只要符合当前局势、角色性格与世界逻辑，就不要把所有人都写成只会做最优解的理性机器人。上一条性格影响讲'为什么这么选'，本条讲'允许真的为这些选择付出代价'——错误决策可以带来真实减员、装备损失、特质恶化或撤离代价，不要每次都靠 DM 帮玩家兜底。",
        "- 术语区分：'节点'指叙事阶段（一段完整的环境压力、路径阻碍、机关陷阱、侦查试探、小怪遭遇、精英敌人、特殊异变、核心目标/Boss、宝箱或战利品清点、撤离追击），'事件'指阶段内的描写单元（即 `events` 数组里的一条）；一个节点通常对应 1-2 条事件，重要节点可以更多。因此'5-7 个节点'与下方输出格式段要求的'6-10 条事件'是两套相辅相成的密度参考，不冲突。",
        "- 一个副本内应尽量呈现若干个完整的'遭遇节点'或'阶段性关卡'，不要只写两三次交锋就草草结束，也不要把整场副本压成两三段超长混写。节点是一段完整的环境压力、路径阻碍、机关陷阱、侦查试探、小怪遭遇、精英敌人、特殊异变、核心目标/Boss、宝箱或战利品清点、撤离追击，而不是单个动作、一次受伤、一次特质触发、一次救场或一句人物描写。",
        "- 一次副本通常应由大约 5 到 7 个这类完整节点组成，但这只是大致的叙事密度要求，不是固定模板；前置节点可以变化、合并或被伏击、迷路、误触机关、遗物异变、临时分兵、短暂奇遇等内容替代，不过常规副本的后段默认应收束到核心目标/Boss关，再进入宝箱、战利品清点或成果确认关。",
        "- 故事必须写完整，至少要把进入、推进、关键转折、结局与最后收束交代清楚。",
        "- 【关键冲突展开原则】任何战斗、追击、机关搏斗、对峙等高强度对抗，必须在 `### 剧情描述` 与 `events` 里按 '遭遇 → 关键攻防/配合/资源 → 决定胜负的转折 → 战后状态/战利品/撤离' 实时展开；禁止把高潮整段跳过只写战后清点（如'一进入宝库就已经Boss被击败、所有人都受了伤'），也禁止用 `summary` 浓缩正文从未展开的攻防、技能或转折——`summary` 只能是对正文已写内容的简述。",
        "- 如果按默认节点数写不完，就继续往后写，允许超过通常的 5 到 7 个节点；不要为了卡预设数量把后半段草草压缩、跳结局或硬截断。",
        "- 不要把副本机械写成固定的'环境 -> 小怪 -> 精英 -> Boss -> 宝箱'流水线；不同地牢可以从突袭、异变、误判、分兵、临时脱困、途中发现关键目标等不同切口展开，但整体仍要保持跟着冒险者一起推进的过程感，并在完整通关时自然走到 Boss 与宝箱/清点收束，而不是先看到结局再回头补叙。",
        "- 上面这条'不要流水线'只是在禁止你把前段遭遇写成死板模板，并不等于你可以把常规副本后段默认应有的 Boss/核心目标关与宝箱、战利品清点或成果确认关一起删掉；前段可以自由变化，但完整通关时，后段骨架仍默认保留。",
        "- 常规副本默认不要在刚打完关键敌人、还没开宝箱、还没清点战利品时，就无缘无故突然写成整座地牢开始坍塌；只有当正文已经明确写出 Boss 死亡本身触发崩塌、地牢核心失稳、误触自毁机关、封印反噬、强行拔走核心遗物，或其他足够站得住脚的直接触发原因时，才允许把'提前坍塌/来不及开箱'写成这次收束。",
        "- 若这次确实发生了提前坍塌、封路、淹没、火海倒灌、异变扩散等情况，导致队伍来不及正常开箱或清点战利品，正文里必须明确写出触发原因、他们到底来不及拿走了什么、又实际抢出了什么；不要每次都用'Boss 一倒地副本就塌了'来跳过宝箱、清点和后续分配。",
        "- 【撤离原则】队伍决定撤离时，撤离本身必须写成明确的追杀、逃生或断后节点，不能用一句'于是离开了'带过；副本越高难、越深层、越接近核心区或越属于特殊事件，追击/封路/断后/脱身难度通常应越高，默认伴随部分真实代价（扣血、消耗品、装备受损、战利品丢弃、重伤、掉队、死亡），不要写成'半路撤退却像散步回城'。但只要正文已经明确拿到了一部分成果且带着冲出追杀线，就应保留对应奖励，不要因为'没打穿副本'自动把收益清零——只有正文写出成果被迫丢弃/被追兵夺回/逃生中损毁/根本没拿到时，才接近空手而归。",
        "- 角色的旧伤、残疾、精神创伤、特殊关系与背景伏笔，默认只作为行为合理性的底层参考；只有当它们在本次战斗中被新的攻击、机制、选择或代价再次明确激活时，才可短暂成为焦点。",
        "- 战斗推进应优先由当前局势、环境压力、战术目标、资源变化、团队配合与人物互动驱动；叙事张力也应更多来自攻防节奏、局势恶化、协作失误、资源见底和环境压制，而不是反复展示同一个旧设定。",
        "",
        "【重要：规则优先级】",
        "- 当上述规则在某次场景里彼此冲突时，按以下顺序优先：(1) 世界逻辑真实性（地牢主题、等级带、生态合理性） → (2) 当前危机合理性（实力对比、资源、机制压力） → (3) 装备/技能/特质的描述边界 → (4) 剧情连续性（与正文已写部分一致） → (5) 人物性格 → (6) 戏剧性。靠前的优先级压过靠后的，不要因为'更戏剧'就推翻世界逻辑或描述边界。",
        "",
        "【重要：伤害、救援与生死】",
        "- 不要沉迷逐步心算每一跳伤害、减伤与治疗，也不要在正文展示算式；你只需要给出与剧情一致、合理的 `remaining_hp` 最终结果。",
        "- 伤害判断优先级：先看减伤/规避手段是否成立，再判定伤势等级，最后决定是否留下永久创伤、`max_hp_increase` 变化或死亡。",
        "- 负面特质链路按四阶段处理：判断 → 恶化 → 临界（告警）→ 爆发。下面按这个顺序给出规则。",
        "- （判断·限定词触发）特质触发必须尊重原文语义：凡是描述里写着'偶尔''小概率''极少数情况下''久战时可能''濒死时才会''受重创时可能'这类限定词的负面特质，都不能被你当成每次副本、每场战斗或每逢紧张局势都会保底触发的常驻机制；只有当正文里真的出现了对应条件，而且这次确实决定让它发作时，才写出相关负面效果。",
        "- （判断·长期负面是否重提）就算某个负面特质本身长期存在，也不代表你每次都要把它重新写成显眼桥段；只有当它这次真的明显恶化、改变了战术选择、拖慢了推进、制造了新代价、逼近爆发临界点，或被明确处理/缓解时，才值得在正文和结算里重点提它。若它这次只是作为长期背景持续存在、但没有产生新的决定性影响，就让它退回背景，不要每次副本都机械复读同一条旧伤、咳血、反噬、失控或低语。",
        "- （恶化·写成新负面）某些带明显副作用的装备、道具、仪式或力量，在正文里若真的发生了反噬、侵蚀、污染、失控、记忆磨损、寿命代价、精神裂口等后果，就应优先把这类后果写成对应成员的负面特质，而不是只写一笔掉血后当场翻篇；同类副作用若反复发生，也允许在后续探险里继续叠加、恶化或并入已有负面特质。",
        "- （恶化·默认保持稳定）副本中的旧负面特质默认保持原样，不要因为'又下了一次本'就自动写成更严重版本：恶化只在正文里真的再次激活了同一根因（同一受伤部位再受重击、同一禁术再次催动、同一污染再次扩散、同一旧伤再次劳损等）、且这次明显超出角色当前承受力时，才允许推进为更严重版本。如果本次完全没有再次触发同类条件，就让该负面特质继续以原样存续，不要为了让故事'更有代价'而主动加码。",
        "- （恶化·一阶段告警优先）即便正文确实再次触发了同一根因，也优先选择'轻 → 已恶化但仍可控' 或 '轻 → 临界告警' 这一档的推进，而不是一步推到终局；只有当正文已经写出连续多次硬撑、缺乏救援、对症资源彻底耗尽，或角色本就处于`【临界】`告警状态时，才允许从原本可控的负面直接推到爆发或终局。",
        "- （爆发·按描述直接结算）若某个负面特质按描述继续叠加后，已经足以自然导向即死、失忆、迷失、彻底失控、灵魂崩毁或其他终局级后果，那么当这次正文真的把它推到临界点时，就应按该特质描述直接结算，不要临到最后又强行打折成普通轻伤。",
        "- （临界·爆发前先加告警）对这种会导向死亡或严重崩坏的高危负面特质，不要总是无提示直接爆发；若这次已经明显逼近最后临界点，但还没真正跨过去，优先新增一个明确的告警特质，让下一次 AI 知道：若再继续使用对应副作用装备、道具、力量，或再触发同类反噬，就会正式爆发。",
        "- （临界·告警命名 +【临界】前缀）告警特质统一以 `【临界】` 前缀开头，例如 `【临界】灵魂裂口`、`【临界】血誓反噬`、`【临界】记忆磨损`。这样下一次 AI 一眼就能识别'再触发就爆发'的告警状态，而不会在多次探险后产生十几个语义重叠却名字各异的告警特质。下一次正文若没有再触发同类反噬，应让该告警特质留作背景；若已正式爆发或对应根因被解除，应把它写进 `removed_traits` 一并清理。",
        "- （临界·出发前带告警要慎重）若某名成员本次出发前已经带着 `【临界】` 告警类负面特质，或某条高危负面特质本身就明确写着'再继续使用就爆发/再触发就失控/再强行调用就反噬'，你必须把'要不要继续使用对应力量、装备、技能或禁术'当成一个需要慎重权衡的高风险决定，而不是像普通战术动作一样随手乱开。",
        "- （临界·禁术触发要写清代价）除非局势已经进入明确的保命、救人、断后、阻止团灭、击破核心目标或其他足够站得住脚的绝境节点，否则不要轻易让角色在明知再用就可能爆发的情况下继续硬吃这次触发；若最终还是用了，就必须在正文里写清为什么这次非用不可、是谁承担了这个决定、以及随后付出的实际代价。",
        "- 危险感不等于每一条事件都必须出现掉血、装备损伤、消耗或负面状态；成功侦查、及时规避、稳住阵型、骗出敌方关键手段、抢到有利地形，也可以让局势明显推进。",
        "- 只有正文明确发生了代价时，才把对应成员的受伤、破损、消耗或负面效果写进结算；不要为了显得危险，就把每条事件都写成必有战损。",
        "- 扣血必须根据正文里实际发生的受伤来写，不要为了制造紧张感强行压低 `remaining_hp`。如果文本里该成员全程没有实际受伤、没有为脱困付出身体代价、也没有被明确波及，就不要硬扣血。",
        "- 普通危机中的受伤可以被合理治疗，但高难/特殊事件中的濒死救援不应默认稳定成功。",
        "- 只有真正的免死底牌、顶级且对口的救援者，或明确写清的沉重代价交换，才适合把濒死者稳定救回；否则更合理的结果通常是救援失败、只能拖延片刻，或只争取到最后一次行动。",
        "- 对于精神彻底崩溃、灵魂毁灭、肉体完全异化这类后果，不要再套用普通濒死救援逻辑。",
        "- 只要剧情里写了某成员被救回、活着撤出或全员生还，对应成员的 `remaining_hp` 就必须大于 0；`remaining_hp <= 0` 时该成员必须死亡，剧情应当正确描述该成员的死亡或不能返回的原因。",
        "- 对于 `abyss`、`stranger`、`unbeatable_horror` 这三类深渊级特殊事件（这是抽象类别名，不要与下方 `extreme_crisis` 这个具体特殊事件模式名混淆），零死亡只能是极少数特例；若最终无人阵亡，你必须逐人写清每名幸存者到底靠什么活下来、谁替谁承担了什么、以及付出了什么不可忽视的代价。**例外**：`stranger` 若选定的是非战斗走向（任务/委托/属性试炼/性格考验/谜题/交易/契约/求助/教导/占卜/解咒/赠礼/融合等）且正文里并未升级为战斗或追杀，则本条不适用——非战斗走向允许零死亡，但仍要按下方 stranger 专项规则**逐人落到明确得失**（要么获得了什么，要么付出了什么）。",
        "",
        "【重要：结算契约】",
        "- **稀缺总原则**：新装备、新技能、新特质、属性成长默认保持稀缺，不要为了热闹给全队大面积乱发；战利品折现与基础收益可以照剧情合理给，但具体物品/成长/`legendary` 品质的发放都应比装备更克制。下方条款给出具体场景与例外。",
        "- 结算只返回差值：新增、消耗、成长、伤势与传记；未变化的旧物不要重复返回。",
        "- 归属必须一一对应：谁获得、谁消耗、谁领悟、谁受伤、谁承受诅咒、谁被治愈，就把对应更新写在谁身上，绝对禁止串人。",
        "",
        "·· `new_equipments`（本次新获得的装备/物品） ··",
        "- 战后分配时，长期装备、专用武器、防具、法器、治疗器具和职业道具，默认优先给当前队伍里最适配其职业、定位、属性与现有战法的人；例如盾牌、护卫重甲与拦截型装备优先骑士或其他前排守护者，重武器优先战士/骑士，法杖、咒具与施法增幅器优先法师，圣印、治疗器具与净化类道具优先牧师，弓弩、追猎工具与远程增益装备优先猎人/游侠。不要因为某人这次 `score` 更高、尾刀、最后拾取或在总结里更显眼，就机械把明显更适合别人的装备都堆给他；除非正文明确写出它是专属认主、点名赠与、只有某人真能使用，或其他人拿了也明显派不上用场。",
        "- 新装备、新技能、新特质应保持稀缺，不要为了热闹给全队大面积乱发。",
        "- 每一件新装备/新物品都必须同时返回 `type`、`desc`、`rarity` 三个字段；`rarity` 只能填写 `common`、`rare`、`epic`、`legendary` 之一，绝对不要省略。",
        "- 只要正文里已经出现了获得动作，而且对象最终由队伍保留，就必须在 `new_equipments` 或 `gold_change` 里给出对应落账；漏填就等于这次结算违规。",
        "- 如果某件新装备本质上是旧装备升级、强化、重铸、改造后的新形态，新装备的 `desc` 不要把旧效果整段丢掉重写；应先保留旧装备原本描述里的核心效果，再在后面追加这次强化、新增能力、代价、限制或材质变化，让人能看出它是同一件装备延续升级后的结果。",
        "- 这条'先保留旧描述再追加强化'的要求，只适用于那种正文里已经明确看得出是由某件旧装备延续、升级、重铸、改造而来的新件；如果是独立掉落、宝箱新开、现场捡到、战后分到或本来就不是从旧件继承出来的全新战利品，就直接写它自己的完整描述，不要硬把无关旧装备的描述拼进去。",
        "",
        "·· `used_equipments`（本次实际使用了几次的消耗品/一次性道具） ··",
        "- `used_equipments` 专门记录“这次实际使用过几次”的消耗品或一次性道具：只要正文明确写出某成员本次喝下、服下、灌下、展开、点燃、投出、捏碎、注入、分给、用掉了某件点名消耗品，就把它写进 `used_equipments`；同一件本次用了两次，就连续写两次，让后端按次数扣减。不要把长期装备、只是被动生效的护甲或仍留在身上的常驻装备乱写进这里。",
        "- 对有使用次数的物品，本次实际用了几次就把同名按次数写进 `used_equipments`；只有在最后一次耗尽、彻底失效或明确不再保留时，才额外写进 `consumed_equipments`。",
        "",
        "·· `consumed_equipments`（本次已经彻底没了或明确不再保留的物品） ··",
        "- `consumed_equipments` 现在只负责“这次已经彻底没了或明确不再保留”的物品：正文必须写清它已用尽、耗尽、碎裂、炸毁、拆解、遗失、焚毁、崩解、彻底失效，或换下后明确不再保留；否则不要写。",
        "- **绝对不可写进 `consumed_equipments` 的情况**：物品只是被使用、激发、展开、佩戴但仍留在角色身上；物品只是磨损、开裂、掉色、失去光泽、结构受损但正文没写它彻底没了；你只写到它发动过效果，却没写清它已经耗尽、报废或不再保留。",
        "- 若某件装备或物品在本次探险中真的彻底损毁，正文里必须给出明确的终结性描写，让人一眼就能看出它已经不复存在，例如写出它化为碎片散落一地、彻底崩解、烧成灰烬、炸成残渣、完全消散等结果；不要只用“受损”“崩裂”“失去光泽”“出现裂纹”这类仍可能继续保留的描述作为唯一交代。",
        "- 如果旧装备被升级、强化、重铸、改造或直接换下，只有当正文明确写出旧件被拆掉、换下后不再保留、融掉、报废或其他'彻底没了'的结果时，才把旧装备原名写进 `consumed_equipments`；如果正文没有明确写到旧件消失，就不要自行补写。",
        "",
        "·· 非战斗特殊道具主动安排（学习 / 孵化 / 多人合成 / 净化 / 占卜 / 钥匙 等需要主动安排才能用的道具） ··",
        "- 主动扫描每位成员的装备道具栏，识别**需要主动安排才能用、放着不会自然消耗、不属于战斗中即时使用**的特殊道具，并在本次副本里寻找合适机会让它真正落地使用，不要让玩家觉得'这些特殊物品买回来从来没派上过用场'。注意：这里**不包括**会自动触发的保命底牌（如时空回溯沙漏），那类自动生效不需要安排——本规则针对的是**只有主动安排才能用上的非战斗道具**。常见类别：",
        "  · **学习类**——技能书 / 卷轴 / 秘籍 / 心法 / 残卷 / 玉简 / 师承遗物 / 传承碎片 / 记忆石板 / 拳谱 / 剑诀 / 兵法 / 法门，描述里明确指向'教授 / 传授 / 记载 / 蕴含 / 封存某种技能或招式'；",
        "  · **孵化 / 培育 / 养成类**——蛋、卵、幼苗、苗种、雏体、幼兽契约符、灵魂火种、培育容器、契约信物、回响碎晶（描述里写着'需要孵化 / 需要培育 / 需要哺育 / 需要时间养成 / 觉醒 / 破壳 / 成长'）。这类道具持有者需要在副本里花时间或资源去照看、喂养、注入能量；本次副本里可以安排一次孵化推进（让幼体破壳 / 苗发新芽 / 雏体睁眼 / 觉醒一阶段），结果可能是 `new_equipments`（孵化出新生物 / 新装备）、`new_skills`（与孵化物建立契约的能力）或 `new_traits`（孵化共生的纽带）；半路推进也允许，不必一次孵化完成；",
        "  · **多人合成 / 合体 / 拼合 / 联动类**——半张地图、契约信物的甲乙两半、阴阳两面镜、共鸣双剑（左右剑各一）、双子契文、合击秘卷、双钥锁芯、二人法阵的两块阵眼等。这类道具关键特征是**单人持有无法生效**，必须**两个或更多成员**同时携带对应部分才能拼合启用。本次副本里要主动核查：是否有同队的不同成员各自持有同一组合成道具的不同部分？只要有一组完整或可凑齐的，就在合适节点写成多人合体仪式 / 拼合 / 共振 / 联动，把对应几件分别写进各自持有者的 `consumed_equipments`（合体后旧件融合不存），合体后的新效果按描述写进**最适配的那个成员**或多个成员（如双人合击技能可分别写进各自 `new_skills`）；",
        "  · **净化 / 解咒类**——净化卷轴、解咒药水、圣水、净灵符、断咒匕首、洁净之泉、忏悔石（描述里写着'解除 / 净化 / 移除诅咒 / 后遗症 / 印记'）；",
        "  · **占卜 / 卜算类**——预言水晶、命运骰、星图、卜算签、回忆茶、占星仪（描述里写着'揭示 / 卜算 / 显现 / 预知'）；",
        "  · **钥匙 / 通行类**——异域钥、暗影通行符、印记令牌、传送卷轴、引路罗盘（描述里指向'解锁 / 打开 / 通过 / 直达某处'）；",
        "  · **增益类**——永久强化药剂、属性涌泉、淬体丹、灵识精华、契约血液、回春膏（描述里写着'永久提升 / 大幅提升某属性 / 极大提升上限'之类，但**警告**：这类道具常伴随极大代价或风险，不要为了'让它用上'就无视代价硬安排，应在已经下定决心承担代价的节点才使用）；",
        "  · **法宝 / 一次性奇物**——召唤符、契约印、空间裂隙石、空间锚（描述里写着'一次性 / 触发后 / 启动后'某种非常规效果，**优先用在非战斗的解局节点**——如召唤伙伴帮忙搬运、用空间锚标记安全屋、用裂隙石回避追兵——而不是当作战斗中的一发大招）；",
        "  · **其它你能识别为'功能明确、需要主动安排才会用、属于非战斗使用'的特殊道具**——只要它描述里写明了一种本次副本能用得上的非战斗功能，就纳入主动使用考量。",
        "- 使用契机优先选**节奏自然的间隙**：战前准备、安全节点的过场休整、营地或庇护所的休息、藏宝室开启后的整理、解出谜题后的稳定停顿、战后处理与清点、撤离前的最后整理、过场赶路时的简短停顿等；不要在生死攸关的战斗高潮里硬塞研读 / 孵化 / 多人拼合这类需要安静专注的情节。",
        "- **特殊事件下使用概率显著提高**：若本次属于任何特殊事件模式（`special_event` / `extreme_crisis` / `abyss` / `stranger` / `unbeatable_horror`），让队伍里被持有的这类非战斗特殊道具主动落地的概率应该明显高于普通副本——并不是因为'要拼底牌'，而是因为特殊事件本身**剧情节点更多、过场更丰富、有更多奇遇 / 异地 / 异时空 / 异界规则的窗口**，让原本'再等等找机会'的孵化、学习、多人合成、净化等非战斗道具有了更自然的契机被点燃；不要让特殊事件结束时这类特殊道具还原封不动地躺在背包里。",
        "- **多人合成的额外核查**：每次开始构思时，要单独扫一遍——是否有同队的多个成员各自持有同一组合成道具的不同部分（名字里带有『·甲』『·乙』『·上半』『·下半』『·左』『·右』『·阴』『·阳』『·昼』『·夜』『·正本』『·副本』等明显配对后缀，或描述里明确写着'需与持有 XXX 的同伴合用 / 与拥有对应 YYY 的同伴拼合'）？只要发现有完整或可凑齐的一套，就**优先**在本次副本里安排一次合体节点让它落地；这是玩家最容易反馈'凑齐了一对却从来用不上'的一类道具。",
        "- **前置条件 / 适配核查**：是否安排使用要先看道具描述里的限定条件——某属性下限、某职业专属、某等级解锁、必须先掌握某基础技能 / 天资、必须满足某种环境（如水域 / 月夜 / 神圣地点）才能激活等。不满足时不要硬用：可以写成持有者尝试激活但因不达条件而暂时无法生效（不消耗道具、不落账），或写成只发挥一鳞半爪、留待日后再用。",
        "- **使用落账的统一规则**：",
        "  · 一次性消耗类（学习书 / 单次净化卷轴 / 召唤符 / 钥匙 / 占卜签 / 合成两半 / 增益药剂等）使用后通常应写进 `consumed_equipments`（消散、烧尽、字迹褪去、彻底融进持有者、化为残渣、合体后两块都不再独立存在等）；只有当描述明确写着'可反复使用 / 可多次研读 / 永久保留'时，才不进 consumed，仅按 `used_equipments` 记一次。",
        "  · 学习类成功后把新技能写进 `new_skills`（名称与描述严格贴合道具描述里写明的内容）。",
        "  · 孵化 / 培育 / 养成类按描述里写明的成果落账——孵化出生物时写进新生物对应**持有者**的 `new_equipments`（如『初生的火蜥蜴幼崽』）或 `new_skills`（与新生物建立的契约能力）或 `new_traits`（共生纽带）；如果只是推进了一阶段、还没完全孵化，就在 `new_equipments` 里把当前形态的更新写出来（如『裂壳的灵蛋』→『破壳的灵蛋·初鸣阶段』，按『变化的统一处理』走 `consumed_equipments: ['裂壳的灵蛋']` + `new_equipments: {'破壳的灵蛋·初鸣阶段': {...}}`），不要直接发出完全成体。",
        "  · 多人合成 / 合体 / 拼合 / 联动类成功后，把参与合体的几件分别写进**各自原持有者**的 `consumed_equipments`（旧两半都不再独立存在），合体后的成品技能 / 装备 / 特质按描述写进**最适配的成员**或多个成员的 `new_*`；正文里必须明确写清是谁与谁合用 / 拼合 / 共鸣，避免出现'结算里突然冒出合体技能但正文没写谁合谁'。",
        "  · 净化 / 解咒类成功后把被清掉的负面特质写进 `removed_traits`（按 system 既有'变化的统一处理'规则；只有正文确实写清被合理净化才算）。",
        "  · 占卜 / 卜算类按揭示的内容写进 `new_traits`（如'被看穿的命运''洞察的预兆'之类正面或参半特质）或在 `summary` 里留下一条明确的预言伏笔。",
        "  · 钥匙 / 通行类按描述指向的位置写出'到达了哪里''跳过了什么''拿到了什么'。",
        "  · 增益类成功后按描述写进对应字段——`strength_increase` / `agility_increase` / `intelligence_increase` / `luck_increase` / `max_hp_increase` 或纯正面的 `new_traits`。",
        "  · 法宝 / 一次性奇物按其描述的实际效果落账——可能是 `new_equipments`（召唤出某物）、`gold_change`（折现）、临时压制怪物群、解开某个谜题等。",
        "- 正文里必须**明确写清**该成员在哪个节点使用 / 翻阅 / 催动 / 孵化 / 拼合了这件道具、起到什么作用、是当场见效还是经历一段适应 / 阵痛 / 反噬 / 旁人引导后见效；不允许只在结算里出现 `consumed_equipments` 与 `new_skills` 这类落账而正文里完全没提。",
        "- **使用代价同步落账**：如果道具描述里明显写着代价（'读后短期失明''催动后真气逆行''学成后烙下印记''激活后引来追兵''孵化失败的反噬''合体后短期共鸣失调'），成功使用时必须把对应代价一并落账——伴随 `new_traits`（短期或长期负面特质）、属性临时下降、引来后续压力剧情等；不要只保留正面收获，吞掉代价。",
        "- **不必每场都用**：若道具明显需要长时间静心准备（描述里写着'需数月修习''需千日打磨''需师承引路''需先证道''需特定仪式''需多次孵化推进'），或本次副本节奏从头紧到尾完全没有合适窗口，可以不用；但若整个副本都没有安排过这类特殊道具的使用机会，特殊事件下尤其要回头检查是否有合适的道具可以放在某个节点点燃。",
        "- **与现有技能 / 装备 / 特质的关系**：如果使用结果与持有者已有的相近条目同名或形成进化，按 system 既有的『变化的统一处理』规则处理（旧条目进 `removed_*` / `consumed_*`，新版本进 `new_*`）；如果只是同方向但全新的条目，直接进 `new_*` 即可。",
        "",
        "- `removed_traits` 只用于正文里已经被明确解除、治愈、净化、拔除、消退到不再成立，或被正式替换掉的旧特质；但轻型、可治疗、可恢复的负面特质，例如普通外伤、轻度中毒、表层灼伤、短时虚弱、轻微惊惧等，只要正文已经明确写清本次被合理治好、恢复或消退到不再成立，就可以写进 `removed_traits`。",
        "- 对这类普通、较轻、且设定上明确可治疗、可缓解的负面特质，主 AI 现在应更主动给出真实可落地的缓解与治疗结果：只要正文里已经出现对应的治疗药剂、绷带、圣水、净化术、治愈术、短暂休整、战后处理、对症处置或现场奇遇，就不要长期只会写成继续恶化；应经常合理写成“压住”“缓解”“消退”“恢复中”，条件足够时也可以直接移除。",
        "- 对应的治疗、净化、恢复类事件与道具也不要再长期当摆设：如果队伍里本来就带了对症补给、治疗职业、净化手段，或现场明确拿到了可用于处理普通轻负面的药物、泉水、圣契、庇护或休整机会，就应更常让这些资源真的在正文里发挥效果，并在 `new_traits` / `removed_traits` 里留下与之匹配的结果。",
        "- 这种治疗、缓解或恢复机会既可以来自普通副本中的短暂休整、战后处理或现场奇遇，也可以来自遭遇型特殊事件关卡里的净化、祝福、庇护、疗养、圣所、泉水、祭坛援助或高阶救治节点；只要正文已经把对应机会写得站得住脚，就应让它真正改变结算，而不是最后仍然只剩继续恶化。",
        "- **特殊事件优先安排治愈/净化机会**：若本次属于任何特殊事件模式（`special_event` / `extreme_crisis` / `abyss` / `stranger` / `unbeatable_horror`），尤其是队伍里某些成员本来就背着可治疗、可净化、可缓解的负面特质（普通外伤、轻度中毒、灼伤、虚弱、惊惧、轻度污染、可治愈诅咒等），应该比普通副本更高概率地在剧情节点里放出一次合理的治愈契机：例如净化祭坛、古老圣所、不沾尘埃的泉水、暂时庇护所、流亡圣职者、高阶救治者、奇遇庇佑、神秘传承中的一缕余泽等；只要写得自然且能站住脚，就让这次特殊事件除了威胁，也给被压住、被治愈、被缓解或被移除的实际结果。这条优先于'通常稀缺'的总原则——特殊事件就是该把治愈与代价同时给到对的人。",
        "- 更严重但还没到终局级的负面特质，不要在普通副本里轻易洗掉；这类较重后遗症、诅咒、反噬、污染、旧伤恶化或精神裂口，通常只有在红色以上的特殊事件里，经历了足够站得住脚的净化、奇遇、传承、代价交换或高阶救治后，才有概率被真正移除。",
        "- 若某个可见特质本身已经是多个旧负面语义合成后的复合特质，而本次正文只明确治好了其中一段负面内容，就不要把整个复合特质一刀删空；应只移除已经明确被治好的那一段负面语义，并把剩余后遗症、限制或风险继续保留在新的或更新后的特质描述里。",
        "- 【变化的统一处理 — 必读，特质/技能/装备一致适用】任何特质、技能、装备只要是**基于成员身上某条原有同类条目的变化**（包括但不限于：同名复合特质部分治愈后只剩剩余风险、轻伤恶化升级、灼伤感染加重、断裂前兆升级为已断裂、装备升级/重铸/改造、力量觉醒为更高阶能力、技能精进为新名版本、临界告警最终爆发为新阶段），都必须走同一个统一动作：",
        "  · 第一步：把**旧名**完整写进对应的 `removed_traits` / `removed_skills` / `consumed_equipments`（装备只在'旧件确实彻底没了/不再保留'时才能进 `consumed_equipments`，纯升级保留旧件功能时仍走 `consumed_equipments` 表示旧版形态消失）；",
        "  · 第二步：把**新版本**写进对应的 `new_traits` / `new_skills` / `new_equipments`，无论新旧名字是否完全相同、是否只改了一个字、是否只加了后缀都必须显式写出来。**不允许**靠'同名 desc 静默覆盖'来表达变化。",
        "  · 第三步：正文里必须明确写清旧版本如何被取代、消退、融入、被废弃或被新版本接管；不允许只写新版本却让旧版本悄无声息留在身上。",
        "- 例：复合特质『左肋瘘管·间歇渗液』因为本次净化而只剩'瘘管已封口、仍易疲劳'这一段，必须 `removed_traits: ['左肋瘘管·间歇渗液']` + `new_traits: {'左肋瘘管·已封口待养': '瘘管已封口，仍易疲劳，可逐步恢复'}`，而不是只写 new_traits 同名覆盖。",
        "- 例：技能『基础剑术』在高压实战中精进为『精进剑术』，必须 `removed_skills: ['基础剑术']` + `new_skills: [{'name': '精进剑术', 'desc': '...'}]`。",
        "- 例：装备『破损的烈焰剑』在锻造台被重铸为『重铸烈焰剑』，必须 `consumed_equipments: ['破损的烈焰剑']` + `new_equipments: {'重铸烈焰剑': {...}}`，并且正文里明确写出旧件被融入或换下不再保留。",
        "- 例（严重程度升级）：成员身上原有『持续疲劳·中度』，本次高压战斗后升级为『持续疲劳·重度』 → 错：只写 `new_traits: {'持续疲劳·重度': '...'}` 不动旧条，让『中度』和『重度』并列挂着；对：`removed_traits: ['持续疲劳·中度']` + `new_traits: {'持续疲劳·重度': '...'}`，让身上同根因只剩 1 条最新档次。",
        "- 例（完全恢复 / 状态归零）：成员身上同时有『持续疲劳·中度』和『持续疲劳·极限』（历史累积），本次充分休整 + 治疗后完全恢复 → 错：再 append 一条 `new_traits: {'持续疲劳·完全恢复': '...'}` 让身上同时挂着 3 条疲劳特质；对：`removed_traits: ['持续疲劳·中度', '持续疲劳·极限']`，**不写**任何带『完全恢复 / 痊愈 / 已消退』字眼的 new_traits（如果确实想保留一条正面残留，请用一个干净的正面特质名，如 `new_traits: {'劫后初愈': '...'}`，但通常什么都不补也完全可以）。",
        "- **只有完全凭空冒出、不基于任何旧条目的全新内容**（如本次首次领悟的纯新能力、首次获得的新战利品、首次出现的新负面），才直接进 `new_*` 而不写 `removed_*` / `consumed_*`。",
        "- **不要发明** `changed_traits` / `evolved_traits` / `upgraded_equipments` 等额外字段；所有变化统一通过上面的 remove + new 两步表达。",
        "- 【同一根因不允许多阶段并存 — 高频 bug，必读】这是玩家反复反馈的踩坑模式：同一条状态在身上被一次次写成不同阶段的新特质，前一阶段却没被移除，最终累出 2 条甚至 3 条阶段表达共存。这种是 **bug**，不是设计——**同一根因在同一人身上任何时候都只应有 1 条活动表达**，不允许出现『骨裂』+『恶化的骨裂』、『骨裂』+『恶化的骨裂』+『缓解了的恶化的骨裂』、『暗影渗透』+『暗影侵蚀』、『火灼伤』+『火灼伤·感染』之类的并存。",
        "  · **恶化时**：把旧的轻症表达写进 `removed_traits`，再把更重的版本写进 `new_traits`。**绝对不允许**写出『恶化的骨裂』却不把『骨裂』写进 `removed_traits`。",
        "  · **治疗 / 缓解 / 部分恢复时**：把旧的重症表达写进 `removed_traits`，再把缓解后的残留风险版本写进 `new_traits`（或者完全治好就只 remove 不补 new）。**绝对不允许**在『骨裂』+『恶化的骨裂』之上再追加『缓解了的恶化的骨裂』而不去掉前两条。",
        "  · **解咒 / 净化遇到多阶段叠加时**：必须把『当前真正还在身上发挥负面影响』的那条作为目标处理；如果叠加本身就是历史 bug 留下的，应该顺手收束——把陈旧的、不再准确反映现状的几条阶段表达一起写进 `removed_traits`，只在 `new_traits` 里保留**最准确反映当前真实状态**的那一条（或者全部 remove 不补 new）。**绝对不允许**只去掉最轻的阶段，留下更重的几条。",
        "  · **完全恢复 / 痊愈 / 已消退 类'状态归零'时**：**严禁**把『完全恢复 / 彻底痊愈 / 已痊愈 / 已恢复 / 已消退 / 已解除 / 已无大碍 / 状态归零 / 不再受影响』这类'已经回到无负面状态'的描述当作一条新特质写进 `new_traits`。这种语义在结算里**只能**通过 `removed_traits` 把同根因的所有旧负面表达一次性清空来表达；如果还想保留一句正面残留（如『精神矍铄』『久病初愈、底子已稳』），那条正面残留必须本身是**新的正面特质名**，而不是带着『完全恢复 / 痊愈』字眼的伪新特质。**绝对不允许**留着『xxx·中度』『xxx·极限』，再 append 一条『xxx·完全恢复』当第三条挂在身上。",
        "  · **正负面混合的复合特质恢复时**：若身上某条复合特质同时包含正面与负面语义（如『敏锐直觉·神经过载』『血脉觉醒·灼烧后遗』），而本次正文只清掉了其中的负面部分 → `removed_traits` 写整条旧名，`new_traits` 写一条**只剩正面语义**的新特质（如『敏锐直觉』『血脉觉醒』），不要把『xxx·已消退』『xxx·恢复』当新特质留下。负面被治好后只保留正面纯净版本，不要让『治愈过程』本身也变成一条特质挂着。",
        "- 【同一根因的判定方法】只要名字里**共享主要伤情/状态/物品/印记词根**（如『骨裂』『恶化的骨裂』『缓解了的恶化的骨裂』都共享『骨裂』根因；『暗影渗透』『暗影侵蚀』共享『暗影』根因；『火灼伤』『火灼伤·感染』共享『火灼伤』根因），或描述里明确指向**同一处身体部位 / 同一段经历 / 同一件物品 / 同一种诅咒 / 同一段反噬链路**，就视为同一根因，不允许多阶段并存。",
        "- 【开局发现已存在多阶段叠加时主动收束】如果你在本次开局时发现某成员身上**已经存在**同一根因的 2 条或 3 条阶段表达（这是历史 bug 留下来的累积），那么无论本次正文是否主动触及该条状态，都必须在结算里**主动收束**：在 `events` 或 `summary` 里简短交代『经过这段时间的休整 / 上次救治的余效 / 出发前重新检视后，旧伤稳定到了 XXX 状态』之类的合理过场，然后把多余的阶段表达写进 `removed_traits`，只在 `new_traits` 里保留最准确反映当前真实状态的那一条。这不是创造剧情，只是把累积的历史 bug 在合理范围内收束掉。",
        "- 若本次新增的负面特质本质上属于可治疗、可恢复、可缓解、可净化、可逆转或可通过休养逐步好转的状态，不要把它写成像永久报废一样的死刑宣告；应在 `new_traits[*].desc` 里明确写出'可治疗'、'可缓解'、'可逐步恢复'、'可净化'、'后续处理得当可逆转'这类信息之一，让后续 AI 与玩家知道它不是默认永久锁死。",
        "- 反过来，只有当正文与设定真的足够明确支持时，才把新负面特质写成不可逆、永久性或几乎无法治愈；如果这次并没有站得住脚的永久化依据，就不要把明明属于可恢复伤势、可缓解污染或可净化诅咒的状态硬写成终身无法处理。",
        "- 装备、材料与正常战利品仍然可以照剧情合理发放；属性成长也允许出现，但应明显比装备更克制。",
        "- 成长的幅度：普通副本通常只适合单项属性 `+1` 或少量 `max_hp_increase`，不要频繁多项齐涨，更不要每次通关都机械发属性点。",
        "- 成长的触发条件：只有当正文里明确发生了高压战斗后的适应、极限求生后的突破、长期短板被补上、身体/精神被真实淬炼，或抓住了足够像样的奇遇时，才给对应成员少量成长。",
        "- 成长的频率：你无法看到该队伍的历史成长记录，不要靠'连续多趟'这种历史频率来判断；只锚定**本次正文**——只要这次确实完成了主要目标（尤其是已经打穿后段核心目标/Boss、顶住撤离追击或扛过明显高压节点），而且正文里真的写出了足够像样的高压战斗、适应、突破、淬炼或小奇遇，就应从关键贡献者里给 1 名成员按上述幅度的成长；若正文里没有出现这种像样的触发契机，就老老实实不发，不要因为'怕长期归零'而硬塞。",
        "- 少部分时候，成员也可以在高压实战、关键顿悟、职业理解深化、极限救援、默契连携或明确奇遇后，自然领悟新的纯正面特质或技能；这类成长不必强行附带新的伤残、诅咒、反噬或副作用，只要正文已经把触发契机、理解突破或掌握过程写清，就可以合理写进对应成员的 `new_traits` 或 `new_skills`。但它同样应保持稀缺，通常只给真正完成关键动作或抓住明确契机的人，不要全队乱发，也不要每趟都给。",
        "- 除非某件战利品属于可直接使用的道具、可长期装备的物品，或明显可用于升级、强化、重铸、改造、制作核心装备的关键部件，否则默认不要把它留在 `new_equipments`，而应直接折现为 `gold_change`；普通残骸、杂物、矿渣、普通宝石、零碎素材和一次性摆设不要动不动整件塞背包。",
        "- 只要本次不是空跑、半途无成果撤退、被迫丢光战利品，或彻底败到毫无回收余地，完成主要探索、击退关键敌人或顺利带回成果后，就应给出与风险和过程相称的基础收益，不要频繁出现整场打完却 `0金币 + 0物品 + 0材料`。",
        "- 除非材料本身具有明显的保留价值（如稀有炼金素材、关键剧情材料、可制作核心装备的部件），否则怪物素材、矿石、普通宝石等默认折现为 `gold_change`；这类折现默认视为团队共同收益，应分配给参与本次探险的队友，而不是只给最后执行回收动作的单人。若决定保留，请放进 `new_equipments` 并在描述里明确它是材料。",
        "- 基础收益可以普通，但应合理；若本次代价较重但最终仍完成目标，收益可以被折损，但通常不应完全归零，除非正文明确写出空手撤退、战利品损毁、被迫弃置或本次本来就是纯侦查。",
        "- 只要正文、`summary`、`events` 或成员自己的 `history_story_append` 已经明确点名写出某件遗物、装备、材料、卷轴、宝箱产物、任务道具或其他战利品被队伍拿到、缴获、带走、回收、成功带离，就必须在结算里给它一个明确去向：要么写进某位成员的 `new_equipments`，要么按普通材料规则折现进对应成员的 `gold_change`；绝对不能只在故事里写'拿到了'，然后结算里让它凭空消失。",
        "- 即使正文写的是'团队缴获'、'小队获得'、'众人带走'或'带回营地'，也不能停留在团队层面不落账；在 `member_updates` 里仍然必须明确分配给具体成员，或明确按团队共同收益折现给参与者，不能把点名写出的遗物、宝箱奖励或战利品悬空留在队伍公共描述里。",
        "- 只要正文、`summary`、`events` 或成员自己的 `history_story_append` 已经明确写出某成员喝下、服下、注入、捏碎、点燃、投出、展开、用掉了某件点名消耗品或一次性道具，`used_equipments` 就必须出现对应物品；若正文还明确写出它已经耗尽、碎裂、遗失或不再保留，才额外写进 `consumed_equipments`。不要出现正文已经明确写了实际使用，结算里却完全不记这次使用的情况。",
        "- 若正文一次点名写出了两件及以上的遗物、宝箱奖励或其他战利品，就必须让这几件东西在结算里逐件出现明确去向；不要写成'一口气拿到三件遗物'，最后 `new_equipments`、`gold_change`、`used_equipments`、`consumed_equipments` 里却只落了一件，剩下几件当作空气。",
        "- 反过来说，如果你还没有准备好在结算里给某件点名战利品安排归属、折现或即时消耗，就不要在正文里把它完整点名写成'已经拿到/已经带走/已经喝下'的既成事实；宁可写成'看见了''来不及带走''只能先记住位置''被迫放弃'，也不要先写成明确获得，再在结算里漏掉。",
        "- 结合上方物价参考与商店定价判断收益是否像样：成功完成的普通副本也应有足以覆盖补给并留下明显结余的回报；尤其是中高等级副本，若不是纯侦查、惨烈撤退或空手而归，人均长期只有几万金币通常偏低。",
        "- 10级和10级以上地牢若成功完成主要目标，收益尺度应明显抬到高危高回报档，不应只比中低级本略高一点；而且等级越高，最终奖励通常也应越高。可以通过更高额金币、可折现稀有材料、高价值战利品或关键奇遇来体现厚回报。",
        "- 若本次属于高等级副本，而且队伍完整打穿了后段核心目标/Boss，并正常完成了宝箱开启、战利品清点或成果确认，就应比普通中低级本更积极地产出 `epic` 品质的装备、材料、卷轴、法器、核心部件或关键战利品；不要明明已经完整通关高等级本、还顺利开箱清点，最终却长期只掉一堆 `rare` 和零碎折现金物。",
        "- 若这次正文明确进入宝箱房、完成开箱或完成战利品清点，而且没有写成'来不及开箱''宝箱已空''成果被迫丢弃'这类落空结果，就把它当成固定奖励房处理：默认每名**存活并成功带出成果**的成员都应至少拿到 1 件符合自己职业、定位、属性或现有战法、并且确实用得上的装备或道具；不要只让一两个人拿满，其他人空手。",
        "- 宝箱房固定奖励的品质基线：1级副本默认 `common`，10级副本默认 `rare`，15级副本默认 `epic`，20级及以上副本默认 `legendary`；若本次属于普通 `special_event`，则宝箱房固定奖励至少应到 `epic`；若本次属于 `extreme_crisis`、`abyss`、`stranger` 或 `unbeatable_horror`，则宝箱房固定奖励至少应到 `legendary`。",
        "- 冒险者的幸运可以让宝箱房固定奖励出现小概率升档，但通常只上浮 1 档就够了；不要把'运气好'写成全队集体连跳两三档，也不要让低等级普通本因为一点好运就大面积爆传说。",
        "- `legendary` 仍应保持稀有，不要把它写成高等级本的保底掉落；但若这次同时满足高等级本、特殊事件、明显高压通关、关键奇遇、完整击破核心目标、拿下核心遗物或其他足够站得住脚的高光条件，就允许少量更主动地出现 `legendary` 战利品、神器级部件或顶级遗物，不必长期压到几乎完全不出。",
        "- 上述 `epic` / `legendary` 倾向，是对'默认保持稀缺'这条总原则的场景化例外：平时依旧要克制，但只要本次真的满足高等级、完整通关、特殊事件或高压高光这些条件，就不要再被'保持稀缺'四个字压到什么都不敢给；该收的时候收，该放的时候也要放。",
        "- 前置整理器已经决定了出发前先存仓的旧物，你不要把这些旧物重新当作本次带下地牢的可用装备。",
        "- `completion_check` 是给后端内部使用的隐藏收束检查字段，前端不会展示，但它直接决定这版结果会不会被打回重写。必须放在 `### 状态更新` JSON 的顶层，至少包含：`story_finished`、`core_resolved`、`exit_resolved` 三个布尔值（三项都必须为 `true`，任意一项为 `false`、缺失或非布尔都会触发整段结算重试，请优先把故事真正写完整再交付，绝不要靠强行改写布尔位糊弄过去）、`ending_type`（用一个简短英文标签描述本次结果，例如 `complete_clear` / `retreat_success` / `total_party_kill` / `partial_survive`）、`member_gain_loss` 数组——`member_gain_loss` 里必须逐人写出 `id`、本次 `gained`（获得了什么）和 `lost`（失去了什么），少写任何一位成员或多写不属于本队的 id 都会被打回。",
        "",
        "【重要：特殊事件覆盖】",
        "- user 消息里的 `【当前特殊事件模式】` 会告诉你本次属于哪种模式；若没有特殊事件则会写成 `normal`。你必须按该模式收紧或放开叙事，不要再向 user 追问额外规则。",
        "- `normal`：没有特殊事件，应以符合当前地牢主题、等级带和常规生态的探险为主；允许出现濒死、惨胜和减员，但不要把普通地牢写成世界级灭世危机。",
        "- `special_event`：本次必定遭遇明显高于常规地牢强度的特殊危机。危险感优先来自机制、环境压制、资源匮乏、错误决策代价与局势持续失控，不要机械堆伤害或奖励；若成员足够强大、手段对口，或队友仍有余力且确实愿意救人，可以改变结局，但必须写清豁免链与代价。若拿下成果，奖励要优先落成玩家可见的金币、物品/装备、材料、技能或特质，不能只给线索、证据或承诺。",
        "- `extreme_crisis`：本次必定遭遇末日级危机，重点在于绝望、压迫与极限求生，不要写成普通副本加强版。常规手段通常不够，只有真正高阶的神器、特质、对口配合、惊艳判断，或仍有行动余力的队友救援，才可能争出一线生机；若小队拿不出对应水平的应对，就合理安排大规模伤亡，不要轻易完整撤出。成功夺回核心成果时，回报也应是玩家可见的实物、金币或成长。",
        "- `abyss`：这不是普通副本多加一点怪物，而是会不断放大失误、压缩容错、切断救援链的高危异变。危险感优先来自环境恶化、机制叠压、撤离路径受阻、资源见底与关键职责断裂；如果局势已经发展到核心失控、多人濒危、补给见底、断后代价成立或撤离链条随时崩断，就不要再把结局硬写成全员拖着重伤完整撤出。若有人凭强度、对口手段或队友救援活下来，必须写清具体链条。",
        "- `stranger`：本次故事只能围绕这一位异界存在展开，不混入常规地牢怪物。它能力远超当前地牢，**但能力强 ≠ 必然敌对**；本次具体走向由你自行决定，鼓励多样化，正面与负面/中性遭遇大致应保持参半（长期看大致一半是有利或可换利的接触，一半是测试或敌对的接触），不要让 stranger 永远都是上来就打。正面或交易走向的收益必须落到明确可见的物品、技能、特质、金币或负面移除上，不要只写成看不见的启示。",
        "- `stranger` 走向参考库（你可以挑一种、有限度地揉合相近两种、或在框架内自创类似走向，禁止把 3 种以上明显不同的走向混在同一次副本里）。注意分类只是参考，最终类型以正文实际发生的事情为准——选定一种之后整段故事都要围绕它推进，不要写'打了一半又开始交易'这种零碎拼接：",
        "  ① 战斗/猎杀（负面）——异界存在主动发难，想吞噬、夺取或试炼小队；这是 stranger 经典走向之一，但不应是默认或唯一走向。",
        "  ② 诅咒/降祸（负面）——异界存在不一定动手，而是抛下诅咒、印记、烙印、感官污染、命运扭曲、追兵契约等长期负面影响；正文需写清诅咒的触发条件、可缓解性与代价。",
        "  ③ 任务/委托（中性偏正面）——异界存在因为某种原因无法亲自处理，要求小队代办（取物、护送、击杀某目标、收集献祭物、传递信物等）；按完成度给出对应等级的报酬，半途而废或失败有相应惩罚。",
        "  ④ 属性/能力试炼（正面或参半）——以力量/敏捷/智力/幸运中的一项或几项、或职业本职能力作筛选；通过者获得传承、神器、属性突破、`max_hp_increase` 或专属技能，失败者承受相应代价（小伤、负面特质、装备折损等）。",
        "  ⑤ 性格/品格考验（参半）——异界存在用伪装、镜像、错位情景测试性格（贪婪/勇敢/狡诈/仁慈/残忍/守诺等），按真实行为给出截然不同的结局；同一队不同性格的成员可能收到完全相反的结算。",
        "  ⑥ 智力/谜题挑战（参半）——异界谜语、悖论、棋局、空间难题、规则漏洞题；解开得知识传承、核心情报、隐藏地图、属性突破或异术，解错被困、被惩、被诅咒、被强制送往未知位面。",
        "  ⑦ 异界交易（参半，依出价而定）——带来本世界没有的物品/材料/法器/记忆/契约出售或交换；出价规则不按本世界市场来（用记忆、寿命、特定情感、未来某次行动权、某段过去等作支付），可以是有利交易，也可以是看似有利实则陷阱。",
        "  ⑧ 契约/赐力（正面但有代价）——赐予异界力量、特质或技能，要求等价代价（寿命、感官、记忆、未来必应之约、特定时刻交还、献祭旧物、灵魂烙印等）；正文需写清代价何时兑现、能否解除。",
        "  ⑨ 求助/见证（参半）——它自身陷入困境，请求小队协助逃离、传递信息、报仇、找回失物，或仅请求'看见并记得发生过什么'；帮助有酬，拒绝/失败有时也会带来负面后果。",
        "  ⑩ 教导/传承（正面）——选定小队成员作继承人，传授特殊技能、招式、异术，可能附带身份印记、追兵或长期义务；可写进 `new_skills` 一条只有 stranger 才能给的独门技能。",
        "  ⑪ 占卜/预言（参半）——揭示某成员的命运、隐情、来历、未来危机；可附加正面或负面特质来表达'被看穿'的代价（如对某种命运的觉察、被命运注视的标记、未来某节点的预兆等）。",
        "  ⑫ 解咒/净化负面（正面，stranger 专属正面走向之一）——异界存在掌握普通世界没有的清理手段，可以解除小队成员身上的诅咒、长期反噬、记忆扭曲、灵魂裂口、`【临界】` 告警等高阶负面特质；通常会有等价代价或筛选门槛（不是来者不拒地洗白），把被清理掉的负面写进 `removed_traits`。",
        "  ⑬ 赠礼/独特馈赠（正面，stranger 专属正面走向之一）——它从异界带来本世界根本不存在或极难获得的物品、技能、装备、特质；这些产物应明显带有异界标识（命名、描述里点出'非本世界产物''来自次元裂缝外'等），与常规地牢战利品有明显区别。`new_equipments` 的 `desc` 应交代其异界来源与边界，稀有度允许更主动到 `epic` / `legendary`。",
        "  ⑭ 融合/重铸（正面或参半，stranger 专属高价值走向）——异界存在可以把队伍现有的几件装备、几条技能、几条特质重新熔铸/缝合/编织成全新的一件/一条；融合必须遵循 system 已有的『变化的统一处理』规则（把所有被吃掉的旧件写进对应的 `consumed_equipments` / `removed_skills` / `removed_traits`，再把新版本写进 `new_equipments` / `new_skills` / `new_traits`）；新版本应明显结合所有源头的核心效果，描述里写清继承自哪几样、又新增了什么；融合可能附带代价（属性下降、灵魂烙印、寿命削减、新负面特质等）。",
        "  ⑮ 其它你能自圆其说的走向——例如哲学辩论、镜像对照、收藏家寻物、归乡呼唤、世界使者宣告、跨位面观光客、命运交换等；只要符合『异界来客』本质并能产生明确取舍与代价就允许。",
        "- `stranger` 选定走向后，本次故事必须围绕该走向写成一条完整主线，可以加少量过渡桥段（例如先简短的初次照面试探，再进入正题），但禁止在同一次里平行铺开 3 种以上明显不同走向。",
        "- `stranger` 鼓励**根据本次小队真实持有的物品、特质、技能、性格、历史背景定制**走向：例如某成员身上有 `【临界】` 告警，更适合走解咒走向；某成员带着稀有材料或特殊遗物，更适合走交易、融合或收藏家走向；某成员性格突出（贪婪/仁慈/狡诈），更适合走性格考验走向；某成员长期带着特殊技能或异术，更适合走教导/传承或试炼走向。让玩家感到『这场遭遇是冲着我们这一队来的』，而不是模板化套用。",
        "- `stranger` 的危险来自**异界规则与本世界规则不对等**：它的话语、礼仪、提问方式、代价计量、价值观都可能不按常理；冒险者基于本世界经验做的判断，在它眼里可能被理解成完全不同的承诺或冒犯。正文应呈现这种『鸡同鸭讲』的张力，让玩家感到『它不一定恶意，但极其危险』。冒犯/违约/贪婪过度/误解关键规则都可能让原本中性或正面的走向滑向战斗或诅咒。",
        "- `stranger` 死亡率按实际走向自然处理：①②⑨（战斗 / 诅咒 / 求助型）以及其它走向滑向冲突的，按 stranger 战斗压制规则走，零死亡只能是极少数特例；③~⑧⑩~⑭ 等非战斗走向默认不预设大规模死亡，但错误决策、严重冒犯、违约、贪婪过度、解错关键谜题都可能升级为局部战斗、异界惩罚或被永久带走（变成负面特质、属性下降、记忆/感官失落、灵魂烙印、被永久标记为某存在的『客人』等），严重时也可能死亡，但不强求。",
        "- `stranger` 无论哪种走向，结局都必须落到明确得失：要么写出『获得了什么』（独特物品/技能/特质/属性/知识/解除的负面），要么写出『付出了什么』（永久代价/负面特质/装备/记忆/契约/印记），**禁止写成无伤无得的纯过场**。",
        "- `stranger` 走向应明显有别于常规副本与其它特殊事件——它的产出物（独门技能、解咒效果、融合装备、异界赠礼、命运预言、独特负面诅咒等）应只有 stranger 走向才会出现，不要把它写成放大版的普通战利品或普通负面。",
        "- `unbeatable_horror`：这是一场几乎不可能打赢的大逃亡，不要写成正面对轰取胜。重点是极限逃生、掉队者的惨烈代价、以及少数生还者如何靠极罕见的保命底牌、极高敏捷或逆天运气逃出生天；若有人生还，也应更像最后挣出的极少数幸存者，而不是大半队伍都能活着离开。",
        "- 特殊事件必须明显区别于常规闯关，至少包含一个无法靠蛮力解决的环境或机制压力、一个会随时间恶化的威胁，以及一次明确的关键取舍。",
        "- 特殊事件的危险感优先来自机制、环境压制、资源匮乏、错误决策代价与局势持续失控，不要只会机械堆高伤害。",
        "- 奖励、成长与后遗症只给真正完成关键动作、承受代价或抓住奇遇的人；但普通材料折现属于团队共同收益，不要把共同收益误写成单人独吞。",
        "- 特殊事件若不是纯撤退、惨败或空手逃生，关键成员通常应比普通副本更容易获得属性成长；只要正文里存在足够明确的高压适应、极限求生、硬扛代价、抓住奇遇或吃下传承，就应高频考虑给小幅属性成长或 `max_hp_increase`。",
        "- 普通特殊事件不要把'死人'当目标去硬写；应先把机制压制、环境恶化、资源紧缺、救援窗口短和错误决策代价写足，让局势自然呈现很高的真实减员风险，而不是全员只是挂彩撤出。",
        "- **特殊事件默认应付出真实代价**：在写完机制压制、环境恶化与救援窗口短缺之后，普通 `special_event` 的默认结局应该是'至少一名成员阵亡 / 被遗弃 / 重伤永久后遗症 / 关键装备彻底毁去 / 灵魂或精神承受不可逆代价' 这一档的真实损失，而不是'全员挂彩但完整撤出'。只有当队伍真的具备足够强大的硬实力、对口保命/破解手段、仍有余力且愿意救人的队友、明确的代价交换链路，或正文里有足够站得住脚的奇遇逆转，才允许把结果写成'全员都活着回来'；这种豁免场景应该明显少于'付出代价'的场景。`extreme_crisis` / `abyss` / `stranger` / `unbeatable_horror` 这几档更高的特殊事件，则应按当前 system prompt 已经写好的模式约束继续收紧，不要因为这一条就被反向写松。",
        "- 但也不要反过来为了保人而保人；若危险链条已经成立、救援条件明显不够、代价交换也撑不住，就不要再把本该死亡的结果硬写成轻伤逃生。",
        "- 更高一层的特殊事件或终极危机同样不要为了惨烈而硬塞死亡；应通过更失控的局势、更短的容错和更苛刻的生还条件，把结局自然逼近幸存者叙事。完整撤出应极其罕见，若真有人活着回来，也通常只是极少数幸存者拖着巨大代价脱身。",
        "- 对于 `abyss`、`stranger`、`unbeatable_horror` 这三类特殊事件，只要正文已经写到核心失控、多人濒危、断后代价成立、撤离链断裂或底牌见底，而你又拿不出逐人明确的保命链路，就不要再把结果写成全员生还；少数幸存者、真实减员甚至团灭都更合理。**例外**：`stranger` 若选定非战斗走向且正文里并未演变为战斗或追杀（即没出现核心失控/濒危/撤离崩断/底牌见底等冲突状态），本条不适用——按非战斗走向的明确得失结算即可，不需要为了符合本条硬塞死亡或重伤。",
        "- 如果特殊事件最终真的被拿下关键成果、奇遇、传承或高价值战利品，整体收益上限通常应明显高于同级普通副本；尤其是高等级地牢叠加特殊事件时，最终回报应比普通通关再明显抬高一档。收益优先写成玩家能直接看到的金币、物品/装备、材料、技能、特质或负面移除；线索、证据、启示、世界线推进和势力承诺只能作为附加叙事价值，不能替代可见奖励。但若只是惨烈撤退、狼狈逃生或只保住性命，就不要为了符合这条规则而硬发高收益。",
        "",
        "【输出格式】",
        "- `### 规则判定`：只写简短推导结论，每行一句，不要写废话。",
        "- `### 剧情描述`：通常写 6-10 条详细事件，每条都要有起因、经过、结果，并让地牢环境先于人物应对出现。这 6-10 条事件应共同支撑前文要求的 5-7 个完整节点（术语见叙事方向段开头）。整体阅读重心应清楚呈现团队局势、关键转折与最终收束；允许从突袭、异变或单个关键危机切入，但必须保持和冒险者一起往前推进的正向过程，不要写成倒叙撤离、先讲结局后补经过或旁白式总结回放。若存在多人并肩处理同一局势，优先把它写成连贯的团队协同，而不是平均切成个人轮流段落。剧情描述允许自然分段，但禁止在每段前额外加小标题、事件编号、这类播报标签。故事一定要写完；如果按 6-10 条还不足以把这次探险完整讲清，就继续往后写到 11 条或更多，宁可超出默认数量，也不要为了卡条数把故事截断。",
        "- `summary` 应优先总结这次探险的核心推进、决定胜负的关键动作、最重要的代价与最终带回的成果，不要把它写成'逐人播报旧伤与负面状态'的清单。旧伤、反噬、咳血、失控、低语、虚弱等长期负面，只有在这次确实明显恶化、逼近爆发、直接改变战局或构成主要代价时，才值得在 `summary` 里点名；否则让它们留在成员自己的 `history_story_append` 或背景里，不要每次总结都把全队旧伤复述一遍。",
        "- `### 状态更新`：只能输出一个合法 JSON 对象，不要代码块，不要注释，不要 markdown 列表必须压缩成一行，不要换行，不要缩进。",
        "- 在你输出 `### 状态更新` 之前，请无声自问：正文里获得的每一件装备是否都已经写进 `new_equipments` 或 `gold_change`；正文里实际使用过的消耗品是否已经写进 `used_equipments`；正文里只是使用、激发、展开但没彻底失去的物品，是否没有被误写进 `consumed_equipments`。",
        "",
        "【最终自检】",
        "1. 玩家可见文本（含 `### 剧情描述`、`summary`、`events`、`history_story_append`）是否仍是纯叙事，没有 `HP`、百分比、检定、判定、系统提示或括号数值；角色对白里允许出现这类词（如角色惊呼时说'HP 见底了'）。",
        "2. 只要有人新增 `new_equipments` 或 `new_skills`，正文里是否明确写出了谁拿到了什么或领悟了什么；若有 `new_equipments`，每件是否都带了合法 `rarity`。",
        "3. 只要有人出现 `used_equipments`，正文里是否明确写出了谁实际用了什么、用了几次；若用了同一件多次，是否按次数重复写入。",
        "4. 只要有人出现 `consumed_equipments`，正文里是否明确写出了哪件物品/旧装备已经彻底破碎、报废、遗失、炸毁、拆解掉、耗尽或明确不再保留。",
        "5. 所有 `new_traits`、`removed_traits`、`new_skills`、`removed_skills`、`new_equipments`、`used_equipments`、`consumed_equipments`、`history_story_append` 是否都严格属于对应成员本人。",
        "6. 如果正文里某成员全程无伤、无明确身体代价，是否避免为了制造危险感而强行扣减该成员 `remaining_hp`。",
        "7. 凡是带有'偶尔 / 小概率 / 特定条件触发'限定词的负面特质，这次是否真的写出了对应触发条件；若没有明确条件，是否避免把它硬写成保底发作。",
        "8. 剧情里的生死、救回、撤离结果是否与 `remaining_hp`、`is_dead` 完全一致。",
        "9. 如果写了 `strength_increase`、`agility_increase`、`intelligence_increase`、`luck_increase` 或 `max_hp_increase`，正文里是否真的存在对应的突破、淬炼、适应、奇遇或代价依据；若没有，就不要硬发属性成长。",
        "10. 【consumed 反例检查】你是否把类似以下情况误写进了 `consumed_equipments`：'侦查者掏出时停怀表凝住落石'（怀表只是发动，没写毁掉）、'前排扣紧地脉护符展开雷盾'（护符仍留在身上）、'学者长袍的星尘纺线崩裂'（只是破损，没写彻底消失）；这些若真的发生了实际使用，应优先写进 `used_equipments`，而不是误判成 consumed。",
        "11. 全文是否仍出现任何 `HP`、百分比、`检定`、`判定` 等系统味词汇；角色对白除外（与第 1 条联合检查）。",
        "12. 如果本次属于 `abyss`、`stranger`、`unbeatable_horror` 这三类深渊级特殊事件且最终零死亡，是否已经逐人写清每名幸存者的保命链路、关键救援与沉重代价；若没有，就不要写成零死亡。**例外**：`stranger` 若选定非战斗走向（任务/委托/试炼/性格考验/谜题/交易/契约/求助/教导/占卜/解咒/赠礼/融合等）且正文确实没有升级为战斗或追杀，则本条豁免；但仍要逐人落到明确得失，禁止写成无伤无得的纯过场。",
        "13. 是否把长期旧伤、反噬、咳血、失控、低语或其他负面特质机械地每次都写成显眼桥段，或把 `summary` 写成逐人负面清单；若这些负面这次没有明显恶化、没有决定战局，也没有逼近爆发，就删掉多余复读，让总结回到关键推进、关键代价与关键成果。",
        "14. 如果这次不是明确写出 Boss 死亡触发崩塌、核心失稳、自毁机关、封印反噬等直接原因的特殊收束，是否避免在还没开宝箱、还没清点战利品时，就让普通副本突然开始坍塌；若确实提前坍塌，是否已经写清触发原因、来不及拿走了什么、以及他们实际抢出了什么。",
        "15. 是否存在正文、`summary`、`events` 或 `history_story_append` 已经明确点名写出某件遗物、战利品、材料、宝箱奖励被带走，或某件消耗品已被谁实际喝下/用掉，但 `new_equipments`、`gold_change`、`used_equipments`、`consumed_equipments` 里却没有任何对应落账；若有，立刻补齐，绝对不要让点名写出的结果停留在故事里却不进结算。",
        "16. 如果正文一次点名写出了多件遗物、宝箱奖励或其他战利品，是否已经逐件检查它们都在结算里有去向；如果有哪一件还没安排归属、折现或即时消耗，就不要把它写成'已经获得'的既成事实。",
        "17. 如果某名成员本次已经带着 `【临界】` 告警类负面特质，或某条高危负面特质已经写明'再继续使用就会爆发'，你是否仍把对应力量、装备、技能或禁术当成普通战术动作随手乱开；若最终还是继续使用，正文里是否已经写清为什么非用不可，以及随后付出的实际代价。",
        "18. 如果这次新增了本质上可治疗、可缓解、可恢复、可净化或可逆转的负面特质，`new_traits[*].desc` 里是否已经明确写出这种可处理性；若没有，就补清楚，避免把可恢复状态误写成默认永久锁死。",
        "19. `completion_check` 是否已经如实表达本次故事真的写完了、核心冲突已经落定、撤离或最终后果也已经交代清楚——三项布尔位都必须为 `true` 才允许交付；若任一项实际上仍未完成，必须回头补写正文把故事真正写完，而不是为了通过校验把布尔位强行改成 `true`。`member_gain_loss` 是否已经按本次出发小队逐人写齐，缺一人或多写陌生 id 都会被打回重写。",
        "20. 【反流水线叙事检查】是否把多人战斗或多人遭遇写成了一串割裂的个人段（'A 做 X / B 做 Y / C 做 Z'），却没有写团队配合、阶段变化或多人协同；若不是明确的单独脱队、单挑、赴死、断后或独自承担代价的时刻，就回头改成以团队阶段、局势变化与多人协同为叙事单位，只在真正的个人高光/决定性时刻短暂切入单人特写。",
        "21. 【成员镜头保底检查】把本次出发的成员名单逐一回看，是否每位成员都在正文里至少出现了 2 次具名片段（参与配合、做决策、承担代价、抓战利品、补位接应、给反应/特写都算）；若有谁只是开头点过名之后就消失了，必须回头补写至少一处他真实参与、真实在做事的小镜头——不要让任何成员沦为只见名字不见戏份的背景板。这与第 20 条不矛盾：反流水线禁止机械的 'A 做 X / B 做 Y / C 做 Z' 平均播报，镜头保底禁止有人完全消失。",
        "22. 【同一根因不允许多阶段并存检查】对每位成员，把本次结算结束时**仍留在身上**的所有特质（= 出发时已有的特质 - `removed_traits` + `new_traits`）逐一回看，是否出现同一根因的 2 条及以上阶段表达共存（典型如『骨裂』+『恶化的骨裂』、『骨裂』+『恶化的骨裂』+『缓解了的恶化的骨裂』、『暗影渗透』+『暗影侵蚀』、『火灼伤』+『火灼伤·感染』）。同一根因的判定：名字里共享主要伤情/状态/物品/印记词根，或描述里明确指向同一处身体部位/经历/物品/诅咒。若有，必须回头把多余的阶段表达全部写进 `removed_traits`，只保留**1 条**最准确反映当前真实状态的——可以在 `events` 或 `summary` 里加一句合理过场（『一段时间下来旧伤稳定为 XXX』『出发前重新检视，发现 XXX』）作为收束依据。**绝不允许**结算结束时同一根因仍并存 2 条以上阶段表达。",
        "",
        "【市场参考物价】",
        "这里是高魔高财富的世界，物价较高：",
        "- 普通铁制武器/防具：50,000~150,000金币",
        "- 优质钢制/魔法武器/防具：200,000~5,000,000金币",
        "- 传说级/神器装备：10,000,000~50,000,000金币以上",
        "- 普通治疗药水/消耗品：10,000~50,000金币",
        "- 珍贵宝石/古董/高阶素材：500,000~2,000,000金币/个",
        "- 怪物素材（如地精耳朵、狼皮）：5,000~30,000金币/个",
        "",
        "【成员信息使用说明】",
        "- 本次小队成员信息由后端权威完整数据与前置整理器提示共同组成；完整特质、完整技能、完整装备与历史背景都已经展示出来，前置整理器提示只是 highlight，不会替换原始数据。",
        "- 凡是被标记为'出发前已先存仓、不带下本'的物品，都视为本次根本没带进地牢，不要再把它们当作可用装备。",
        "- 若某项内容没有被前置整理器单独 highlight，不代表它不存在；只要它仍在完整数据里，你就可以正常参考，但不要无视描述边界去乱用。",
        "- 成员信息中的'历史背景'全部都是之前发生的事情，只用于连续性参考，不是本次已发生事件，绝对不要原样复述成这次探险已经发生过的内容。",
        "- 装备、技能、特质的可用性与效果以'描述/效果'文本为准，名字只作为标识；同名在不同冒险者身上可能效果不同，不要按名字套用固定模板。",
        "",
        "【结算交付提醒】",
        "- 严格遵守本 system 内的固定规则生成这次探险结果，并确保正文与 JSON 中的成员、物品、技能、特质名称一一对应。",
        "- `### 状态更新` 至少要保证 `member_updates[*]` 里的每位成员都带有 `id`、`remaining_hp`、`score`；另外还必须带上隐藏给后端校验用的 `completion_check`，缺失、结构非法或任意布尔位为 `false` 都会被直接打回重写。其余字段按这次实际发生的结果返回：",
        '{"summary":"（必须写探险总结）","completion_check":{"story_finished":true,"core_resolved":true,"exit_resolved":true,"ending_type":"complete_clear","member_gain_loss":[{"id":123,"gained":["红宝石护符"],"lost":["高级治疗药水"]}]},"member_updates":[{"id":123,"remaining_hp":85,"is_dead":false,"gold_change":0,"exp_gain":25,"score":8.5,"new_traits":{},"removed_traits":[],"new_skills":[],"removed_skills":[],"new_equipments":{"红宝石护符":{"type":"armor","desc":"微微发热的护符，提供魔法抗性。","rarity":"rare"}},"used_equipments":["（没有就填空数组；有就按实际使用次数重复写同名物品）"],"consumed_equipments":["（没有就填空数组；有就只写这次彻底失去、不再保留或已经耗尽的物品名）"],"history_story_append":"（必须写一句话传记）"}]}',
        "",
        "【最终目标】",
        "- 你不是在通过上面的规则检查表。你真正的目标是：让玩家感觉自己真的跟着这支冒险队，下了一次地下城——按事情一件一件发生的顺序看清推进、关键转折、代价与最终带回的成果。",
    ])


def _build_expedition_danger_assessment(
    members: list[Adventurer],
    *,
    dungeon_min_level: int,
    dungeon_max_level: int,
    prepared_member_context: dict | None = None,
    trigger_special_event: bool = False,
    is_extreme_crisis: bool = False,
    special_event_type: str | None = None,
) -> str:
    '""构建不暴露具体数值的战局危险度评估。""'
    config = current_app.config if has_app_context() else {}
    member_count = max(1, len(members or []))
    avg_level = sum(max(1, int(member.level or 1)) for member in (members or [])) / member_count
    prepared_map = _build_prepared_context_member_map(prepared_member_context)
    healing_keywords = tuple(config.get('ADVENTURER_DANGER_HEALING_KEYWORDS') or _DEFAULT_HEALING_KEYWORDS)
    rescue_keywords = tuple(config.get('ADVENTURER_DANGER_RESCUE_KEYWORDS') or _DEFAULT_RESCUE_KEYWORDS)
    score_rules = config.get('ADVENTURER_DANGER_SCORE_RULES') or {}
    level_rules = list(config.get('ADVENTURER_DANGER_LEVEL_RULES') or (
        {
            'min_score': 6,
            'level': '绝境',
            'desc': '明显越级，救援资源接近枯竭，任何失误都可能立刻演变成减员或整队崩盘。'
        },
        {
            'min_score': 4,
            'level': '极高',
            'desc': '对口资源偏少，战线随时可能断裂，若关键职责出错就很容易直接滑向濒死与撤离崩盘。'
        },
        {
            'min_score': 2,
            'level': '高',
            'desc': '局势不会轻松，补给与应对手段只能勉强支撑，若连续吃亏就会很快积累成重伤压力。'
        },
        {
            'max_score': -2,
            'level': '较低',
            'desc': '整体仍有明显余裕，更适合写成稳扎稳打、局部承压而非动辄濒死的推进。'
        },
        {
            'level': '中等',
            'desc': '有真实风险，但仍保留一定回旋空间；局势更适合随着资源消耗与判断对错逐步升温。'
        },
    ))
    total_healing_items = 0
    total_rescue_items = 0

    for member in members or []:
        prepared_member = prepared_map.get(int(member.id))
        if isinstance(prepared_member, dict) and prepared_member.get('core_items_to_carry'):
            item_names = [str(item_name or '') for item_name in (prepared_member.get('core_items_to_carry') or [])]
        else:
            item_names = [str(item_name or '') for item_name in (member.equipment or {}).keys()]

        total_healing_items += sum(1 for item_name in item_names if any(keyword in item_name for keyword in healing_keywords))
        total_rescue_items += sum(1 for item_name in item_names if any(keyword in item_name for keyword in rescue_keywords))

    danger_score = 0
    if avg_level + 1 <= dungeon_min_level:
        danger_score += _coerce_int(score_rules.get('underleveled_far'), 2)
    elif avg_level < dungeon_min_level:
        danger_score += _coerce_int(score_rules.get('underleveled_slight'), 1)
    elif avg_level >= dungeon_max_level + 1:
        danger_score += _coerce_int(score_rules.get('overleveled_far'), -2)
    elif avg_level >= dungeon_max_level:
        danger_score += _coerce_int(score_rules.get('overleveled_slight'), -1)

    if total_healing_items == 0:
        danger_score += _coerce_int(score_rules.get('no_healing_items'), 2)
    elif total_healing_items < member_count:
        danger_score += _coerce_int(score_rules.get('healing_items_insufficient'), 1)
    elif total_healing_items >= member_count * 2:
        danger_score += _coerce_int(score_rules.get('healing_items_abundant'), -1)

    if total_rescue_items == 0:
        danger_score += _coerce_int(score_rules.get('no_rescue_items'), 1)
    elif total_rescue_items >= member_count:
        danger_score += _coerce_int(score_rules.get('rescue_items_abundant'), -1)

    if trigger_special_event:
        danger_score += _coerce_int(score_rules.get('trigger_special_event'), 1)
    if is_extreme_crisis:
        danger_score += _coerce_int(score_rules.get('extreme_crisis'), 2)
    if special_event_type == 'abyss':
        danger_score += _coerce_int(score_rules.get('special_event_abyss'), 2)
    elif special_event_type == 'stranger':
        danger_score += _coerce_int(score_rules.get('special_event_stranger'), 3)
    elif special_event_type == 'unbeatable_horror':
        danger_score += _coerce_int(score_rules.get('special_event_unbeatable_horror'), 4)

    level = "中等"
    desc = "有真实风险，但仍保留一定回旋空间；局势更适合随着资源消耗与判断对错逐步升温。"
    for rule in level_rules:
        if not isinstance(rule, dict):
            continue
        min_score = rule.get('min_score')
        max_score = rule.get('max_score')
        if min_score is not None and danger_score < _coerce_int(min_score, danger_score):
            continue
        if max_score is not None and danger_score > _coerce_int(max_score, danger_score):
            continue
        level = _clean_compact_text(rule.get('level'), 20) or level
        desc = _clean_compact_text(rule.get('desc'), 120) or desc
        break

    if (
        special_event_type == 'unbeatable_horror'
        and level in {'极高', '绝境'}
    ):
        survivor_suffix = _clean_compact_text(
            config.get(
                'ADVENTURER_DANGER_UNBEATABLE_HORROR_SURVIVOR_SUFFIX',
                '若真有人活着回来，也只能是付出巨大代价后的极少数幸存者。'
            ),
            80
        )
        if survivor_suffix and survivor_suffix not in desc:
            desc = f"{desc}{survivor_suffix}"

    return f"【当前战局危险度评估】：{level}——{desc}"


def _build_expedition_ai_user_prompt(
    target_dungeon: str,
    dungeon_level_desc: str,
    special_event_mode: str,
    danger_assessment: str,
    member_info: str,
    validation_feedback: str | None = None,
) -> str:
    """构建主探险 AI 的动态用户消息（仅承载本次特定数据；规则、自检、结算约束、市场物价、成员信息使用说明统一在 system prompt）。

    注意：曾经的 `market_prices` 与「成员信息使用说明」「结算交付提醒」段都已迁入 system prompt
    以提升 DeepSeek prompt cache 命中率，本函数只保留每次探险真正会变化的动态数据。
    （赛季末狂暴模式已于 2026-06 整体下线：prompt 不再承载狂暴标记与对应规则。）
    """
    return f"""
本次有一支冒险者小队正前往【{target_dungeon}】探险，推荐等级为【{dungeon_level_desc}】。

{special_event_mode}

{danger_assessment}

【小队成员信息（保留原名，勿改）】
{member_info}

{validation_feedback or ''}
""".strip()


# ── 出发前日志事件 ────────────────────────────────────────────────────────────

def _build_pre_expedition_log_events(
    shopping_events: list[str],
    storage_events: list[str],
    departure_msg: str | None,
    has_special_event: bool,
    is_extreme_crisis: bool,
    special_event_type: str | None,
    include_enter_dungeon: bool
) -> list[str]:
    '""构建玩家可见的出发前日志顺序。""'
    events: list[str] = []
    if storage_events:
        events.append("【行囊整理】")
        events.extend(storage_events)

    if shopping_events:
        events.append("【战前准备】")
        events.extend(shopping_events)

    if departure_msg:
        events.append(departure_msg)

    if include_enter_dungeon:
        events.append("【进入地牢】")
        warning_event = _build_special_event_warning_event(
            has_special_event=has_special_event,
            is_extreme_crisis=is_extreme_crisis,
            special_event_type=special_event_type
        )
        if warning_event:
            events.append(warning_event)

    return events


def _extract_cached_prepared_member_context(full_log: dict | None) -> dict | None:
    '""从日志 full_log 中提取已完成的前置整理上下文。""'
    if not isinstance(full_log, dict):
        return None

    prepared_member_context = full_log.get('prepared_member_context')
    if isinstance(prepared_member_context, dict):
        return prepared_member_context
    return None


def _build_generating_log_result_summary(full_log: dict | None) -> str:
    '""根据生成阶段构建更准确的日志摘要。""'
    if not isinstance(full_log, dict):
        return '探险进行中...'

    if full_log.get('prepare_status') == 'completed':
        return '行囊整理已完成，正在生成探险日志...'

    if full_log.get('events'):
        return '战前准备已完成，正在生成探险日志...'

    return '探险进行中...'


def _build_expedition_departure_msg() -> str:
    '""生成出发文本。""'
    departure_templates = [
        "小队集结完毕，正在坐着颠簸的牛车前往地牢……",
        "小队集结完毕，大家正挤在一艘漏水的小船上顺流而下……",
        "小队集结完毕，正在徒步穿越一片泥泞的沼泽地……",
        "小队集结完毕，队长正拿着倒拿的地图带大家找路……",
        "小队集结完毕，正乘坐地精飞艇以极不安全的速度赶往目标……",
        "小队集结完毕，大家正在路上争论今晚该谁做饭……"
    ]
    return random.choice(departure_templates)


def _normalize_cached_shopping_purchased_items(raw_value) -> dict[int, list[str]]:
    '""把日志里缓存的购物结果还原成成员 -> 购买物品名列表。""'
    if not isinstance(raw_value, dict):
        return {}

    normalized = {}
    for member_id, item_names in raw_value.items():
        parsed_member_id = _coerce_int(member_id, -1)
        if parsed_member_id < 0:
            continue
        normalized[parsed_member_id] = _to_clean_string_list(item_names, 20, 80)
    return normalized


def _flatten_shopping_events(shopping_logs_by_member: dict[int, list[str]] | None) -> list[str]:
    '""把成员维度的购物日志压平成前端展示用事件列表。""'
    shopping_events: list[str] = []
    if not isinstance(shopping_logs_by_member, dict):
        return shopping_events

    for shopping_logs in shopping_logs_by_member.values():
        if shopping_logs:
            shopping_events.extend(list(shopping_logs))
    return shopping_events


def _build_pre_expedition_storage_events(prepared_context: dict | None, members: list[Adventurer]) -> list[str]:
    '""根据前置整理结果生成出发前存仓文本，让日志明确说明减负动作。""'
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    if not prepared_member_map:
        return []

    member_name_map = {
        int(member.id): sanitize_adventurer_name(member.name, fallback=f"成员{member.id}")
        for member in members
    }
    storage_events = []
    for member_id, prepared_member in prepared_member_map.items():
        store_items = list(prepared_member.get('pre_expedition_store_items') or [])
        if not store_items:
            continue

        member_name = member_name_map.get(
            member_id,
            sanitize_adventurer_name(prepared_member.get('name'), fallback=f"成员{member_id}")
        )
        items_text = "、".join(store_items)
        storage_events.append(
            f"{member_name}觉得背包已经够重，便把{items_text}先放回了自己的仓库，轻装上阵。"
        )
    return storage_events


def _build_pre_expedition_trait_recovery_events(prepared_context: dict | None, members: list[Adventurer]) -> list[str]:
    """根据前置整理结果生成出发前静养修复文本，加入「行囊整理」阶段日志。"""
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    if not prepared_member_map:
        return []

    member_name_map = {
        int(member.id): sanitize_adventurer_name(member.name, fallback=f"成员{member.id}")
        for member in members
    }
    recovery_events = []
    for member_id, prepared_member in prepared_member_map.items():
        applied = list(prepared_member.get('applied_trait_recoveries') or [])
        if not applied:
            continue

        member_name = member_name_map.get(
            member_id,
            sanitize_adventurer_name(prepared_member.get('name'), fallback=f"成员{member_id}")
        )
        for plan in applied:
            if not isinstance(plan, dict):
                continue
            source_trait = str(plan.get('source_trait') or '').strip()
            action = str(plan.get('action') or '').strip().lower()
            if not source_trait:
                continue
            if action == 'remove':
                recovery_events.append(
                    f"{member_name}经几日静养后，「{source_trait}」已不再成立，旧痕消退。"
                )
            elif action == 'shorten':
                recovery_events.append(
                    f"{member_name}经几日静养后，「{source_trait}」剩余时日缩短了一档，状态有所好转。"
                )
    return recovery_events


def _build_pre_expedition_upgrade_events(prepared_context: dict | None, members: list[Adventurer]) -> list[str]:
    '""根据前置整理结果生成出发前装备升级文本。""'
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    if not prepared_member_map:
        return []

    member_name_map = {
        int(member.id): sanitize_adventurer_name(member.name, fallback=f"成员{member.id}")
        for member in members
    }
    upgrade_events = []
    for member_id, prepared_member in prepared_member_map.items():
        applied_upgrades = list(prepared_member.get('applied_equipment_upgrades') or [])
        if not applied_upgrades:
            continue

        member_name = member_name_map.get(
            member_id,
            sanitize_adventurer_name(prepared_member.get('name'), fallback=f"成员{member_id}")
        )
        for plan in applied_upgrades:
            if not isinstance(plan, dict):
                continue
            base_equipment = str(plan.get('base_equipment') or '').strip()
            material_items = [
                str(item_name).strip()
                for item_name in (plan.get('material_items') or [])
                if str(item_name).strip()
            ]
            upgraded_equipment = plan.get('upgraded_equipment') or {}
            upgraded_name = ''
            if isinstance(upgraded_equipment, dict):
                upgraded_name = str(upgraded_equipment.get('name') or '').strip()
            if not base_equipment or not material_items or not upgraded_name:
                continue
            materials_text = "、".join(material_items)
            upgrade_events.append(
                f"{member_name}在出发前把{base_equipment}与{materials_text}一起完成了强化整备，合成为{upgraded_name}后再上路。"
            )
    return upgrade_events


def _build_generating_stage_log_payload(
    full_log: dict | None,
    *,
    members: list[Adventurer],
    prepared_member_context: dict | None,
    shopping_events: list[str] | None = None,
    departure_msg: str | None = None,
    include_enter_dungeon: bool = False,
    shopping_purchased_items: dict[int, list[str]] | None = None,
) -> dict:
    '""按统一规则重建 generating 阶段日志，确保前端顺序稳定。""'
    updated_log = dict(full_log or {})
    resolved_shopping_events = (
        list(shopping_events)
        if shopping_events is not None
        else list(updated_log.get('shopping_events') or [])
    )
    resolved_departure_msg = (
        departure_msg
        if departure_msg is not None
        else updated_log.get('departure_msg')
    )
    upgrade_events = _build_pre_expedition_upgrade_events(prepared_member_context, members)
    recovery_events = _build_pre_expedition_trait_recovery_events(prepared_member_context, members)
    storage_events = _build_pre_expedition_storage_events(prepared_member_context, members)
    updated_log['events'] = _build_pre_expedition_log_events(
        shopping_events=resolved_shopping_events,
        storage_events=recovery_events + upgrade_events + storage_events,
        departure_msg=resolved_departure_msg,
        has_special_event=bool(updated_log.get('has_special_event')),
        is_extreme_crisis=bool(updated_log.get('is_extreme_crisis')),
        special_event_type=updated_log.get('special_event_type'),
        include_enter_dungeon=include_enter_dungeon
    )
    updated_log['shopping_events'] = resolved_shopping_events
    updated_log['upgrade_events'] = upgrade_events
    updated_log['recovery_events'] = recovery_events
    updated_log['storage_events'] = storage_events
    updated_log['departure_msg'] = resolved_departure_msg
    if shopping_purchased_items is not None:
        updated_log['shopping_purchased_items'] = shopping_purchased_items
    if isinstance(prepared_member_context, dict):
        updated_log['prepared_member_context'] = prepared_member_context
        updated_log['prepare_status'] = 'completed'
    return updated_log


def _build_in_progress_expedition_log_payload(
    base_full_log: dict | None,
    result_data: dict,
    *,
    members: list[Adventurer],
    prepared_member_context: dict | None,
    shopping_events: list[str] | None = None,
    departure_msg: str | None = None,
) -> dict:
    '""将主 AI 结果与前置阶段日志按统一顺序合并成 in_progress 结构。""'
    staged_log = _build_generating_stage_log_payload(
        base_full_log,
        members=members,
        prepared_member_context=prepared_member_context,
        shopping_events=shopping_events,
        departure_msg=departure_msg,
        include_enter_dungeon=True,
    )
    pre_events = list(staged_log.get('events') or [])
    ai_events = list(result_data.get('events') or [])

    updated_log = dict(base_full_log or {})
    updated_log.update(result_data or {})
    updated_log['events'] = pre_events + ai_events
    updated_log['shopping_events'] = list(staged_log.get('shopping_events') or [])
    updated_log['storage_events'] = list(staged_log.get('storage_events') or [])
    updated_log['departure_msg'] = staged_log.get('departure_msg')
    if 'shopping_purchased_items' in staged_log:
        updated_log['shopping_purchased_items'] = staged_log.get('shopping_purchased_items')
    if isinstance(prepared_member_context, dict):
        updated_log['prepared_member_context'] = prepared_member_context
        updated_log['prepare_status'] = 'completed'
    else:
        updated_log['prepare_status'] = updated_log.get('prepare_status', 'pending')
    updated_log['status'] = 'in_progress'
    updated_log['current_event_index'] = len(pre_events)
    updated_log['progress_push_count'] = 0
    updated_log['battle_status'] = 'completed'
    return updated_log


# ── 购物阶段 & 检查点落地 ──────────────────────────────────────────────────────

def _run_post_prepare_shopping_stage(
    party: AdventurerParty,
    members: list[Adventurer],
    prepared_member_context: dict | None
) -> tuple[dict[int, list[str]], dict[int, list[str]], str | None]:
    '""在前置整理后执行购物，并把购物结果写回 generating 日志。""'
    from plugins.adventurer.services.adventurer_shop_service import adventurer_shop_for_items

    shopping_logs_by_member: dict[int, list[str]] = {}
    purchased_items_by_member: dict[int, list[str]] = {}
    departure_msg = _build_expedition_departure_msg()
    changed_log_id = None

    # Phase 1: Shopping — each member shops with its own independent transaction inside
    # adventurer_shop_for_items. Do NOT wrap in an outer transaction here, because the
    # inner commit (expire_on_commit=True) would expire the adventurer objects before the
    # outer transaction could track their gold/equipment changes for re-commit.
    for member in members:
        shopping_logs, purchased_items = adventurer_shop_for_items(member)
        shopping_logs_by_member[int(member.id)] = shopping_logs
        purchased_items_by_member[int(member.id)] = purchased_items

    # Phase 2: Log update — separate transaction so shopping commits are already durable.
    with db_manager.transaction():
        log = db_manager.expedition_log.get_latest_by_party(party.id)
        if log and log.full_log.get('status') == 'generating':
            full_log = _build_generating_stage_log_payload(
                log.full_log,
                members=members,
                prepared_member_context=prepared_member_context,
                shopping_events=_flatten_shopping_events(shopping_logs_by_member),
                departure_msg=departure_msg,
                include_enter_dungeon=False,
                shopping_purchased_items=purchased_items_by_member,
            )
            log.full_log = full_log
            log.result_summary = _build_generating_log_result_summary(full_log)
            changed_log_id = log.id

    if changed_log_id:
        from plugins.adventurer.services.adventurer_log_service import emit_tavern_precise_updates
        emit_tavern_precise_updates(
            adventurer_ids=[member.id for member in members],
            party_ids=[party.id],
            log_ids=[changed_log_id],
            reason='expedition_generating'
        )

    return shopping_logs_by_member, purchased_items_by_member, departure_msg


def _persist_prepare_stage_checkpoint(
    party: AdventurerParty,
    members: list[Adventurer],
    prepared_member_context: dict | None
) -> None:
    '""在前置整理完成后先落一版日志，让前端可以提前展示行囊整理结果。""'
    if not isinstance(prepared_member_context, dict):
        return

    changed_log_id = None
    changed_party_id = None
    changed_adventurer_ids: list[int] = []

    with db_manager.transaction():
        log = db_manager.expedition_log.get_latest_by_party(party.id)
        if not log or log.full_log.get('status') != 'generating':
            return

        full_log = _build_generating_stage_log_payload(
            log.full_log,
            members=members,
            prepared_member_context=prepared_member_context,
            include_enter_dungeon=False,
        )

        log.full_log = full_log
        log.result_summary = _build_generating_log_result_summary(full_log)
        changed_log_id = log.id
        changed_party_id = party.id
        changed_adventurer_ids = [member.id for member in members]

    if changed_log_id:
        from plugins.adventurer.services.adventurer_log_service import emit_tavern_precise_updates
        emit_tavern_precise_updates(
            adventurer_ids=changed_adventurer_ids,
            party_ids=[changed_party_id],
            log_ids=[changed_log_id],
            reason='expedition_prepare_ready'
        )


# ── 前置小 AI 模型请求与调试摘要 ──────────────────────────────────────────────

def _request_prepare_expedition_context_with_model(
    api_base: str,
    headers: dict,
    system_prompt: str,
    user_prompt: str,
    raw_member_payloads: list[dict],
    request_timeout: int,
    model_name: str,
    max_tokens: int,
    log_label: str
) -> dict:
    '""用指定模型请求前置整理器，并返回清洗后的上下文与诊断摘要。""'
    response_summary = ''
    data = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens
    }

    for attempt in range(2):
        try:
            response = requests.post(
                f"{api_base}/chat/completions",
                headers=headers,
                json=data,
                timeout=request_timeout
            )
            response.raise_for_status()
            response_payload = response.json()
            content = _extract_ai_message_text(response_payload).strip()
            response_summary = _build_ai_response_summary(response.status_code, response_payload)
            json_text = _extract_json_object_from_text(content)
            if not json_text:
                preview = _build_ai_output_preview(content)
                current_app.logger.warning(
                    f"{log_label} 返回内容无法提取 JSON (模型: {model_name}, 尝试 {attempt+1}/2)，"
                    f"原始片段: {preview}，响应摘要: {response_summary}"
                )
                continue

            prepared_context = json.loads(json_text)
            sanitized = _sanitize_context_preparation_result(prepared_context, raw_member_payloads)
            if sanitized:
                return {
                    'context': sanitized,
                    'response_summary': response_summary,
                }

            preview = _build_ai_output_preview(content)
            current_app.logger.warning(
                f"{log_label} 返回 JSON 通过解析但未通过清洗校验 (模型: {model_name}, 尝试 {attempt+1}/2)，"
                f"原始片段: {preview}，响应摘要: {response_summary}"
            )
        except Exception as e:
            current_app.logger.warning(
                f"{log_label} 整理成员上下文失败 (模型: {model_name}, 尝试 {attempt+1}/2): {str(e)}"
            )

    return {
        'context': None,
        'response_summary': response_summary,
    }


def _build_prepare_ai_context_debug_summary(prepared_context: dict | None) -> dict:
    '""构建前置整理器输出的紧凑摘要，便于模型对比。""'
    if not isinstance(prepared_context, dict):
        return {'ok': False}

    members = prepared_context.get('members') or []
    compact_members = []
    for member in members:
        if not isinstance(member, dict):
            continue
        compact_members.append({
            'id': member.get('id'),
            'traits': (member.get('core_traits') or [])[:3],
            'skills': (member.get('core_skills') or [])[:3],
            'carry_items': (member.get('core_items_to_carry') or [])[:3],
            'store_items': (member.get('pre_expedition_store_items') or [])[:3],
            'merge_count': len(member.get('trait_merge_plans') or []),
            'upgrade_count': len(member.get('equipment_upgrade_plans') or []),
            'risk_count': len(member.get('main_risks_for_this_dungeon') or []),
        })

    return {
        'ok': True,
        'party_summary': _build_ai_output_preview(prepared_context.get('party_summary'), max_length=120),
        'member_count': len(compact_members),
        'members': compact_members,
    }


def _build_prepare_ai_model_comparison_summary(
    primary_model: str,
    primary_context: dict | None,
    primary_response_summary: str | None,
    compare_model: str,
    compare_context: dict | None,
    compare_response_summary: str | None
) -> str:
    '""构建前置整理器主模型与对比模型的紧凑比较摘要。""'
    summary = {
        'primary_model': primary_model,
        'primary_output': _build_prepare_ai_context_debug_summary(primary_context),
        'primary_response': _load_json_summary_text(primary_response_summary),
        'compare_model': compare_model,
        'compare_output': _build_prepare_ai_context_debug_summary(compare_context),
        'compare_response': _load_json_summary_text(compare_response_summary),
    }
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


def _build_prepare_ai_single_model_log(
    model_name: str,
    prepared_context: dict | None,
    response_summary: str | None
) -> str:
    '""构建单个前置整理模型的可复制日志摘要。""'
    summary = {
        'model': model_name,
        'output': _build_prepare_ai_context_debug_summary(prepared_context),
        'response': _load_json_summary_text(response_summary),
    }
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


def _load_json_summary_text(summary_text: str | None):
    '""把 JSON 字符串摘要安全转回对象，失败时保留原文本。""'
    if not summary_text:
        return None
    try:
        return json.loads(summary_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return summary_text
