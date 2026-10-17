"""Frozen hidden-drop provenance for appeal evidence and reclaim protection."""

from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix


HIDDEN_DROP_EVIDENCE_KEY = 'hidden_dungeon_reward_receipts'
HIDDEN_DROP_SOURCE_RULE = """【隐藏副本独立掉落归属】
expedition_evidence.hidden_dungeon_reward_receipts 是后端已经发放的隐藏副本掉落回执，逐项 adventurer_id、item_name、quantity 绑定原获奖者及实际数量。这些数量已经包含在成员 new_equipments 和队伍结算摘要中，不是额外一份；其归属不由正文决定，正文未描写也不是错发。此回执优先于剧情、事实卡、玩家主张和先前申诉对这部分掉落的归属解释；不得把正文中相似名称或形态的战利品当作同一物品，亦不得按拿取、战斗贡献或治疗行为重分配隐藏掉落。
回执只证明已发放事实，不授权重新抽取、补发随机结果或解释掉落概率；不得把这部分结算与正文差异记成归属冲突。正文另有独立普通所得时只按其自身证据核对，不把物品名称列为全局禁用名单。
"""
HIDDEN_DROP_APPEAL_RULE = HIDDEN_DROP_SOURCE_RULE + """
reclaim_items.quantity 仍为申诉者剧情应得总量 entitlement，不是来源扣除量。来源普通份额等于该成员本次同名总到账量扣除隐藏掉落及此前已执行追回量，实际追回至多为 entitlement 与剩余普通份额的较小值；来源没有普通份额时拒绝向其追回，不扣隐藏掉落或折价金币。申诉者普通已到账量也须从同名总到账量扣除本人的隐藏掉落，补发差额只按普通已到账量与剧情 entitlement 比较。不能通过其它补发操作转移隐藏掉落，保留其它独立合法操作。原记录确有发放故障时交后端掉落结算核验，不以剧情改判回执。
例如来源同名总到账3、隐藏1、此前未追回，剧情 entitlement=5时，最多追回普通2，补发仍独立核对申诉者普通收益差额；若来源仅隐藏1、没有普通收益，则拒绝向其追回。申诉者本人隐藏1不能抵消另外应得但漏发的普通1，也不能再次补发隐藏1。
"""


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError('hidden_drop_receipt_integer_invalid')
    return value


def project_hidden_drop_receipts(full_log: dict) -> list[dict]:
    """Project only actual grants, never probabilities, candidates or prose."""
    if not isinstance(full_log, dict):
        raise ValueError('hidden_drop_log_unavailable')
    planned = full_log.get('_hidden_dungeon_reward_plan')
    rows = full_log.get('hidden_dungeon_rewards')
    if not planned and rows is None:
        return []
    if full_log.get('hidden_dungeon_rewards_settled') is not True:
        raise ValueError('hidden_drop_settlement_unconfirmed')
    if not isinstance(rows, list):
        raise ValueError('hidden_drop_receipts_unavailable')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('items'), list):
            raise ValueError('hidden_drop_receipt_invalid')
        mid = _positive_int(row.get('adventurer_id'))
        for item in row['items']:
            if not isinstance(item, dict):
                raise ValueError('hidden_drop_item_invalid')
            name = item.get('item_name')
            granted = item.get('granted')
            if (not isinstance(name, str) or not name.strip()
                    or not isinstance(granted, list) or not granted
                    or any(not isinstance(n, str) or not n.strip() for n in granted)):
                raise ValueError('hidden_drop_grant_unconfirmed')
            key = (mid, name.strip())
            result[key] = result.get(key, 0) + _positive_int(item.get('quantity'))
    return [
        {'adventurer_id': mid, 'item_name': name, 'quantity': quantity}
        for (mid, name), quantity in sorted(result.items())
    ]


def hidden_drop_reclaim_error(
    full_log: dict, reclaims: list[dict], previous_reclaims: list[dict],
    execution_context: dict | None = None,
) -> dict | None:
    """Check the remaining ordinary share before any removal or compensation."""
    receipts = project_hidden_drop_receipts(full_log)
    protected = {(r['adventurer_id'], r['item_name']): r['quantity'] for r in receipts}
    if execution_context is not None:
        execution_context['protected_quantities'] = protected
        execution_context['reclaim_quantities'] = {}
    if not protected:
        return None
    totals = {}
    for update in full_log.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        mid = update.get('id')
        items = update.get('new_equipments')
        if not isinstance(items, dict):
            continue
        for name, value in items.items():
            quantity = value.get('quantity', 1) if isinstance(value, dict) else 1
            name, quantity = split_stack_suffix(name, quantity)
            key = (mid, name)
            if key in protected:
                totals[key] = totals.get(key, 0) + _positive_int(quantity)
    used = {}
    for row in previous_reclaims:
        name, _ = split_stack_suffix(str(row.get('name') or ''), 1)
        key = (row.get('from_adventurer_id'), name)
        if key in protected:
            quantity = row.get('reclaim_quantity', row.get('quantity'))
            used[key] = used.get(key, 0) + _positive_int(quantity)
    seen = set()
    for row in reclaims:
        name, _ = split_stack_suffix(str(row.get('name') or ''), 1)
        key = (row.get('from_adventurer_id'), name)
        if key not in protected:
            continue
        if key in seen:
            return {
                'code': 'hidden_dungeon_drop_reclaim_duplicate',
                'detail': '同一来源物品重复声明追回，无法唯一确定普通收益扣除量。',
            }
        seen.add(key)
        quantity = _positive_int(row.get('quantity'))
        remaining = max(0, totals.get(key, 0) - protected[key] - used.get(key, 0))
        if remaining <= 0:
            return {
                'code': 'hidden_dungeon_drop_reclaim_forbidden',
                'detail': f'「{name}」包含原获奖者的隐藏副本独立掉落，本次普通收益剩余可追回数量为 {remaining}，拒绝转移隐藏掉落或折价扣款。',
            }
        reclaim_quantity = min(quantity, remaining)
        if execution_context is not None:
            execution_context['reclaim_quantities'][key] = reclaim_quantity
        used[key] = used.get(key, 0) + reclaim_quantity
    return None
