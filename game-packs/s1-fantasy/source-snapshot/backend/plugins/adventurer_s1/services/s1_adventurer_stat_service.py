"""S1 冒险者基础属性与生命上限封顶。

四项基础属性（力量/敏捷/智力/运气）与 max_hp 在 config 中配置上限；
所有永久增减必须经本模块，避免单点漏拦。
"""
from __future__ import annotations

from flask import current_app

S1_BASE_ATTRS = ('strength', 'agility', 'intelligence', 'luck')


def get_s1_attr_cap() -> int:
    return max(1, int(current_app.config.get('ADVENTURER_S1_ATTR_CAP', 200)))


def get_s1_max_hp_cap() -> int:
    return max(1, int(current_app.config.get('ADVENTURER_S1_MAX_HP_CAP', 1000)))


def clamp_s1_attr_value(value: int) -> int:
    return max(0, min(int(value), get_s1_attr_cap()))


def clamp_s1_max_hp_value(value: int) -> int:
    return max(1, min(int(value), get_s1_max_hp_cap()))


def clamp_s1_adventurer_stats(adv) -> None:
    """将冒险者当前基础属性与 max_hp 夹紧到上限，并保证 hp 不超过 max_hp。"""
    for attr in S1_BASE_ATTRS:
        setattr(adv, attr, clamp_s1_attr_value(getattr(adv, attr, 0) or 0))
    adv.max_hp = clamp_s1_max_hp_value(adv.max_hp or 100)
    adv.hp = max(0, min(int(adv.hp or 0), int(adv.max_hp)))


def add_s1_attr(adv, attr: str, delta: int) -> int:
    """永久增加基础属性，返回实际生效的增量。"""
    if attr not in S1_BASE_ATTRS:
        raise ValueError(f'无效基础属性: {attr}')
    current = int(getattr(adv, attr, 0) or 0)
    cap = get_s1_attr_cap()
    new_val = min(cap, max(0, current + int(delta)))
    applied = new_val - current
    setattr(adv, attr, new_val)
    return applied


def add_s1_max_hp(adv, delta: int, *, sync_hp: bool = True) -> int:
    """永久增加 max_hp，返回实际生效的增量。sync_hp 时同步抬高当前 hp（仅存活时）。"""
    current = int(adv.max_hp or 100)
    cap = get_s1_max_hp_cap()
    new_val = min(cap, max(1, current + int(delta)))
    applied = new_val - current
    adv.max_hp = new_val
    if int(adv.hp or 0) > new_val:
        adv.hp = new_val
    elif sync_hp and applied > 0 and int(adv.hp or 0) > 0:
        adv.hp = min(new_val, int(adv.hp or 0) + applied)
    return applied
