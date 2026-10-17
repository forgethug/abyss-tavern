"""收购商行：按类型+稀有度底价折价收购随身/仓库物品，商店定价商品再 cap 为商店价一半防套利。"""
from runtime_core.time import utc_now as _portable_utc_now
import math
from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.s1_item_types import build_recycle_type_floors
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_item_quantity_service import (
    coerce_item_quantity,
    item_quantity_of,
)
from utils.time_utils import to_utc_iso

_HANDLER_KEY = 'buyback_shop'
_VALID_RARITIES = frozenset({'common', 'rare', 'epic', 'legendary'})
_VALID_SOURCES = frozenset({'equipment', 'warehouse'})

# 与 plugin.default_config 同值兜底（测试环境不走 PluginLoader）
_DEFAULT_TYPE_FLOORS = build_recycle_type_floors()
_DEFAULT_RARITY_FLOORS = {
    'common': 10, 'rare': 60, 'epic': 400, 'legendary': 2000,
}


class S1BuybackService:
    """收购商行业务逻辑。"""

    @staticmethod
    def _get_type_floors() -> dict:
        cfg = current_app.config.get('ADVENTURER_S1_RECYCLE_TYPE_FLOORS')
        return cfg if isinstance(cfg, dict) else _DEFAULT_TYPE_FLOORS

    @staticmethod
    def _get_repurchase_hours() -> float:
        try:
            value = float(current_app.config.get(
                'ADVENTURER_S1_APPEAL_BUYBACK_REPURCHASE_HOURS', 24,
            ))
        except (TypeError, ValueError):
            value = 24
        return max(1.0, min(168.0, value))

    @staticmethod
    def _get_default_floors() -> dict:
        cfg = current_app.config.get('ADVENTURER_S1_RECYCLE_DEFAULT_FLOORS')
        return cfg if isinstance(cfg, dict) else _DEFAULT_RARITY_FLOORS

    @staticmethod
    def _get_recycle_rate() -> float:
        try:
            return float(current_app.config.get('ADVENTURER_S1_RECYCLE_RATE', 0.5))
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _floor_price(item_type: str, rarity: str) -> int:
        rarity = rarity if rarity in _VALID_RARITIES else 'common'
        type_floors = S1BuybackService._get_type_floors()
        type_key = (item_type or '').strip().lower()
        if type_key in type_floors:
            floor = type_floors[type_key].get(rarity)
            if floor is not None:
                return max(0, int(floor))
        default_floors = S1BuybackService._get_default_floors()
        return max(0, int(default_floors.get(rarity, 10)))

    @staticmethod
    def compute_recycle_price(name: str, item_type: str, rarity: str) -> int:
        """商店商品按半价单价回收（如 12 金/10 支箭 → 1 金/支）；非商店商品走底价×回收比例。"""
        from plugins.adventurer_s1.seed import (
            get_s1_any_shop_catalog_price,
            get_s1_any_shop_catalog_purchase_quantity,
        )

        shop_price = get_s1_any_shop_catalog_price(name)
        if shop_price is not None and shop_price > 0:
            qty = get_s1_any_shop_catalog_purchase_quantity(name)
            return max(1, int(round(shop_price / max(qty, 1) / 2)))

        floor = S1BuybackService._floor_price(item_type, rarity)
        rate = S1BuybackService._get_recycle_rate()
        return max(1, int(math.floor(floor * rate)))

    @staticmethod
    def _validate_building(inst: Optional[AdventurerS1BuildingInstance]) -> Tuple[Optional[AdventurerS1BuildingInstance], Optional[str]]:
        if not inst:
            return None, '建筑不存在'
        if inst.status != 'built':
            return None, '建筑尚未建成'
        template = inst.template
        if not template or template.handler_key != _HANDLER_KEY:
            return None, '该建筑不是收购商行'
        return inst, None

    @staticmethod
    def _get_user_adventurer(user_id: int) -> Tuple[Optional[AdventurerS1], Optional[str]]:
        adv = (
            AdventurerS1.query.filter_by(owner_id=user_id)
            .filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES))
            .order_by(AdventurerS1.id.asc())
            .first()
        )
        if not adv:
            return None, '你还没有冒险者，请先创建'
        return adv, None

    @staticmethod
    def _item_desc(name: str, custom_desc: Optional[str]) -> str:
        if custom_desc:
            return custom_desc
        row = AdventurerS1Item.query.filter_by(name=name).first()
        return (row.description or '') if row else ''

    @staticmethod
    def _serialize_sellable(
        item_id: int,
        source: str,
        name: str,
        item_type: str,
        rarity: str,
        custom_desc: Optional[str],
        uses_left: Optional[int],
        created_at,
        quantity: int = 1,
        catalog: Optional[AdventurerS1Item] = None,
        is_player_locked: bool = False,
    ) -> dict:
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            build_clue_attach_meta,
            is_hidden_boss_clue_item,
            sanitize_player_catalog_item_dict,
        )

        clue_catalog = bool(
            catalog is not None and is_hidden_boss_clue_item(catalog.use_condition)
        )
        effective_rarity = (
            str(catalog.rarity or 'common') if clue_catalog else rarity
        )
        reject_reason = None
        if is_player_locked:
            reject_reason = f'「{name}」已受保护，请先解锁再出售'
        elif effective_rarity not in _VALID_RARITIES:
            reject_reason = f'「{name}」稀有度异常，无法出售'

        stack_qty = coerce_item_quantity(quantity, 1)
        recycle_price = S1BuybackService.compute_recycle_price(
            name,
            item_type,
            effective_rarity,
        )
        payload = {
            'id': item_id,
            'source': source,
            'name': name,
            'type': item_type,
            'description': custom_desc or ((catalog.description or '') if catalog else ''),
            'rarity': effective_rarity or 'common',
            'uses_left': uses_left,
            'created_at': to_utc_iso(created_at) if created_at else None,
            'recycle_price': recycle_price,
            'can_sell': reject_reason is None,
            'reject_reason': reject_reason,
            'is_player_locked': bool(is_player_locked),
        }
        if stack_qty > 1:
            payload['quantity'] = stack_qty
            payload['recycle_total'] = recycle_price * stack_qty
        sanitize_player_catalog_item_dict(
            payload,
            build_clue_attach_meta(catalog) if catalog is not None else None,
            name_key='name',
            desc_key='description',
            type_key='type',
        )
        return payload

    @staticmethod
    def list_sellable(
        building_instance_id: int,
        user_id: int,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        inst, err = S1BuybackService._validate_building(
            AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id),
        )
        if err:
            return None, err

        adv, err = S1BuybackService._get_user_adventurer(user_id)
        adv_summary = None
        gold_balance = 0
        carry_items = []
        warehouse_items = []

        if adv:
            balance, bal_err = S1CurrencyService.get_balance(adv.id, 'gold')
            if bal_err:
                return None, bal_err
            gold_balance = int(balance or 0)
            adv_summary = {
                'id': adv.id,
                'name': adv.name,
                'gold': gold_balance,
            }
            equipment_rows = sorted(adv.equipment_items, key=lambda x: x.id)
            warehouse_rows = sorted(adv.warehouse_items, key=lambda x: x.id)
            item_names = {
                row.name for row in [*equipment_rows, *warehouse_rows] if row.name
            }
            catalog_by_name = {
                row.name: row
                for row in (
                    AdventurerS1Item.query
                    .filter(AdventurerS1Item.name.in_(sorted(item_names)))
                    .all()
                )
            } if item_names else {}
            for eq in equipment_rows:
                carry_items.append(
                    S1BuybackService._serialize_sellable(
                        eq.id, 'equipment', eq.name, eq.type,
                        eq.rarity or 'common', eq.custom_desc, eq.uses_left,
                        eq.created_at,
                        item_quantity_of(eq),
                        catalog_by_name.get(eq.name),
                        is_player_locked=eq.is_player_locked,
                    )
                )
            for wh in warehouse_rows:
                warehouse_items.append(
                    S1BuybackService._serialize_sellable(
                        wh.id, 'warehouse', wh.name, wh.type,
                        wh.rarity or 'common', wh.custom_desc, wh.uses_left,
                        wh.created_at,
                        item_quantity_of(wh),
                        catalog_by_name.get(wh.name),
                        is_player_locked=wh.is_player_locked,
                    )
                )

        template = inst.template
        return {
            'building_id': inst.id,
            'shop_name': template.name if template else '收购商行',
            'adventurer': adv_summary,
            'carry_items': carry_items,
            'warehouse_items': warehouse_items,
        }, None

    @staticmethod
    def sell_item(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        source: str,
        item_id: int,
        quantity: Optional[int] = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        data, err = S1BuybackService.sell_items(
            building_instance_id=building_instance_id,
            adventurer_id=adventurer_id,
            user_id=user_id,
            items=[{
                'source': source,
                'item_id': item_id,
                'quantity': quantity,
            }],
            is_admin=is_admin,
        )
        if err:
            return None, err

        sold = data['sold_items'][0]
        return {
            'sold': sold,
            'adventurer': data['adventurer'],
        }, None

    @staticmethod
    def _normalize_sell_requests(items: list) -> Tuple[Optional[list[dict]], Optional[str]]:
        if not isinstance(items, list) or not items:
            return None, 'items 必须是非空数组'

        normalized = []
        seen = set()
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                return None, f'items[{index}] 必须是对象'

            source = str(raw.get('source') or '').strip().lower()
            if source not in _VALID_SOURCES:
                return None, f'items[{index}].source 必须是 equipment 或 warehouse'

            try:
                item_id = int(raw.get('item_id'))
            except (TypeError, ValueError):
                return None, f'items[{index}].item_id 必须是正整数'
            if item_id <= 0:
                return None, f'items[{index}].item_id 必须是正整数'

            quantity = raw.get('quantity')
            if quantity is not None:
                try:
                    quantity = int(quantity)
                except (TypeError, ValueError):
                    return None, f'items[{index}].quantity 必须是正整数'
                if quantity <= 0:
                    return None, f'items[{index}].quantity 必须是正整数'

            key = (source, item_id)
            if key in seen:
                return None, f'items[{index}] 与前面的物品重复'
            seen.add(key)
            normalized.append({
                'source': source,
                'item_id': item_id,
                'quantity': quantity,
            })
        return normalized, None

    @staticmethod
    def sell_items(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        items: list,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """在一个事务内出售多个随身/仓库物品；任一项失败则整批回滚。"""
        inst, err = S1BuybackService._validate_building(
            AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id),
        )
        if err:
            return None, err

        normalized, err = S1BuybackService._normalize_sell_requests(items)
        if err:
            return None, err
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            build_clue_attach_meta,
            is_hidden_boss_clue_item,
            sanitize_player_catalog_item_dict,
            sanitize_player_clue_error,
        )
        from plugins.adventurer_s1.services.s1_item_instance_service import (
            ITEM_OPERATION_PLAYER_DESTRUCTIVE,
            validate_item_operation,
        )

        template_name = inst.template.name if inst.template else '收购商行'
        sold_items = []
        currency_entries = []
        gold_total = 0
        new_balance: float | None = None
        adv_name = ''
        catalog_by_name: dict[str, AdventurerS1Item] = {}
        clue_meta_by_name: dict[str, dict] = {}

        try:
            with db_manager.transaction():
                adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
                if not adv:
                    raise RuntimeError('冒险者不存在')
                if not is_admin and adv.owner_id != user_id:
                    raise RuntimeError('只能让自己的冒险者出售物品')
                can, reason = adv.can_dispatch()
                if not can:
                    raise RuntimeError(reason or '当前状态无法出售物品')
                adv_name = adv.name

                equipment_ids = sorted(
                    row['item_id'] for row in normalized if row['source'] == 'equipment'
                )
                warehouse_ids = sorted(
                    row['item_id'] for row in normalized if row['source'] == 'warehouse'
                )
                locked_items = {}
                if equipment_ids:
                    rows = (
                        AdventurerS1Equipment.query
                        .filter(
                            AdventurerS1Equipment.adventurer_id == adventurer_id,
                            AdventurerS1Equipment.id.in_(equipment_ids),
                        )
                        .order_by(AdventurerS1Equipment.id.asc())
                        .with_for_update()
                        .all()
                    )
                    locked_items.update({('equipment', row.id): row for row in rows})
                if warehouse_ids:
                    rows = (
                        AdventurerS1WarehouseItem.query
                        .filter(
                            AdventurerS1WarehouseItem.adventurer_id == adventurer_id,
                            AdventurerS1WarehouseItem.id.in_(warehouse_ids),
                        )
                        .order_by(AdventurerS1WarehouseItem.id.asc())
                        .with_for_update()
                        .all()
                    )
                    locked_items.update({('warehouse', row.id): row for row in rows})

                locked_names = {
                    item.name for item in locked_items.values() if item.name
                }
                if locked_names:
                    from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
                        CatalogBusyError,
                        lock_catalog_names,
                    )
                    if not lock_catalog_names(
                        locked_names,
                        shared=True,
                        wait=False,
                    ):
                        raise CatalogBusyError()
                    catalog_rows = (
                        AdventurerS1Item.query
                        .filter(AdventurerS1Item.name.in_(sorted(locked_names)))
                        .populate_existing()
                        .all()
                    )
                    catalog_by_name = {row.name: row for row in catalog_rows}
                    clue_meta_by_name = {
                        row.name: clue_meta
                        for row in catalog_rows
                        if is_hidden_boss_clue_item(row.use_condition)
                        and (clue_meta := build_clue_attach_meta(row)) is not None
                    }

                repurchase_expires_at = _portable_utc_now() + timedelta(
                    hours=S1BuybackService._get_repurchase_hours(),
                )
                for request_row in normalized:
                    source = request_row['source']
                    item_id = request_row['item_id']
                    item = locked_items.get((source, item_id))
                    if not item:
                        raise RuntimeError(f'{source} 物品 {item_id} 已不存在或已被移走')

                    protection_err = validate_item_operation(
                        item,
                        ITEM_OPERATION_PLAYER_DESTRUCTIVE,
                        action_label='出售',
                    )
                    if protection_err:
                        raise RuntimeError(protection_err)

                    catalog_row = catalog_by_name.get(item.name)
                    rarity = item.rarity or 'common'
                    if catalog_row is not None and is_hidden_boss_clue_item(
                        catalog_row.use_condition
                    ):
                        rarity = str(catalog_row.rarity or 'common')
                    if rarity not in _VALID_RARITIES:
                        raise RuntimeError(f'「{item.name}」稀有度异常，无法出售')

                    current_qty = item_quantity_of(item)
                    requested_qty = request_row['quantity']
                    sell_qty = current_qty if requested_qty is None else requested_qty
                    if sell_qty > current_qty:
                        raise RuntimeError(f'「{item.name}」出售数量不能超过堆叠数量 {current_qty}')

                    from plugins.adventurer_s1.services.s1_item_instance_service import (
                        build_consumed_item_restore_snapshot,
                    )
                    restore_snapshot = build_consumed_item_restore_snapshot(
                        item,
                        'carry' if source == 'equipment' else 'warehouse',
                        sell_qty,
                    )

                    unit_price = S1BuybackService.compute_recycle_price(
                        item.name, item.type, rarity,
                    )
                    item_gold_total = unit_price * sell_qty
                    # 隐藏线索等资产以出售时图鉴稀有度结价，买回快照同步冻结该权威值。
                    restore_snapshot['rarity'] = rarity

                    if sell_qty >= current_qty:
                        ok, del_err = db_manager.delete_instance(item, flush=False)
                        if not ok:
                            raise RuntimeError(del_err or f'删除「{item.name}」失败')
                    else:
                        item.quantity = current_qty - sell_qty

                    qty_note = f'×{sell_qty}' if sell_qty > 1 else ''
                    currency_entries.append({
                        'amount': float(item_gold_total),
                        'source_type': 'buyback_sell',
                        'related_id': item_id,
                        'related_type': source,
                        'description': f'{template_name}出售「{item.name}」{qty_note}',
                        'business_payload': {
                            'schema': 's1_buyback_sale_v1',
                            'item_snapshot': restore_snapshot,
                            'unit_price': unit_price,
                            'gold_total': item_gold_total,
                        },
                        'business_expires_at': repurchase_expires_at,
                    })
                    gold_total += item_gold_total
                    sold_items.append({
                        'id': item_id,
                        'source': source,
                        'name': item.name,
                        'recycle_price': unit_price,
                        'quantity': sell_qty,
                        'gold_total': item_gold_total,
                    })

                new_balance, add_err = S1CurrencyService.add_many(
                    adventurer_id,
                    'gold',
                    currency_entries,
                    auto_commit=False,
                )
                if add_err:
                    raise RuntimeError(add_err)
        except Exception as e:
            message = f'出售失败：{e}'
            for catalog_name in clue_meta_by_name:
                message = sanitize_player_clue_error(message, catalog_name)
            return None, message

        if sold_items and clue_meta_by_name:
            for sold in sold_items:
                catalog_name = str(sold.get('name') or '')
                sanitize_player_catalog_item_dict(
                    sold,
                    clue_meta_by_name.get(catalog_name),
                    name_key='name',
                )

        return {
            'sold_items': sold_items,
            'gold_total': gold_total,
            'adventurer': {
                'id': adventurer_id,
                'name': adv_name,
                'gold': int(new_balance or 0),
            },
        }, None
