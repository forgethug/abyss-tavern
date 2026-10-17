"""装备/仓库物品实例定位与消耗：玩家操作按 id，AI 结算仍按 name（同名时 FIFO）。

统一消耗入口：consume_item()，内部写 ItemLog。
"""
from typing import Optional, Tuple, TypeVar, Union

from sqlalchemy import func, or_, select, text

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1Equipment,
    AdventurerS1ItemLog,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_item_quantity_service import coerce_item_quantity, item_quantity_of

T = TypeVar('T', AdventurerS1Equipment, AdventurerS1WarehouseItem)
ITEM_ATTRIBUTE_UNSET = object()

ITEM_OPERATION_NORMAL_CONSUME = 'normal_consume'
ITEM_OPERATION_SYSTEM_REVOKE = 'system_revoke'
ITEM_OPERATION_TRANSFORM_INPUT = 'transform_input'
ITEM_OPERATION_PLAYER_DESTRUCTIVE = 'player_destructive'

# PostgreSQL two-key advisory-lock namespace: ASCII "S1WH".
_WAREHOUSE_AGGREGATE_LOCK_NAMESPACE = 0x53315748

_IMMUTABLE_ALLOWED_OPERATIONS = {
    ITEM_OPERATION_NORMAL_CONSUME,
    ITEM_OPERATION_SYSTEM_REVOKE,
}

_PLAYER_LOCK_BLOCKED_OPERATIONS = {
    ITEM_OPERATION_TRANSFORM_INPUT,
    ITEM_OPERATION_PLAYER_DESTRUCTIVE,
}

_SUPPORTED_ITEM_OPERATIONS = _IMMUTABLE_ALLOWED_OPERATIONS | _PLAYER_LOCK_BLOCKED_OPERATIONS


def normalize_stack_custom_desc(value: Optional[str]) -> Optional[str]:
    """只归一化“没有个性描述”的两种存储形式。"""
    return None if value is None or value == '' else value


def _resolve_item(
    model,
    adventurer_id: int,
    *,
    item_id: Optional[int] = None,
    item_name: Optional[str] = None,
    label: str = '物品',
) -> Tuple[Optional[Union[AdventurerS1Equipment, AdventurerS1WarehouseItem]], Optional[str]]:
    if item_id is not None:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return None, f'无效的 {label} id'
        item = model.query.filter_by(adventurer_id=adventurer_id, id=item_id).first()
        if not item:
            return None, f'未找到指定{label}'
        return item, None

    name = (item_name or '').strip()
    if not name:
        return None, f'缺少 item_id 或 item_name'

    rows = model.query.filter_by(adventurer_id=adventurer_id, name=name).order_by(model.id.asc()).all()
    if not rows:
        return None, f'未找到「{name}」'
    if len(rows) > 1:
        return None, f'有多件「{name}」，请指定 item_id'
    return rows[0], None


def resolve_warehouse_item(
    adventurer_id: int,
    *,
    item_id: Optional[int] = None,
    item_name: Optional[str] = None,
) -> Tuple[Optional[AdventurerS1WarehouseItem], Optional[str]]:
    return _resolve_item(
        AdventurerS1WarehouseItem, adventurer_id,
        item_id=item_id, item_name=item_name, label='仓库物品',
    )


def resolve_equipment_item(
    adventurer_id: int,
    *,
    item_id: Optional[int] = None,
    item_name: Optional[str] = None,
) -> Tuple[Optional[AdventurerS1Equipment], Optional[str]]:
    return _resolve_item(
        AdventurerS1Equipment, adventurer_id,
        item_id=item_id, item_name=item_name, label='随身装备',
    )


def resolve_equipment_by_name_fifo(adventurer_id: int, name: str) -> Optional[AdventurerS1Equipment]:
    """AI 结算按名消耗时取最早一件（id 升序）。"""
    return (
        AdventurerS1Equipment.query
        .filter_by(adventurer_id=adventurer_id, name=name)
        .order_by(AdventurerS1Equipment.id.asc())
        .first()
    )


def resolve_item_ref(
    adv,
    ref: dict,
) -> Tuple[Optional[Union[AdventurerS1Equipment, AdventurerS1WarehouseItem]], Optional[str], str]:
    """建筑锻造等：item_refs 支持 {source, id} 或 legacy {source, name}。"""
    source = str(ref.get('source') or '').strip()
    if source not in {'carry', 'warehouse', 'any'}:
        return None, '无效的物品来源', source

    item_id = ref.get('id')
    name = str(ref.get('name') or '').strip()

    if item_id is not None:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return None, '无效的物品 id', source
        if source in {'carry', 'any'}:
            item = next((e for e in adv.equipment_items if e.id == item_id), None)
            if item:
                return item, None, 'carry'
        if source in {'warehouse', 'any'}:
            item = next((w for w in adv.warehouse_items if w.id == item_id), None)
            if item:
                return item, None, 'warehouse'
        return None, '未找到指定物品', source

    if not name:
        return None, '缺少物品 id 或名称', source

    carry = next((e for e in adv.equipment_items if e.name == name), None)
    warehouse = next((w for w in adv.warehouse_items if w.name == name), None)
    if source == 'carry':
        return (carry, None, 'carry') if carry else (None, f'随身没有「{name}」', source)
    if source == 'warehouse':
        return (warehouse, None, 'warehouse') if warehouse else (None, f'仓库没有「{name}」', source)
    if carry:
        return carry, None, 'carry'
    if warehouse:
        return warehouse, None, 'warehouse'
    return None, f'未找到「{name}」', source


def immutable_item_names(item_names: set[str] | list[str] | None) -> set[str]:
    from plugins.adventurer_s1.services.s1_item_use_condition_service import (
        catalog_immutable_item_names,
    )

    return catalog_immutable_item_names(item_names)


def item_is_immutable(item) -> bool:
    name = str(getattr(item, 'name', '') or '').strip()
    return bool(name and name in immutable_item_names({name}))


def first_immutable_item_name(items) -> str | None:
    names = {str(getattr(item, 'name', '') or '').strip() for item in (items or [])}
    locked = immutable_item_names(names)
    for item in items or []:
        name = str(getattr(item, 'name', '') or '').strip()
        if name in locked:
            return name
    return None


def validate_item_operation(
    item,
    operation: str = ITEM_OPERATION_NORMAL_CONSUME,
    *,
    action_label: str = '该操作',
) -> Optional[str]:
    """统一校验物品能否参与指定操作；普通使用允许消耗不可变物品。"""
    if operation not in _SUPPORTED_ITEM_OPERATIONS:
        return '无效的物品操作类型'
    if operation in _IMMUTABLE_ALLOWED_OPERATIONS:
        return None
    if bool(getattr(item, 'is_player_locked', False)):
        name = str(getattr(item, 'name', '') or '').strip()
        return f'「{name}」已受保护，请先解锁再进行{action_label}'
    if operation == ITEM_OPERATION_PLAYER_DESTRUCTIVE:
        return None
    if item_is_immutable(item):
        name = str(getattr(item, 'name', '') or '').strip()
        return f'「{name}」是不可变物品，不能用于{action_label}'
    return None


def validate_items_operation(
    items,
    operation: str = ITEM_OPERATION_NORMAL_CONSUME,
    *,
    action_label: str = '该操作',
) -> Optional[str]:
    """批量版本只查询一次目录，供多材料变换预检使用。"""
    if operation not in _SUPPORTED_ITEM_OPERATIONS:
        return '无效的物品操作类型'
    if operation in _IMMUTABLE_ALLOWED_OPERATIONS:
        return None
    normalized_items = list(items or [])
    for item in normalized_items:
        if bool(getattr(item, 'is_player_locked', False)):
            name = str(getattr(item, 'name', '') or '').strip()
            return f'「{name}」已受保护，请先解锁再进行{action_label}'
    if operation == ITEM_OPERATION_PLAYER_DESTRUCTIVE:
        return None
    immutable_name = first_immutable_item_name(normalized_items)
    if immutable_name:
        return f'「{immutable_name}」是不可变物品，不能用于{action_label}'
    return None


def item_refs_total(refs: list | None) -> int:
    """物品引用的总投入数量（支持 quantity 或重复条目）。"""
    if not isinstance(refs, list):
        return 0
    total = 0
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        qty = ref.get('quantity')
        if qty is None:
            total += 1
            continue
        try:
            count = int(qty)
        except (TypeError, ValueError):
            continue
        if count > 0:
            total += count
    return total


def parse_item_refs(
    raw_refs: list | None,
    *,
    min_count: int,
    max_count: int,
    selection_error: str,
    preserve_order: bool = False,
) -> tuple[list[dict], int, Optional[str]]:
    """解析数量化投入；构装可保留逐项顺序，其余入口默认合并同一实例。"""
    if not isinstance(raw_refs, list) or not raw_refs:
        return [], 0, selection_error

    merged: dict[tuple, dict] = {}
    ordered_refs: list[dict] = []
    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue
        source = str(ref.get('source') or '').strip()
        name = str(ref.get('name') or '').strip()
        item_id = ref.get('id')
        if source not in {'carry', 'warehouse', 'any'}:
            continue
        if not name and item_id is None:
            continue
        qty = ref.get('quantity')
        if qty is None:
            count = 1
        else:
            try:
                count = int(qty)
            except (TypeError, ValueError):
                return [], 0, '无效的物品数量'
            if count < 1:
                continue
        key = (source, item_id, name)
        if preserve_order:
            ordered_refs.append({
                'source': source, 'name': name, 'id': item_id, 'quantity': count,
            })
            continue
        if key in merged:
            merged[key]['quantity'] += count
        else:
            merged[key] = {
                'source': source,
                'name': name,
                'id': item_id,
                'quantity': count,
            }

    compact_refs = ordered_refs if preserve_order else list(merged.values())
    total = sum(int(r.get('quantity') or 0) for r in compact_refs)
    if total < min_count or total > max_count:
        return [], total, selection_error
    return compact_refs, total, None


def resolve_item_refs(
    adv,
    raw_refs: list | None,
    *,
    min_count: int,
    max_count: int,
    selection_error: str,
    preserve_order: bool = False,
) -> tuple[list[dict], Optional[str]]:
    """解析并校验数量化物品投入（含库存），返回 compact refs（含 quantity）。"""
    compact_refs, _, err = parse_item_refs(
        raw_refs,
        min_count=min_count,
        max_count=max_count,
        selection_error=selection_error,
        preserve_order=preserve_order,
    )
    if err:
        return [], err
    resolved_by_identity: dict[tuple[str, int], dict] = {}
    ordered_resolved: list[dict] = []
    for ref in compact_refs:
        item, item_err, resolved_source = resolve_item_ref(adv, ref)
        if item_err:
            return [], item_err
        need = int(ref.get('quantity') or 1)
        identity = (str(resolved_source), int(item.id))
        if preserve_order:
            ordered_resolved.append({
                'source': resolved_source, 'id': item.id,
                'name': item.name, 'quantity': need,
            })
        if identity not in resolved_by_identity:
            resolved_by_identity[identity] = {
                'source': resolved_source,
                'id': item.id,
                'name': item.name,
                'quantity': 0,
                '_item': item,
            }
        resolved_by_identity[identity]['quantity'] += need

    resolved: list[dict] = []
    for entry in resolved_by_identity.values():
        item = entry.pop('_item')
        need = int(entry['quantity'])
        available = item_quantity_of(item)
        if need > available:
            return [], f'「{item.name}」数量不足（需要 {need}，仅有 {available}）'
        resolved.append(entry)
    return ordered_resolved if preserve_order else resolved, None


def expand_item_refs(compact_refs: list[dict]) -> list[dict]:
    """将 compact refs 展开为每条消耗 1 的 ref 列表。"""
    expanded: list[dict] = []
    for ref in compact_refs or []:
        if not isinstance(ref, dict):
            continue
        try:
            count = int(ref.get('quantity') or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, count)
        base = {
            'source': ref.get('source'),
            'id': ref.get('id'),
            'name': ref.get('name'),
        }
        for _ in range(count):
            expanded.append(dict(base))
    return expanded


# 兼容既有炼金调用和测试；新工坊统一使用上面的通用数量接口。
def alchemy_item_refs_total(refs: list | None) -> int:
    return item_refs_total(refs)


def parse_alchemy_item_refs(
    raw_refs: list | None,
) -> tuple[list[dict], int, Optional[str]]:
    return parse_item_refs(
        raw_refs,
        min_count=1,
        max_count=5,
        selection_error='炼金工房需要选择 1~5 件物品',
    )


def resolve_alchemy_item_refs(
    adv,
    raw_refs: list | None,
) -> tuple[list[dict], Optional[str]]:
    return resolve_item_refs(
        adv,
        raw_refs,
        min_count=1,
        max_count=5,
        selection_error='炼金工房需要选择 1~5 件物品',
    )


def expand_alchemy_item_refs(compact_refs: list[dict]) -> list[dict]:
    return expand_item_refs(compact_refs)


def consume_item_ref(
    adventurer_id: int,
    ref: dict,
    amount: int = 1,
) -> Tuple[bool, Optional[str]]:
    """按 item_ref（source + id）消耗堆叠数量。返回 (是否已删行, error)。"""
    source = str(ref.get('source') or '').strip()
    if source not in {'carry', 'warehouse'}:
        return False, '无效的物品来源'
    try:
        item_id = int(ref.get('id'))
    except (TypeError, ValueError):
        return False, '无效的物品 id'

    if source == 'carry':
        item = (
            AdventurerS1Equipment.query
            .filter_by(adventurer_id=adventurer_id, id=item_id)
            .with_for_update()
            .first()
        )
    else:
        lock_warehouse_aggregate(adventurer_id)
        item = (
            AdventurerS1WarehouseItem.query
            .filter_by(adventurer_id=adventurer_id, id=item_id)
            .with_for_update()
            .first()
        )
    if not item:
        return False, '未找到指定物品'
    return consume_item_stack(item, amount)


def consume_item_stack(
    item: Union[AdventurerS1Equipment, AdventurerS1WarehouseItem],
    amount: int = 1,
    *,
    source_type: str = '',
    description: str = '',
    hidden_from_player: bool = False,
    related_id: str | None = None,
    related_type: str | None = None,
    flush: bool = True,
) -> Tuple[bool, Optional[str]]:
    """从堆叠物品扣除数量；数量归零则删除行。返回 (是否已删除, error)。

    hidden_from_player=True：日志照写（可查 + 申诉AI证据可见），但不进玩家「我的动态」。
    """
    amount = coerce_item_quantity(amount, 1)
    current = item_quantity_of(item)
    if amount > current:
        return False, f'「{item.name}」数量不足'
    # 删行前先捕获字段供日志使用（delete flush 后再读 ORM 字段不稳）
    log_adv, log_name = item.adventurer_id, item.name
    log_type, log_rarity = item.type or '', item.rarity or 'common'
    if amount >= current:
        from sqlalchemy import inspect
        state = inspect(item)
        if state.pending:
            state.session.expunge(item)
            ok, err = True, None
        else:
            ok, err = db_manager.delete_instance(item, flush=flush)
        if not ok:
            return False, err or '消耗物品失败'
    else:
        item.quantity = current - amount

    # 日志
    if source_type:
        log_ok, log_err = db_manager.add(AdventurerS1ItemLog(
            adventurer_id=log_adv, item_name=log_name,
            item_type=log_type, item_rarity=log_rarity,
            change_type='consume', quantity=amount,
            source_type=source_type,
            description=description or f'消耗「{log_name}」',
            hidden_from_player=hidden_from_player,
            related_id=related_id,
            related_type=related_type,
        ), flush=flush)
        if not log_ok:
            return amount >= current, log_err or '保存物品消耗流水失败'
    return amount >= current, None


def build_consumed_item_restore_snapshot(
    item: Union[AdventurerS1Equipment, AdventurerS1WarehouseItem],
    source: str,
    amount: int = 1,
) -> dict:
    """冻结实际消耗实例的原位置与可变属性，供同一事务链精确补偿。"""
    normalized_source = str(source or '').strip()
    if normalized_source not in {'carry', 'warehouse'}:
        raise ValueError('无效的物品来源')
    quantity = coerce_item_quantity(amount, 1)
    if quantity > item_quantity_of(item):
        raise ValueError(f'「{item.name}」数量不足')
    snapshot = {
        'source': normalized_source,
        'row_id': int(item.id),
        'name': str(item.name or ''),
        'type': str(item.type or ''),
        'rarity': str(item.rarity or 'common'),
        'quantity': quantity,
        'uses_left': item.uses_left,
        'custom_desc': item.custom_desc,
        'evolution_stage': int(getattr(item, 'evolution_stage', 0) or 0),
    }
    if isinstance(item, AdventurerS1Equipment):
        snapshot.update({
            'damage_stage': item.damage_stage,
            'damage_note': item.damage_note,
        })
    return snapshot


def consume_item_ref_with_restore_snapshot(
    adventurer_id: int,
    ref: dict,
    *,
    amount: int = 1,
    source_type: str,
    description: str,
    operation: str,
    action_label: str,
    related_id: str | None = None,
    related_type: str | None = None,
) -> Tuple[bool, Optional[str], Optional[dict]]:
    """按实例引用加锁消耗，并返回本次实际扣除物品的精确恢复快照。"""
    if not isinstance(ref, dict):
        return False, '无效的物品引用类型', None
    source = str(ref.get('source') or '').strip()
    if source not in {'carry', 'warehouse'}:
        return False, '无效的物品来源', None
    try:
        item_id = int(ref.get('id'))
    except (TypeError, ValueError):
        return False, '无效的物品 id', None

    if source == 'carry':
        item = (
            AdventurerS1Equipment.query
            .filter_by(adventurer_id=adventurer_id, id=item_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    else:
        lock_warehouse_aggregate(adventurer_id)
        item = (
            AdventurerS1WarehouseItem.query
            .filter_by(adventurer_id=adventurer_id, id=item_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    if not item:
        return False, '未找到指定物品', None
    operation_err = validate_item_operation(item, operation, action_label=action_label)
    if operation_err:
        return False, operation_err, None
    try:
        snapshot = build_consumed_item_restore_snapshot(item, source, amount)
    except ValueError as exc:
        return False, str(exc), None
    deleted, consume_err = consume_item_stack(
        item,
        amount,
        source_type=source_type,
        description=description,
        related_id=related_id,
        related_type=related_type,
    )
    if consume_err:
        return deleted, consume_err, None
    return deleted, None, snapshot


def remove_item_row(
    item: Union[AdventurerS1Equipment, AdventurerS1WarehouseItem],
    *,
    source_type: str,
    description: str = '',
    hidden_from_player: bool = False,
) -> Tuple[bool, Optional[str]]:
    """统一物品整行移除入口：删整行 + 必写一条 ItemLog consume（按整堆数量）。

    所有「整条删除物品」的特权路径（管理员后台删物、数据申诉删物）都应走这里，
    禁止再裸调 db_manager.delete_instance —— 否则物品流失不留痕，会变成申诉里的「无故消失」。
    hidden_from_player 控制是否对玩家「我的动态」隐身（申诉AI证据始终可见）。
    返回 (是否已删除, error)。
    """
    return consume_item_stack(
        item, item_quantity_of(item),
        source_type=source_type, description=description,
        hidden_from_player=hidden_from_player,
    )


# ── 统一消耗入口 ────────────────────────────────────────────────────────────

def consume_one_by_name_carry_only(
    adventurer_id: int,
    name: str,
    *,
    source_type: str,
    description: str = '',
    hidden_from_player: bool = False,
) -> Tuple[bool, Optional[str]]:
    """按名消耗一件随身物品（不含仓库）。特殊机缘门票建队/入队/发车扣票用。

    返回 (是否扣除成功, error)。调用方需在事务内调用并自行加成员行锁。
    """
    clean = str(name or '').strip()
    if not clean:
        return False, '物品名称为空'
    item = (
        AdventurerS1Equipment.query
        .filter_by(adventurer_id=adventurer_id, name=clean)
        .order_by(AdventurerS1Equipment.id.asc())
        .with_for_update()
        .first()
    )
    if not item:
        return False, f'随身没有「{clean}」'
    _, consume_err = consume_item_stack(
        item, 1, source_type=source_type,
        description=description or f'消耗「{clean}」',
        hidden_from_player=hidden_from_player,
    )
    if consume_err:
        return False, consume_err
    from plugins.adventurer_s1.services.s1_item_grant_service import invalidate_special_dungeons_ticket_cache
    invalidate_special_dungeons_ticket_cache(adventurer_id)
    return True, None


def consume_one_by_name_any_location(
    adventurer_id: int,
    name: str,
    *,
    source_type: str,
    description: str = '',
    hidden_from_player: bool = False,
) -> Tuple[bool, Optional[str]]:
    """按名消耗一件物品，随身优先、其次仓库（通用凭证扣除用）。

    返回 (是否扣除成功, error)。调用方需在事务内调用并自行加成员行锁。
    """
    clean = str(name or '').strip()
    if not clean:
        return False, '物品名称为空'
    item = (
        AdventurerS1Equipment.query
        .filter_by(adventurer_id=adventurer_id, name=clean)
        .order_by(AdventurerS1Equipment.id.asc())
        .with_for_update()
        .first()
    )
    if not item:
        lock_warehouse_aggregate(adventurer_id)
        item = (
            AdventurerS1WarehouseItem.query
            .filter_by(adventurer_id=adventurer_id, name=clean)
            .order_by(AdventurerS1WarehouseItem.id.asc())
            .with_for_update()
            .first()
        )
    if not item:
        return False, f'没有「{clean}」'
    _, consume_err = consume_item_stack(
        item, 1, source_type=source_type,
        description=description or f'消耗「{clean}」',
        hidden_from_player=hidden_from_player,
    )
    if consume_err:
        return False, consume_err
    from plugins.adventurer_s1.services.s1_item_grant_service import invalidate_special_dungeons_ticket_cache
    invalidate_special_dungeons_ticket_cache(adventurer_id)
    return True, None


def consume_item(
    adventurer_id: int,
    name_or_ref,
    *,
    amount: int = 1,
    source_type: str = 'unknown',
    description: str = '',
    operation: str,
    action_label: str = '该操作',
    related_id: str | None = None,
    related_type: str | None = None,
) -> Tuple[bool, Optional[str]]:
    """统一消耗物品。支持两种输入：
    - name_or_ref 是 str → 按 name FIFO 消耗（探险结算用）
    - name_or_ref 是 dict {source, id} → 按 ref 消耗（建筑/锻造用）

    Returns: (是否已删行, error)
    """
    if isinstance(name_or_ref, str):
        # 按 name FIFO
        e = resolve_equipment_by_name_fifo(adventurer_id, name_or_ref)
        if not e:
            return False, f'随身没有「{name_or_ref}」'
        operation_err = validate_item_operation(e, operation, action_label=action_label)
        if operation_err:
            return False, operation_err
        return consume_item_stack(
            e,
            amount,
            source_type=source_type,
            description=description,
            related_id=related_id,
            related_type=related_type,
        )
    elif isinstance(name_or_ref, dict):
        # 按 ref
        source = str(name_or_ref.get('source') or '').strip()
        if source not in {'carry', 'warehouse'}:
            return False, '无效的物品来源'
        try:
            item_id = int(name_or_ref.get('id'))
        except (TypeError, ValueError):
            return False, '无效的物品 id'

        if source == 'carry':
            item = (
                AdventurerS1Equipment.query
                .filter_by(adventurer_id=adventurer_id, id=item_id)
                .with_for_update()
                .first()
            )
        else:
            lock_warehouse_aggregate(adventurer_id)
            item = (
                AdventurerS1WarehouseItem.query
                .filter_by(adventurer_id=adventurer_id, id=item_id)
                .with_for_update()
                .first()
            )
        if not item:
            return False, '未找到指定物品'
        operation_err = validate_item_operation(item, operation, action_label=action_label)
        if operation_err:
            return False, operation_err
        return consume_item_stack(
            item,
            amount,
            source_type=source_type,
            description=description,
            related_id=related_id,
            related_type=related_type,
        )
    else:
        return False, '无效的物品引用类型'


# ── 堆叠拆分 / 合并（玩家可选数量的移动操作用：取出/存入/挂单等） ──────────────

def resolve_move_quantity(row, requested) -> Tuple[int, Optional[str]]:
    """计算本次要移动的数量。

    requested 为 None → 整堆（向后兼容旧客户端/AI）；否则按 1~堆叠数 校验。
    申诉改量限制仅作用于数据申诉链路（S1AppealLineageGuard / bug reclaim），
    不拦截正常的取出、存入、上架等库存操作。
    返回 (move_quantity, error)。
    """
    current = item_quantity_of(row)
    if requested is None:
        return current, None
    qty = coerce_item_quantity(requested, 1)
    if qty > current:
        return 0, f'数量不能超过堆叠数量 {current}'
    return qty, None


def lock_warehouse_aggregate(adventurer_id: int) -> None:
    """Serialize one adventurer's warehouse writers for this transaction.

    PostgreSQL row locks cannot protect an empty warehouse or a COUNT predicate. A transaction-level
    advisory lock supplies that stable aggregate lock without taking AdventurerS1 late in transactions
    that already own unrelated container rows. SQLite tests stay single-process and need no equivalent.
    """
    lock_warehouse_aggregates([adventurer_id])


def lock_warehouse_aggregates(adventurer_ids) -> None:
    """Acquire warehouse aggregate locks in ascending ID order with one DB round trip."""
    normalized_ids = sorted({int(value) for value in (adventurer_ids or [])})
    if any(value <= 0 for value in normalized_ids):
        raise RuntimeError('冒险者 ID 无效')
    if not normalized_ids:
        return
    session = db_manager.query(AdventurerS1WarehouseItem).session
    if session.get_bind().dialect.name != 'postgresql':
        return
    if len(normalized_ids) == 1:
        result, _ = db_manager.execute_statement(
            select(func.pg_advisory_xact_lock(
                _WAREHOUSE_AGGREGATE_LOCK_NAMESPACE,
                normalized_ids[0],
            )),
            raise_errors=True,
        )
        result.scalar()
        return
    result, _ = db_manager.execute_statement(
        text('''
            SELECT pg_advisory_xact_lock(:namespace, adventurer_id)
            FROM (
                SELECT DISTINCT unnest(CAST(:adventurer_ids AS INTEGER[])) AS adventurer_id
            ) AS ordered_locks
            ORDER BY adventurer_id
        '''),
        {
            'namespace': _WAREHOUSE_AGGREGATE_LOCK_NAMESPACE,
            'adventurer_ids': normalized_ids,
        },
        raise_errors=True,
    )
    result.fetchall()


def find_mergeable_stack(
    model,
    adventurer_id: int,
    *,
    name: str,
    item_type,
    rarity,
    custom_desc,
    uses_left,
    evolution_stage=0,
    recovery_minutes=0,
    recovery_started_at=None,
    recovery_ends_at=None,
    is_player_locked: bool = False,
    damage_stage=ITEM_ATTRIBUTE_UNSET,
    damage_note=ITEM_ATTRIBUTE_UNSET,
    for_update: bool = False,
):
    """在目标容器找一个完全同属性的堆用于合并；无则 None。"""
    normalized_custom_desc = normalize_stack_custom_desc(custom_desc)
    filters = {
        'adventurer_id': adventurer_id,
        'name': name,
        'type': item_type,
        'rarity': rarity,
        'uses_left': uses_left,
        'evolution_stage': evolution_stage,
        'is_player_locked': bool(is_player_locked),
    }
    if model is AdventurerS1Equipment:
        if damage_stage is not ITEM_ATTRIBUTE_UNSET:
            filters['damage_stage'] = damage_stage
        if damage_note is not ITEM_ATTRIBUTE_UNSET:
            filters['damage_note'] = damage_note
    from plugins.adventurer_s1.services.s1_asset_recovery_service import (
        filter_recovery_mergeable_rows,
    )

    query = model.query.filter_by(**filters)
    if normalized_custom_desc is None:
        query = query.filter(or_(model.custom_desc.is_(None), model.custom_desc == ''))
    else:
        query = query.filter(model.custom_desc == normalized_custom_desc)
    query = filter_recovery_mergeable_rows(
        query,
        model,
        recovery_minutes=recovery_minutes,
        recovery_started_at=recovery_started_at,
        recovery_ends_at=recovery_ends_at,
    ).order_by(model.id.asc())
    if for_update:
        query = query.populate_existing().with_for_update()
    return query.first()


def take_from_stack(row, amount: int) -> bool:
    """从 row 扣除 amount（须在事务内、amount 已校验 ≤ 当前数量时调用）。

    数量归零 → 删行返回 True；否则原地减少返回 False。
    """
    current = item_quantity_of(row)
    if amount >= current:
        db_manager.delete_instance(row, flush=False)
        return True
    row.quantity = current - amount
    return False


def add_or_merge_stack(
    model,
    adventurer_id: int,
    *,
    name: str,
    item_type,
    rarity,
    custom_desc,
    uses_left,
    evolution_stage=0,
    recovery_minutes=0,
    recovery_started_at=None,
    recovery_ends_at=None,
    is_player_locked: bool = False,
    quantity: int,
):
    """向目标容器加入 quantity 个物品：同属性堆存在则合并，否则新建一行（须在事务内调用）。

    返回 (row, is_new)。
    """
    qty = coerce_item_quantity(quantity, 1)
    if model is AdventurerS1WarehouseItem:
        lock_warehouse_aggregate(adventurer_id)
    target = find_mergeable_stack(
        model, adventurer_id, name=name, item_type=item_type,
        rarity=rarity, custom_desc=custom_desc, uses_left=uses_left,
        evolution_stage=evolution_stage,
        recovery_minutes=recovery_minutes,
        recovery_started_at=recovery_started_at,
        recovery_ends_at=recovery_ends_at,
        is_player_locked=is_player_locked,
        for_update=True,
    )
    if target is not None:
        target.quantity = item_quantity_of(target) + qty
        return target, False
    row = model(
        adventurer_id=adventurer_id, name=name, type=item_type,
        rarity=rarity, custom_desc=normalize_stack_custom_desc(custom_desc),
        uses_left=uses_left,
        evolution_stage=evolution_stage,
        recovery_minutes=recovery_minutes,
        recovery_started_at=recovery_started_at,
        recovery_ends_at=recovery_ends_at,
        is_player_locked=bool(is_player_locked),
        quantity=qty,
    )
    ok, err = db_manager.add(row)
    if not ok:
        raise RuntimeError(err)
    return row, True
