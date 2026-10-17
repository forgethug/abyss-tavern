"""S1 隐藏副本：独立入口判定、冻结掉落计划与事务内结算。"""

from __future__ import annotations

import random
from typing import Iterable

from flask import current_app

from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_ACTIVE_DUNGEON_NAMES,
    get_s1_hidden_dungeons_by_source,
    get_s1_dungeon_level_range,
)
from plugins.adventurer_s1.services.adventurer_s1_settings_service import (
    S1SettingsService,
)


HIDDEN_DUNGEON_REWARD_PLAN_KEY = '_hidden_dungeon_reward_plan'


def _select_hidden_dungeon(candidates: Iterable[dict], draw) -> dict | None:
    """在已启用候选中按正权重选择；单候选不消耗额外随机数。"""
    eligible = tuple(
        row for row in candidates
        if row.get('enabled') is True and float(row.get('selection_weight') or 0) > 0
    )
    if not eligible:
        return None
    if len(eligible) == 1:
        return eligible[0]

    total_weight = sum(float(row.get('selection_weight') or 0) for row in eligible)
    threshold = max(0.0, min(1.0, float(draw()))) * total_weight
    cumulative = 0.0
    for row in eligible:
        cumulative += float(row.get('selection_weight') or 0)
        if threshold < cumulative:
            return row
    return eligible[-1]


def build_hidden_dungeon_prompt_block(context: dict | None) -> str:
    if not isinstance(context, dict) or not context.get('dungeon'):
        return ''
    return (
        '【隐藏副本权威入口】\n'
        f"- 队伍原本选择：{context.get('source_dungeon') or '常规副本'}（仅表示入口来源）。\n"
        f"- 本次实际进入：{context['dungeon']}（{context.get('level_desc') or '难度提高'}）。\n"
        '- 之后所有场景、生态、地点状态与世界情报都以实际进入的隐藏副本为目标地点。\n'
        '- 特殊事件、隐藏 Boss、委托等输入均为独立判定；若同时存在，必须继续执行，不得因进入隐藏副本而删除、降级或替换。\n'
        '- 不要虚构隐藏副本掉落或概率；固定掉落由后端另行结算。'
    )


def roll_hidden_dungeon(
    source_dungeon: str,
    members: Iterable,
    *,
    random_fn=None,
) -> tuple[dict | None, list[dict]]:
    """对常规副本独立掷骰，并冻结每名成员的成功掉落。

    返回值只描述隐藏副本自身；调用方仍须照常执行特殊事件、Boss 与委托判定。
    """
    candidates = get_s1_hidden_dungeons_by_source(source_dungeon)
    if source_dungeon not in S1_ACTIVE_DUNGEON_NAMES or not candidates:
        return None, []
    if S1SettingsService.get('hidden_dungeon_enabled') is not True:
        return None, []
    draw = random_fn or random.random
    trigger_probability = float(current_app.config.get(
        'ADVENTURER_S1_HIDDEN_DUNGEON_TRIGGER_PROBABILITY', 0.10,
    ) or 0)
    if draw() >= max(0.0, min(1.0, trigger_probability)):
        return None, []

    hidden = _select_hidden_dungeon(candidates, draw)
    if hidden is None:
        return None, []
    source_min, _ = get_s1_dungeon_level_range(source_dungeon)
    level_min, level_max = get_s1_dungeon_level_range(hidden['name'])
    level_offset = max(0, level_min - source_min)
    context = {
        'code': hidden['code'],
        'source_dungeon': source_dungeon,
        'dungeon': hidden['name'],
        'level_offset': level_offset,
        'level_desc': f'Lv.{level_min}+' if level_max >= 999 else f'Lv.{level_min}-{level_max}',
    }

    configured_rates = current_app.config.get('ADVENTURER_S1_HIDDEN_DUNGEON_DROP_RATES') or {}
    reward_plan: list[dict] = []
    for member in members:
        planned_items: list[dict] = []
        for drop in hidden.get('drops') or []:
            rarity = str(drop.get('rarity') or 'common').strip().lower()
            rate = float(configured_rates.get(rarity, 0) or 0)
            if draw() >= max(0.0, min(1.0, rate)):
                continue
            planned_items.append({
                'item_name': str(drop.get('item_name') or '').strip(),
                'quantity': max(1, int(drop.get('quantity') or 1)),
                'rarity': rarity,
            })
        if planned_items:
            reward_plan.append({
                'adventurer_id': int(member.id),
                'items': planned_items,
            })
    return context, reward_plan


def _merge_reward_into_member_update(
    update: dict,
    *,
    item_name: str,
    quantity: int,
    rarity: str,
) -> None:
    """把后端固定掉落并入最终成员结算，供回执、保险与玩家展示统一消费。"""
    equipments = update.get('new_equipments')
    equipments = dict(equipments) if isinstance(equipments, dict) else {}
    existing = equipments.get(item_name)
    item_data = dict(existing) if isinstance(existing, dict) else {}
    existing_quantity = 1 if item_name in equipments else 0
    try:
        existing_quantity = (
            max(1, int(item_data.get('quantity') or 1))
            if existing_quantity
            else 0
        )
    except (TypeError, ValueError):
        existing_quantity = 1 if existing_quantity else 0
    item_data['quantity'] = existing_quantity + quantity
    if rarity and not item_data.get('rarity'):
        item_data['rarity'] = rarity
    equipments[item_name] = item_data
    update['new_equipments'] = equipments


def _append_capacity_discard(
    update: dict,
    *,
    item_name: str,
    quantity: int,
) -> None:
    discarded = update.get('discarded_equipments')
    discarded = list(discarded) if isinstance(discarded, list) else []
    discarded.append({
        'name': item_name,
        'quantity': quantity,
        'reason': 'warehouse_full',
        'message': f'仓库已满，未能存入「{item_name}」，该物品已丢弃',
    })
    update['discarded_equipments'] = discarded


def settle_hidden_dungeon_rewards(
    log,
    members_by_id: dict[int, object],
    updates_by_id: dict[int, dict],
) -> tuple[list[dict], str | None]:
    """发放冻结掉落并合入最终成员结算；事务与 completed 写回由调用方统一提交。"""
    full_log = dict(log.full_log or {})
    if not log.hidden_dungeon:
        return [], None
    if full_log.get('hidden_dungeon_rewards_settled'):
        return list(full_log.get('hidden_dungeon_rewards') or []), None

    from plugins.adventurer_s1.services.s1_item_grant_service import (
        grant_catalog_item,
        is_warehouse_capacity_error,
    )

    settled: list[dict] = []
    for member_plan in full_log.get(HIDDEN_DUNGEON_REWARD_PLAN_KEY) or []:
        try:
            adventurer_id = int(member_plan.get('adventurer_id'))
        except (TypeError, ValueError):
            return [], '隐藏副本掉落计划包含无效冒险者 ID'
        member = members_by_id.get(adventurer_id)
        if member is None:
            return [], f'隐藏副本掉落结算找不到冒险者 {adventurer_id}'
        update = updates_by_id.get(adventurer_id)
        if not isinstance(update, dict):
            return [], f'隐藏副本掉落结算找不到成员结算 {adventurer_id}'
        member_items: list[dict] = []
        for planned in member_plan.get('items') or []:
            item_name = str(planned.get('item_name') or '').strip()
            quantity = max(1, int(planned.get('quantity') or 1))
            rarity = str(planned.get('rarity') or 'common').strip().lower()
            if not item_name:
                return [], '隐藏副本掉落计划包含空物品名'
            names, grant_err = grant_catalog_item(
                member,
                item_name,
                quantity=quantity,
                prefer_warehouse=True,
                source_type='hidden_dungeon_drop',
                description=f'探索【{log.hidden_dungeon}】获得「{item_name}」',
                related_id=str(log.id),
                related_type='expedition_log',
            )
            if grant_err:
                if is_warehouse_capacity_error(adventurer_id, grant_err):
                    _append_capacity_discard(
                        update,
                        item_name=item_name,
                        quantity=quantity,
                    )
                    current_app.logger.warning(
                        'S1 隐藏副本掉落因仓库已满丢弃 log=%s adv=%s item=%s quantity=%s',
                        log.id,
                        adventurer_id,
                        item_name,
                        quantity,
                    )
                    continue
                return [], grant_err
            _merge_reward_into_member_update(
                update,
                item_name=item_name,
                quantity=quantity,
                rarity=rarity,
            )
            member_items.append({'item_name': item_name, 'quantity': quantity, 'granted': names})
        if member_items:
            settled.append({
                'adventurer_id': adventurer_id,
                'adventurer_name': getattr(member, 'name', ''),
                'items': member_items,
            })

    full_log['hidden_dungeon_rewards'] = settled
    full_log['hidden_dungeon_rewards_settled'] = True
    log.full_log = full_log
    return settled, None
