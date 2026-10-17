"""S1 玩家可见名称的机械边界与已确认历史别名。

这里只识别结构形态和精确旧值，不根据描述或关键词猜测名称语义。
"""
from __future__ import annotations

import re


_INTERNAL_CODE_IDENTIFIER_RE = re.compile(
    r'^(?:'
    r'[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+'
    r'|[a-z][a-z0-9]*(?:[A-Z][A-Za-z0-9]*)+'
    r')$',
)
_FILE_PATH_RE = re.compile(r'^(?:[A-Za-z]:)?[^ \t\r\n]+[\\/][^ \t\r\n]+$')
_VISIBLE_NAME_STRUCTURE_RE = re.compile(r'[{}\[\]]')
_RESERVED_VISIBLE_NAMES = frozenset({
    'name', 'desc', 'description', 'type', 'rarity', 'quantity', 'uses_left',
    'new_equipments', 'new_traits', 'new_skills',
    'removed_equipments', 'consumed_equipments', 'removed_traits',
    'removed_skills', 'used_skills', 'member_updates', 'remaining_hp',
    'gold_change', 'completion_check', 'summary', 'events', 'prompt',
    'system', 'user', 'assistant', 'history_story_append', 'lore_append',
})

# 已由探险日志 #8994 的物品描述、成员归属和结算收据逐项确认。
# 原始日志与历史流水保持不变；公开展示和当前资产使用右侧稳定名称。
LEGACY_VISIBLE_NAME_ALIASES = {
    'recorded_pulse_frequency_data': '远古晶核脉冲频率记录',
    'recorded_crystal_ward_network_data': '晶卫感知网络弱点记录',
    'recorded_crystal_ward_jamming_data': '晶卫感知网络干扰记录',
}


def is_internal_code_identifier_name(value: object) -> bool:
    """名称是否是 snake_case / lowerCamelCase 内部标识符形态。"""
    return bool(_INTERNAL_CODE_IDENTIFIER_RE.fullmatch(str(value or '').strip()))


def visible_name_pollution_reason(value: object) -> str | None:
    """返回玩家可见名称的纯结构污染原因，不推断自然语言语义。"""
    name = str(value or '').strip()
    if not name:
        return '名称为空'
    if len(name) > 100:
        return '名称超过 100 字符存储上限'
    lowered = name.lower()
    if lowered in _RESERVED_VISIBLE_NAMES:
        return '名称等于结算协议字段名'
    if is_internal_code_identifier_name(name):
        return '名称使用内部代码标识格式'
    if _FILE_PATH_RE.fullmatch(name):
        return '名称使用文件路径格式'
    if _VISIBLE_NAME_STRUCTURE_RE.search(name):
        return '名称包含 JSON/列表结构片段'
    prefix = lowered.split(':', 1)[0].split('：', 1)[0].strip()
    if prefix in _RESERVED_VISIBLE_NAMES and (':' in name or '：' in name):
        return '名称是协议字段清单标题'
    return None


def resolve_legacy_visible_name(value: object) -> str:
    """只对已确认的精确历史脏名做展示别名解析。"""
    name = str(value or '').strip()
    return LEGACY_VISIBLE_NAME_ALIASES.get(name, name)
