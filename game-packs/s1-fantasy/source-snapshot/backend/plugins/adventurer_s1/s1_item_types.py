"""S1 物品类型注册表。

图鉴 catalog.type、管理后台下拉、校验白名单、收购底价 tier 均由此模块引用；
新增/调整标准类型时只改本文件。
"""
from __future__ import annotations

from typing import Iterable

# ── 标准类型（管理后台可选、图鉴主枚举；顺序即 UI 下拉顺序）────────────────
S1_ITEM_TYPES: tuple[str, ...] = (
    'companion',
    'weapon',
    'armor',
    'shield',
    'accessory',
    'consumable',
    'potion',
    'scroll',
    'material',
    'special',
    'quest',
)
S1_ITEM_TYPE_SET = frozenset(S1_ITEM_TYPES)
S1_DEFAULT_ITEM_TYPE = 'special'
S1_ITEM_TYPE_COMPANION = 'companion'


def normalize_item_type(item_type: str) -> str:
    return str(item_type or '').strip().lower()


def companion_allows_duplicate_name_instances(item_type: str) -> bool:
    """Companion 允许多实例同名，靠 equipment.id / evolution_stage 区分个体。"""
    return str(item_type or '').strip().lower() == S1_ITEM_TYPE_COMPANION


def coerce_evolution_stage(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def clamp_evolution_stage(current, proposed) -> int:
    """同一物品实例的普通更新：evolution_stage 只增不减。"""
    return max(coerce_evolution_stage(current), coerce_evolution_stage(proposed))


def clamp_companion_evolution_stage(current, proposed) -> int:
    """兼容旧调用名；伙伴与其他装备共享同一等阶单调性。"""
    return clamp_evolution_stage(current, proposed)


# 消耗品/材料/门票/杂物等同属性堆叠：再次发放时合并 quantity，而非拒绝或新建同名行
# misc：拾荒垃圾、收购商行回收物等，允许同名增量合并
S1_QUANTITY_STACK_MERGE_TYPES = frozenset({
    'consumable', 'potion', 'scroll', 'material', 'ticket', 'misc',
})


def catalog_item_allows_quantity_stack_merge(item_type: str) -> bool:
    # 勿走 normalize_item_type：ticket 等非 S1_ITEM_TYPES 标准项会被回落为 special
    return str(item_type or '').strip().lower() in S1_QUANTITY_STACK_MERGE_TYPES


# 创建冒险者初始可选装备（is_starter 图鉴合法 type）
S1_STARTER_ITEM_TYPES = frozenset({'weapon', 'shield', 'armor', 'companion', 'special'})

# 军械库在售的初始装备（不含 companion / special）
S1_ARMORY_STARTER_TYPES = frozenset({'weapon', 'shield', 'armor'})

# 可穿戴/战斗装备类（锻造、装备栏语义）
S1_EQUIPMENT_LIKE_TYPES = frozenset({'weapon', 'armor', 'shield', 'accessory'})

# 一次性消耗（图鉴默认 uses_left=1）
S1_CONSUMABLE_ITEM_TYPES = frozenset({'consumable', 'potion', 'scroll'})

# 委托 AI 奖励物品允许的 type
S1_COMMISSION_REWARD_ITEM_TYPES = frozenset({
    'weapon', 'armor', 'accessory', 'companion', 'consumable', 'material', 'special',
})

# 系统内部类型（AI 产出或修复脚本写入，不进管理下拉）
S1_INTERNAL_ITEM_TYPES: tuple[str, ...] = ('clue', 'document', 'tool', 'key_item', 'misc', 'ticket')
S1_INTERNAL_ITEM_TYPE_SET = frozenset(S1_INTERNAL_ITEM_TYPES)


def format_item_types_slash(types: Iterable[str] | None = None) -> str:
    """拼成 prompt 用的 weapon/armor/... 字符串。"""
    return '/'.join(types or S1_ITEM_TYPES)


def normalize_item_type(
    raw: str,
    *,
    allowed: frozenset[str] | None = None,
    fallback: str = S1_DEFAULT_ITEM_TYPE,
) -> str:
    key = str(raw or '').strip().lower()
    pool = allowed or S1_ITEM_TYPE_SET
    return key if key in pool else fallback


# ── 收购商行底价（按 tier 归类；新 standard type 只改 RECYCLE_TIER_BY_TYPE）──
_RECYCLE_FLOOR_EQUIPMENT = {'common': 10, 'rare': 60, 'epic': 400, 'legendary': 2000}
_RECYCLE_FLOOR_CONSUMABLE = {'common': 5, 'rare': 60, 'epic': 400, 'legendary': 2000}
_RECYCLE_FLOOR_MATERIAL = {'common': 1, 'rare': 50, 'epic': 400, 'legendary': 2000}
_RECYCLE_FLOOR_MISC = {'common': 1, 'rare': 60, 'epic': 400, 'legendary': 2000}

RECYCLE_FLOOR_TIERS: dict[str, dict[str, int]] = {
    'equipment': _RECYCLE_FLOOR_EQUIPMENT,
    'consumable': _RECYCLE_FLOOR_CONSUMABLE,
    'material': _RECYCLE_FLOOR_MATERIAL,
    'misc': _RECYCLE_FLOOR_MISC,
}

RECYCLE_TIER_BY_TYPE: dict[str, str] = {
    'weapon': 'equipment',
    'armor': 'equipment',
    'shield': 'equipment',
    'companion': 'equipment',
    'accessory': 'equipment',
    'consumable': 'consumable',
    'potion': 'consumable',
    'scroll': 'consumable',
    'special': 'consumable',
    'material': 'material',
    'quest': 'consumable',
    'clue': 'misc',
    'document': 'consumable',
    'tool': 'consumable',
    'key_item': 'consumable',
    'misc': 'misc',
}


def build_recycle_type_floors() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for type_key, tier_key in RECYCLE_TIER_BY_TYPE.items():
        tier = RECYCLE_FLOOR_TIERS.get(tier_key)
        if tier is not None:
            out[type_key] = dict(tier)
    return out
