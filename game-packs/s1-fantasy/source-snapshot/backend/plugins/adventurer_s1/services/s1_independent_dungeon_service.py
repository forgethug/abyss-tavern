"""独立副本只关闭剧情连续性，保留本场正文与真实结算。"""

from .adventurer_s1_config_service import is_s1_independent_dungeon

MATERIAL_REWARD_INPUT_KEY = '_material_dungeon_reward_input'
MATERIAL_REWARD_PLAN_KEY = '_material_dungeon_reward_plan'


def project_independent_dungeon_result(result: dict | None, target_dungeon: str) -> None:
    """未启用的记忆字段不是本场消费者输入；重试和审核后使用同一投影。"""
    if not isinstance(result, dict):
        return
    from .s1_campaign_world_service import project_activity_result
    projected = project_activity_result(result, target_dungeon)
    if projected is not result:
        result.clear()
        result.update(projected)
    if not is_s1_independent_dungeon(target_dungeon):
        return
    for field in (
        'world_fact_updates', 'dm_handoff_note', 'location_state_changes',
        'dungeon_explorability_change', 'ecology_continuity_debt',
        'countermeasure_learning', 'waiting_npc_updates', 'fate_progress',
    ):
        result.pop(field, None)
    for update in result.get('member_updates') or []:
        if isinstance(update, dict):
            update.pop('story_threads', None)
            update.pop('relationship_changes', None)


def freeze_material_reward_input() -> dict:
    from copy import deepcopy
    from flask import current_app
    from ..plugin import AdventurerS1Plugin
    from .s1_independent_material_catalog import MATERIAL_POOL_VERSION, material_pool_snapshot

    key = 'ADVENTURER_S1_MATERIAL_DUNGEON_BOX_TIERS'
    tiers = deepcopy(current_app.config.get(key, AdventurerS1Plugin.default_config[key]))
    if not isinstance(tiers, list) or not tiers:
        raise ValueError('材料本开箱档位不能为空')
    previous = -1
    for tier in tiers:
        if (not isinstance(tier, dict)
                or type(tier.get('min_score')) not in (int, float)
                or not previous < tier['min_score'] <= 10
                or type(tier.get('boxes')) is not int or tier['boxes'] < 0):
            raise ValueError('材料本开箱档位非法')
        previous = tier['min_score']
    if tiers[0]['min_score'] != 0:
        raise ValueError('材料本开箱档位须从零分开始')
    return {'pool_version': MATERIAL_POOL_VERSION, 'pool': material_pool_snapshot(), 'tiers': tiers}


def build_material_reward_plan(frozen: dict, updates: list[dict], member_ids: list[int], *, choose=None) -> dict:
    """只接收评分与领取身份；材料池及数量规则在发车时冻结。"""
    import random
    from collections import Counter
    from copy import deepcopy

    ids = sorted(member_ids)
    by_id = {row['id']: row for row in updates}
    if not ids or len(ids) != len(set(ids)) or set(by_id) != set(ids) or len(updates) != len(ids):
        raise ValueError('材料本评分成员不完整')
    scores = [by_id[mid].get('score') for mid in ids]
    if any(type(score) not in (int, float) or not 0 <= score <= 10 for score in scores):
        raise ValueError('材料本须使用审核后的有效本场评分')
    team_score = sum(scores) / len(scores)
    boxes = next(tier['boxes'] for tier in reversed(frozen['tiers']) if team_score >= tier['min_score'])
    pool = frozen['pool']
    if not pool:
        raise ValueError('材料本掉落池为空')
    draw = choose or random.choice
    rewards = []
    by_name = {row['name']: row for row in pool}
    for member_id in ids:
        counts = Counter(draw(pool)['name'] for _ in range(boxes))
        rewards.append({
            'adventurer_id': member_id,
            'items': [{**deepcopy(by_name[name]), 'quantity': count} for name, count in counts.items()],
        })
    return {
        'pool_version': frozen['pool_version'], 'team_score': team_score,
        'boxes_per_member': boxes, 'members': rewards,
    }


def settle_material_rewards(log, members_by_id: dict, updates_by_id: dict) -> None:
    """沿日志完成事务发放；计划已冻结，失败回滚和重放不重新抽取。"""
    from .s1_hidden_dungeon_service import _append_capacity_discard, _merge_reward_into_member_update
    from .s1_item_grant_service import (
        grant_raw_item, prepare_warehouse_grant_batch, is_warehouse_capacity_error,
    )
    full_log = dict(log.full_log or {})
    if not is_s1_independent_dungeon(log.target_dungeon) or full_log.get('material_rewards_settled'):
        return
    plan = full_log.get(MATERIAL_REWARD_PLAN_KEY)
    if not isinstance(plan, dict):
        raise ValueError('材料本缺少冻结开箱计划')
    if {row['adventurer_id'] for row in plan['members']} != set(members_by_id):
        raise ValueError('材料本开箱计划与结算成员不一致')
    events = list(full_log.get('events') or [])
    events.append(f"📦 本场挑战结算：全队每人获得 {plan['boxes_per_member']} 个材料箱，现已开启。")
    receipt = []
    for row in plan['members']:
        member_id = row['adventurer_id']
        member, update = members_by_id[member_id], updates_by_id[member_id]
        items = row['items']
        batch = prepare_warehouse_grant_batch(member, [item['name'] for item in items]) if items else None
        granted = []
        for item in items:
            destination, err = grant_raw_item(
                member, item['name'], item,
                source_type='material_dungeon_box', description='独立材料本开箱',
                force_destination='warehouse', register_catalog=False,
                related_id=str(log.id), related_type='expedition_log',
                warehouse_batch_context=batch,
            )
            if err:
                if is_warehouse_capacity_error(member_id, err):
                    _append_capacity_discard(update, item_name=item['name'], quantity=item['quantity'])
                    continue
                raise RuntimeError(f'材料箱发放失败: {err}')
            _merge_reward_into_member_update(
                update, item_name=item['name'], quantity=item['quantity'], rarity=item['rarity'],
            )
            update['new_equipments'][item['name']].update(type='material', desc=item['desc'])
            granted.append({'name': item['name'], 'quantity': item['quantity'], 'destination': destination})
        if granted:
            contents = '、'.join(f"{item['name']}×{item['quantity']}" for item in granted)
            disposition = (
                '本场所得随阵亡遗物一并没收。'
                if getattr(member, 'status', None) == 'dead' else '已收入仓库。'
            )
            events.append(f'📦 {member.name} 的材料箱：{contents}，{disposition}')
        receipt.append({
            'adventurer_id': member_id, 'items': granted,
            'confiscated_on_death': getattr(member, 'status', None) == 'dead',
        })
    full_log.update({
        'events': events, 'current_event_index': len(events),
        'material_rewards': receipt, 'material_rewards_settled': True,
    })
    log.full_log = full_log
