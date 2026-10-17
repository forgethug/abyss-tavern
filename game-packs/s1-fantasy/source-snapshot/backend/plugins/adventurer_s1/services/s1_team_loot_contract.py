"""Current, projected contract for physical expedition loot awaiting ownership."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from plugins.adventurer_s1.s1_item_types import S1_ITEM_TYPE_SET
from plugins.adventurer_s1.services.s1_visible_name_policy import visible_name_pollution_reason
from plugins.adventurer_s1.services.s1_item_quantity_service import coerce_item_quantity

TEAM_LOOT_FIELD = 'unassigned_items'
TEAM_LOOT_MAX_EVIDENCE_EVENTS = 16
TEAM_LOOT_ROW_FIELDS = frozenset({
    'source_event_index', 'evidence_event_indexes', 'name', 'item',
})
TEAM_LOOT_ITEM_FIELDS = frozenset({
    'type', 'desc', 'rarity', 'quantity', 'uses_left', 'recovery_minutes',
    'evolution_stage', 'damage_stage', 'damage_note',
})


def project_team_loot(value):
    if not isinstance(value, list):
        return deepcopy(value)
    result = []
    for raw in value:
        if not isinstance(raw, dict):
            result.append(deepcopy(raw))
            continue
        row = {key: deepcopy(raw[key]) for key in TEAM_LOOT_ROW_FIELDS if key in raw}
        if isinstance(row.get('item'), dict):
            row['item'] = {
                key: val for key, val in row['item'].items()
                if key in TEAM_LOOT_ITEM_FIELDS
            }
        result.append(row)
    return result


def normalize_team_loot(value, *, events: list, path: str = TEAM_LOOT_FIELD):
    """Validate identity/evidence mechanically; semantic approval belongs to review."""
    if value is None:
        return [], []
    projected = project_team_loot(value)
    if not isinstance(projected, list):
        return [], [f'{path}:not_array']
    errors, normalized, seen = [], [], set()
    for index, row in enumerate(projected):
        prefix = f'{path}[{index}]'
        if not isinstance(row, dict):
            errors.append(f'{prefix}:not_object')
            continue
        name = row.get('name')
        source = row.get('source_event_index')
        evidence = row.get('evidence_event_indexes')
        item = row.get('item')
        valid_name = (
            isinstance(name, str) and bool(name.strip()) and len(name.strip()) <= 100
            and not visible_name_pollution_reason(name)
        )
        if not valid_name:
            errors.append(f'{prefix}.name:invalid')
        valid_source = type(source) is int and 0 <= source < len(events)
        if not valid_source:
            errors.append(f'{prefix}.source_event_index:invalid')
        if (
            not isinstance(evidence, list) or not evidence or len(evidence) > TEAM_LOOT_MAX_EVIDENCE_EVENTS
            or any(type(i) is not int or i < 0 or i >= len(events) for i in evidence)
            or len(set(evidence)) != len(evidence)
            or source not in evidence
        ):
            errors.append(f'{prefix}.evidence_event_indexes:invalid')
        if not isinstance(item, dict):
            errors.append(f'{prefix}.item:not_object')
            continue
        if not isinstance(item.get('type'), str) or item['type'] not in S1_ITEM_TYPE_SET:
            errors.append(f'{prefix}.item.type:invalid')
        if not isinstance(item.get('rarity'), str) or item['rarity'] not in {'common', 'rare', 'epic', 'legendary'}:
            errors.append(f'{prefix}.item.rarity:invalid')
        if not isinstance(item.get('desc'), str) or not item['desc'].strip():
            errors.append(f'{prefix}.item.desc:missing')
        if (type(item.get('quantity')) is not int or item['quantity'] <= 0
                or coerce_item_quantity(item['quantity']) != item['quantity']):
            errors.append(f'{prefix}.item.quantity:invalid')
        for key in ('recovery_minutes', 'evolution_stage'):
            if key in item and (type(item[key]) is not int or item[key] < 0):
                errors.append(f'{prefix}.item.{key}:invalid')
        if 'uses_left' in item and item['uses_left'] is not None:
            if type(item['uses_left']) is not int or item['uses_left'] < 0:
                errors.append(f'{prefix}.item.uses_left:invalid')
        if 'damage_note' in item and not isinstance(item['damage_note'], str):
            errors.append(f'{prefix}.item.damage_note:invalid')
        if 'damage_stage' in item and item['damage_stage'] is not None:
            if not isinstance(item['damage_stage'], str):
                errors.append(f'{prefix}.item.damage_stage:invalid')
        if valid_name and valid_source:
            identity = (source, name.strip())
            if identity in seen:
                errors.append(f'{prefix}:duplicate_source')
            seen.add(identity)
        row = deepcopy(row)
        if valid_name:
            row['name'] = name.strip()
        normalized.append(row)
    return normalized, errors


def team_loot_source_key(log_id: int, source_event_index: int, name: str) -> str:
    return sha256(f'{log_id}\0{source_event_index}\0{name}'.encode('utf-8')).hexdigest()


def team_loot_registration_hash(row: dict) -> str:
    return sha256(json.dumps(project_team_loot([row])[0], ensure_ascii=False,
                             sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


TEAM_LOOT_STRUCTURE_RULE = """
【共同实物字段合同】
unassigned_items 是数组；省略、null 或 [] 均表示无待分配实物。每项仅含 source_event_index、evidence_event_indexes、name、item，四者必填。source_event_index 是首次实际取得本份实物的零基 events 下标；evidence_event_indexes 是 1~16 个去重整数下标，包含 source_event_index 并覆盖实际取得、共同带回与最终处置；引用已有正文，不续写取得事实。name 是不超过100字的稳定自然物品名。同份来源与名称唯一，同批总数扣除已经个人分配和不再保留的部分后，才是该份正整数库存数量。
item 必含 type、rarity、desc、quantity；type 仅为 TYPE_VALUES；rarity 仅为 common / rare / epic / legendary；desc 为完整自然物品描述；quantity 为1~999999的正整数，指尚未个人分配的整份库存，不是估值或金币收入，越界不得截断。可选 uses_left 是非负整数或 null，缺失时沿图鉴/类型默认次数，null 表示无次数限制；recovery_minutes 与 evolution_stage 是非负整数，缺失时沿普通新物品合同；damage_stage 是字符串或 null，damage_note 是字符串，缺失保持普通新物品状态。该出口只登记新取得的实物，不能传入账户、所有者、执行权限、资产ID、后台回执或系统时间。
数组逐份保留全部独立合法来源，不截断、合并不同来源或用固定掉落配额补齐。单份证据超出16项时，选择仍能完整证明取得与最终处置的关键事件；不能省略必要证据或提交越界集合。
""".replace('TYPE_VALUES', ' / '.join(sorted(S1_ITEM_TYPE_SET)))

TEAM_LOOT_GENERATION_RULE = """
【共同带回实物】
本场取得且最终由本队带出的独立实物，个人归属已经成立时写入该成员的 new_equipments；仅共同持有、尚未确定个人归属时，写入顶层 unassigned_items，作为本次出发小队的待分配实物。正文照实呈现取得、处置和共同带回，不为填结算字段替成员作归属选择。未取得、已消耗、已交给队外、留在原地及出发已有资产不构成新待分配收获。没有共同带回实物时按原结算处理。
""" + TEAM_LOOT_STRUCTURE_RULE

TEAM_LOOT_OBSERVER_RULE = """
【共同带回实物的落账审核】
逐项核对完整 events 的取得、最终处置与归属。确实共同取得并带回、只是未定个人归属的独立实物进入 unassigned_items；纠正误分给个人的同一份，并补齐正文已成立但候选遗漏的共同收获。个人所有权已经成立的部分沿 member_updates 保留，不改成共同池；出发已有物及其借用、代管或转交沿原资产合同处理。数量先扣除个人已分配和实际消耗、遗失、交付队外的份额，不能同时个人发放和入池，也不以估值折现金币。只有个人归属缺失不构成正文重生成理由。
候选 unassigned_items 非空时，即使全部一致也须用 override_fields.unassigned_items 返回审核确认后的完整合法集合；需补齐或删除时同样输出完整集合（可为 []）。每项沿【共同实物字段合同】绑定实际取得与留存事件；个人物品纠错沿本轮启用的物品交付合同，其它独立合法结果保留。没有实际取得/带回证据或数量无法确定时不创造池条目。
""" + TEAM_LOOT_STRUCTURE_RULE

TEAM_LOOT_APPEAL_RULE = """
【共同实物漏记申诉】
关联探险全文已成立本队取得并共同带回的独立实物，仅未确定个人归属且仍未登记时，可在 patch.unassigned_items 提议进入该次出发小队的待分配池。字段与共同带回实物合同一致：source_event_index 指向首次实际取得，evidence_event_indexes 覆盖取得与最终留存，name 与 item 给出该份完整物品和正整数未分配数量。不是补给申诉者的 create_items，也不使用折现金币的 team_pool；个人归属已成立的物品继续沿原个人修复/reclaim 出口。
输入 expedition_evidence.unassigned_item_status 列出本案已登记的精确来源、物品和分配状态。已经入池、选出领取人或领取的同一份不能再次补发；cancelled 表示此前登记已撤销，可以在新的独立审核后重新登记。初审只提出漏记提案；二审逐 operation_id 独立核对全文、个人结算、池记录和既往申诉回执，明确 allow 后才登记，全队参与资格仍绑定本次原队员而非申诉者当前队伍。玩家复核只判断是否漏修，不直接决定个人获得者或承诺到账。缺取得、留存或数量依据时沿原未成立出口，不能把申诉文字变成新收获。进入池代表共同权益已经登记，个人最终物品与金币以之后的队伍分配回执为准。
"""
