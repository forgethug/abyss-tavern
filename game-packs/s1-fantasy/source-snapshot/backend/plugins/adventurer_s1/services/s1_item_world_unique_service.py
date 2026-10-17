"""S1 物品防复制：同队结算内禁止克隆队友持有物；图鉴 world_unique 仍限制全服发放。"""
from __future__ import annotations

from typing import Any

from flask import current_app
from sqlalchemy import literal, select, union_all

from extensions import db

from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1MarketListing,
    AdventurerS1SecretAuctionLot,
    AdventurerS1WarehouseItem,
)

from plugins.adventurer_s1.s1_item_types import (
    S1_ITEM_TYPE_COMPANION,
    companion_allows_duplicate_name_instances,
    normalize_item_type,
)
from plugins.adventurer_s1.services.s1_item_quantity_service import (
    coerce_item_quantity,
    item_quantity_of,
)

WORLD_UNIQUE_ITEM_TYPES = frozenset({
    S1_ITEM_TYPE_COMPANION, 'pet', 'familiar', 'creature', 'mount', 'partner',
    '灵宠', '伙伴', '宠物', '幼兽', '契约兽', '坐骑',
})


def infer_world_unique(name: str, item_type: str = '', desc: str = '') -> bool:
    """新建图鉴时按明确物品类型给出默认唯一性。"""
    clean_name = ' '.join(str(name or '').split())
    if not clean_name:
        return False
    type_key = str(item_type or '').strip().lower()
    return type_key in WORLD_UNIQUE_ITEM_TYPES


def resolve_world_unique_flag(
    name: str,
    *,
    item_type: str = '',
    desc: str = '',
    catalog_flag: bool | None = None,
) -> bool:
    """已有图鉴以持久化字段为准；仅在尚无图鉴时使用类型默认值。"""
    if catalog_flag is not None:
        return bool(catalog_flag)
    return infer_world_unique(name, item_type, desc)


def load_world_unique_occupancy(
    item_names: list[str],
    *,
    exclude_equipment_item_id: int | None = None,
    exclude_warehouse_item_id: int | None = None,
    exclude_market_listing_id: int | None = None,
    exclude_market_seller_adventurer_id: int | None = None,
    exclude_secret_auction_lot_id: int | None = None,
    exclude_team_loot_lot_id: int | None = None,
) -> dict[str, dict]:
    """用单个数据库快照读取持有者和交易托管，避免搬运提交时出现观测空窗。"""
    names = [str(n).strip() for n in (item_names or []) if str(n).strip()]
    if not names:
        return {}

    market_statement = select(
        AdventurerS1MarketListing.item_name.label('item_name'),
        literal(None).label('adventurer_id'),
        literal(True).label('escrowed'),
        AdventurerS1MarketListing.quantity.label('quantity'),
    ).where(
        AdventurerS1MarketListing.item_name.in_(names),
        AdventurerS1MarketListing.status == 'active',
    )
    if exclude_market_listing_id is not None:
        market_statement = market_statement.where(
            AdventurerS1MarketListing.id != int(exclude_market_listing_id),
        )
    if exclude_market_seller_adventurer_id is not None:
        market_statement = market_statement.where(
            AdventurerS1MarketListing.seller_adventurer_id
            != int(exclude_market_seller_adventurer_id),
        )

    equipment_statement = select(
        AdventurerS1Equipment.name.label('item_name'),
        AdventurerS1Equipment.adventurer_id.label('adventurer_id'),
        literal(False).label('escrowed'),
        AdventurerS1Equipment.quantity.label('quantity'),
    ).where(AdventurerS1Equipment.name.in_(names))
    if exclude_equipment_item_id is not None:
        equipment_statement = equipment_statement.where(
            AdventurerS1Equipment.id != int(exclude_equipment_item_id),
        )
    warehouse_statement = select(
        AdventurerS1WarehouseItem.name.label('item_name'),
        AdventurerS1WarehouseItem.adventurer_id.label('adventurer_id'),
        literal(False).label('escrowed'),
        AdventurerS1WarehouseItem.quantity.label('quantity'),
    ).where(AdventurerS1WarehouseItem.name.in_(names))
    if exclude_warehouse_item_id is not None:
        warehouse_statement = warehouse_statement.where(
            AdventurerS1WarehouseItem.id != int(exclude_warehouse_item_id),
        )

    statements = [equipment_statement, warehouse_statement, market_statement]
    auction_statement = select(
        AdventurerS1SecretAuctionLot.item_name.label('item_name'),
        literal(None).label('adventurer_id'),
        literal(True).label('escrowed'),
        AdventurerS1SecretAuctionLot.quantity.label('quantity'),
    ).where(
        AdventurerS1SecretAuctionLot.item_name.in_(names),
        AdventurerS1SecretAuctionLot.status == 'open',
    )
    if exclude_secret_auction_lot_id is not None:
        auction_statement = auction_statement.where(
            AdventurerS1SecretAuctionLot.id != int(exclude_secret_auction_lot_id),
        )
    statements.append(auction_statement)
    from plugins.adventurer_s1.team_loot_models import AdventurerS1TeamLootLot
    team_loot_statement = select(
        AdventurerS1TeamLootLot.item_name.label('item_name'),
        literal(None).label('adventurer_id'), literal(True).label('escrowed'),
        AdventurerS1TeamLootLot.quantity.label('quantity'),
    ).where(
        AdventurerS1TeamLootLot.item_name.in_(names),
        AdventurerS1TeamLootLot.status.in_(('voting', 'bidding', 'rolling', 'claim_pending', 'unclaimed')),
    )
    if exclude_team_loot_lot_id is not None:
        team_loot_statement = team_loot_statement.where(
            AdventurerS1TeamLootLot.id != int(exclude_team_loot_lot_id),
        )
    statements.append(team_loot_statement)

    occupancy = {
        name: {
            'holders': set(),
            'escrowed': False,
            'escrow_count': 0,
            'instance_count': 0,
            'quantity_total': 0,
        }
        for name in names
    }
    for item_name, adventurer_id, escrowed, quantity in db.session.execute(
        union_all(*statements),
    ).all():
        entry = occupancy.setdefault(item_name, {
            'holders': set(),
            'escrowed': False,
            'escrow_count': 0,
            'instance_count': 0,
            'quantity_total': 0,
        })
        row_quantity = coerce_item_quantity(quantity, 1)
        entry['quantity_total'] += row_quantity
        if escrowed:
            entry['escrowed'] = True
            entry['escrow_count'] += 1
            continue
        entry['instance_count'] += 1
        try:
            entry['holders'].add(int(adventurer_id))
        except (TypeError, ValueError):
            continue
    return occupancy


def load_item_holders_map(item_names: list[str]) -> dict[str, set[int]]:
    """批量查询物品名当前全服持有者（随身 + 仓库）。"""
    return {
        name: set(entry['holders'])
        for name, entry in load_world_unique_occupancy(item_names).items()
    }


def build_party_item_holders_map(
    members_or_ids,
    item_names: list[str] | None = None,
    *,
    include_warehouse: bool = True,
) -> dict[str, set[int]]:
    """统计本队成员持有的物品；支持 ORM 成员或跨 Session 冻结的成员 ID。"""
    names_filter = {str(n).strip() for n in (item_names or []) if str(n).strip()} or None
    member_ids: set[int] = set()
    for member_or_id in members_or_ids or []:
        try:
            raw_member_id = (
                member_or_id
                if isinstance(member_or_id, int) and not isinstance(member_or_id, bool)
                else member_or_id.id
            )
            member_id = int(raw_member_id)
            if member_id > 0:
                member_ids.add(member_id)
        except (AttributeError, TypeError, ValueError):
            continue
    if not member_ids:
        return {}

    holders_map: dict[str, set[int]] = {}
    holder_models = [AdventurerS1Equipment]
    if include_warehouse:
        holder_models.append(AdventurerS1WarehouseItem)
    for model in holder_models:
        query = model.query.with_entities(model.adventurer_id, model.name).filter(
            model.adventurer_id.in_(member_ids),
        )
        if names_filter is not None:
            query = query.filter(model.name.in_(names_filter))
        for adventurer_id, name in query.all():
            clean_name = str(name or '').strip()
            if clean_name:
                holders_map.setdefault(clean_name, set()).add(int(adventurer_id))
    return holders_map


def _load_catalog_by_name(item_names: list[str]) -> dict[str, AdventurerS1Item]:
    names = [str(n).strip() for n in (item_names or []) if str(n).strip()]
    if not names:
        return {}
    rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    return {row.name: row for row in rows}


def _load_holder_custom_desc_flags(
    pairs: list[tuple[int, str]],
    *,
    include_warehouse: bool = True,
) -> dict[tuple[int, str], bool]:
    if not pairs:
        return {}
    adv_ids = {int(adv_id) for adv_id, _ in pairs}
    names = {name for _, name in pairs}
    flags = {(adv_id, name): False for adv_id, name in pairs}

    eq_rows = (
        AdventurerS1Equipment.query
        .with_entities(
            AdventurerS1Equipment.adventurer_id,
            AdventurerS1Equipment.name,
            AdventurerS1Equipment.custom_desc,
        )
        .filter(
            AdventurerS1Equipment.adventurer_id.in_(adv_ids),
            AdventurerS1Equipment.name.in_(names),
        )
        .all()
    )
    wh_rows = []
    if include_warehouse:
        wh_rows = (
            AdventurerS1WarehouseItem.query
            .with_entities(
                AdventurerS1WarehouseItem.adventurer_id,
                AdventurerS1WarehouseItem.name,
                AdventurerS1WarehouseItem.custom_desc,
            )
            .filter(
                AdventurerS1WarehouseItem.adventurer_id.in_(adv_ids),
                AdventurerS1WarehouseItem.name.in_(names),
            )
            .all()
        )
    for adv_id, name, custom_desc in list(eq_rows) + list(wh_rows):
        key = (int(adv_id), name)
        if key in flags and str(custom_desc or '').strip():
            flags[key] = True
    return flags


def is_party_clone_blocked_item(
    name: str,
    holder_id: int,
    *,
    catalog_by_name: dict[str, AdventurerS1Item] | None = None,
    holder_custom_desc: bool | None = None,
) -> bool:
    """队内：队友已持有时，是否应禁止再 clone 给另一人（不含副本新掉落可量产物）。"""
    clean_name = str(name or '').strip()
    if not clean_name:
        return False

    catalog = (catalog_by_name or {}).get(clean_name)
    if catalog is None:
        catalog = AdventurerS1Item.query.filter_by(name=clean_name).first()

    if catalog is None:
        return True

    if bool(getattr(catalog, 'world_unique', False)):
        return True

    if holder_custom_desc is None:
        holder_custom_desc = _load_holder_custom_desc_flags([(holder_id, clean_name)]).get(
            (holder_id, clean_name), False,
        )
    if holder_custom_desc:
        return True

    return False


def resolve_guard_item_type(
    name: str,
    *,
    catalog_by_name: dict[str, AdventurerS1Item] | None = None,
    item_type: str | None = None,
) -> str:
    """解析防重复发放用的物品 type（优先显式传入，其次图鉴）。"""
    if item_type and str(item_type).strip():
        return normalize_item_type(item_type)
    clean_name = str(name or '').strip()
    if not clean_name:
        return ''
    catalog = (catalog_by_name or {}).get(clean_name)
    if catalog is None:
        catalog = AdventurerS1Item.query.filter_by(name=clean_name).first()
    return normalize_item_type(getattr(catalog, 'type', '') or '')


def world_unique_promotion_error(name: str, *, item_type: str = '') -> str | None:
    """Reject promotion when the current single-snapshot occupancy is already ambiguous."""
    clean_name = str(name or '').strip()
    if not clean_name:
        return None
    occupancy = load_world_unique_occupancy([clean_name]).get(clean_name)
    if not occupancy:
        return None
    holders = set(occupancy['holders'])
    escrow_count = int(occupancy.get('escrow_count') or 0)
    if companion_allows_duplicate_name_instances(item_type):
        if len(holders) > 1 or escrow_count > 1 or (holders and escrow_count):
            return f'无法设为全服唯一：物品「{clean_name}」已有多个持有者或托管来源'
        return None
    if int(occupancy.get('quantity_total') or 0) > 1:
        return f'无法设为全服唯一：物品「{clean_name}」当前数量超过 1'
    return None


def world_unique_instance_update_error(
    *,
    name: str,
    item_type: str,
    description: str,
    quantity: int,
    adventurer_id: int,
    equipment_item_id: int | None = None,
    warehouse_item_id: int | None = None,
) -> str | None:
    """Validate one edited instance against the post-edit world-unique snapshot."""
    clean_name = str(name or '').strip()
    if not clean_name:
        return '物品名称不能为空'
    catalog = AdventurerS1Item.query.filter_by(name=clean_name).first()
    resolved_type = str(item_type or getattr(catalog, 'type', None) or '').strip()
    resolved_description = str(
        description or getattr(catalog, 'description', None) or ''
    ).strip()
    if not resolve_world_unique_flag(
        clean_name,
        item_type=resolved_type,
        desc=resolved_description,
        catalog_flag=(bool(catalog.world_unique) if catalog is not None else None),
    ):
        return None

    occupancy = load_world_unique_occupancy(
        [clean_name],
        exclude_equipment_item_id=equipment_item_id,
        exclude_warehouse_item_id=warehouse_item_id,
    ).get(clean_name, {
        'holders': set(),
        'escrowed': False,
        'quantity_total': 0,
    })
    if occupancy['escrowed']:
        return f'物品【{clean_name}】为全服唯一，当前正在交易托管中'
    holders = set(occupancy['holders'])
    adv_id = int(adventurer_id)
    if companion_allows_duplicate_name_instances(resolved_type):
        other_holders = holders - {adv_id}
        if other_holders:
            return f'物品【{clean_name}】为全服唯一，已被其他冒险者持有'
        return None
    if int(quantity or 0) != 1:
        return f'物品【{clean_name}】为全服唯一，数量必须为 1'
    if int(occupancy.get('quantity_total') or 0) > 0:
        return f'物品【{clean_name}】为全服唯一，当前已有其它实例'
    return None


def _can_grant_world_unique_catalog_item(name: str, adventurer_id: int) -> tuple[bool, str | None]:
    """非探险路径：图鉴 world_unique 物品全服仅允许一名持有者。"""
    clean_name = str(name or '').strip()
    if not clean_name:
        return True, None

    catalog = AdventurerS1Item.query.filter_by(name=clean_name).first()
    if catalog is None:
        return True, None
    if not bool(getattr(catalog, 'world_unique', False)):
        return True, None

    return can_grant_world_unique_name(
        clean_name,
        adventurer_id,
        item_type=catalog.type or '',
    )


def can_grant_world_unique_name(
    name: str,
    adventurer_id: int,
    *,
    item_type: str = '',
    grant_quantity: int = 1,
    transfer_from_adventurer_id: int | None = None,
    transfer_source_item_id: int | None = None,
    transfer_quantity: int | None = None,
    transfer_market_listing_id: int | None = None,
    transfer_market_listing_quantity: int | None = None,
    exclude_secret_auction_lot_id: int | None = None,
    transfer_team_loot_lot_id: int | None = None,
    occupancy_by_name: dict[str, dict] | None = None,
) -> tuple[bool, str | None]:
    """Check holders for a name whose caller already established unique semantics."""
    clean_name = str(name or '').strip()
    if not clean_name:
        return True, None

    excluded_market_listing_id = None
    excluded_market_seller_id = None
    excluded_team_loot_id = None
    if transfer_team_loot_lot_id is not None:
        from db_repository.s1_team_loot_repository import S1TeamLootRepository
        # The team service owns the lot -> adventurers lock order and the
        # enclosing transaction. An arbitrary ID cannot exempt other stock.
        source = S1TeamLootRepository.delivery_source(
            transfer_team_loot_lot_id, clean_name, adventurer_id, grant_quantity,
        )
        if source is None:
            return False, '队伍待分配实物来源或胜出者不匹配'
        excluded_team_loot_id = source.id
    if transfer_market_listing_id is not None:
        escrow_source = (
            AdventurerS1MarketListing.query
            .filter_by(
                id=int(transfer_market_listing_id),
                item_name=clean_name,
                status='active',
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if (
            escrow_source is not None
            and item_quantity_of(escrow_source) == int(transfer_market_listing_quantity or 0)
        ):
            excluded_market_listing_id = int(escrow_source.id)
            if int(escrow_source.seller_adventurer_id) == int(adventurer_id):
                excluded_market_seller_id = int(adventurer_id)

    if occupancy_by_name is not None:
        # Only a locked, transaction-local issuance snapshot may bypass the read.
        if any(value is not None for value in (
            transfer_from_adventurer_id, transfer_source_item_id, transfer_quantity,
            transfer_market_listing_id, transfer_market_listing_quantity,
            exclude_secret_auction_lot_id,
            transfer_team_loot_lot_id,
        )) or clean_name not in occupancy_by_name:
            raise RuntimeError('批量唯一性快照不能用于转移或未锁定物品')
        occupancy = occupancy_by_name[clean_name]
    else:
        occupancy = load_world_unique_occupancy(
            [clean_name],
            exclude_market_listing_id=excluded_market_listing_id,
            exclude_market_seller_adventurer_id=excluded_market_seller_id,
            exclude_secret_auction_lot_id=exclude_secret_auction_lot_id,
            exclude_team_loot_lot_id=excluded_team_loot_id,
        ).get(clean_name, {
            'holders': set(),
            'escrowed': False,
            'escrow_count': 0,
            'instance_count': 0,
            'quantity_total': 0,
        })
    if occupancy['escrowed']:
        return False, f'物品【{clean_name}】为全服唯一，当前正在交易托管中'
    holders = set(occupancy['holders'])
    if transfer_from_adventurer_id is not None:
        sender_id = int(transfer_from_adventurer_id)
        source = (
            AdventurerS1Equipment.query
            .filter_by(
                id=int(transfer_source_item_id or 0),
                adventurer_id=sender_id,
                name=clean_name,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        sender_equipment_count = AdventurerS1Equipment.query.filter_by(
            adventurer_id=sender_id,
            name=clean_name,
        ).count()
        sender_warehouse_count = AdventurerS1WarehouseItem.query.filter_by(
            adventurer_id=sender_id,
            name=clean_name,
        ).count()
        if (
            source is not None
            and sender_equipment_count == 1
            and sender_warehouse_count == 0
            and item_quantity_of(source) == int(transfer_quantity or 0)
        ):
            holders.discard(sender_id)
    adv_id = int(adventurer_id)
    resolved_type = resolve_guard_item_type(clean_name, item_type=item_type)
    returning_own_market_escrow = (
        excluded_market_listing_id is not None
        and excluded_market_seller_id == adv_id
        and int(grant_quantity or 0) == int(transfer_market_listing_quantity or 0)
    )
    if (
        returning_own_market_escrow
        or companion_allows_duplicate_name_instances(resolved_type)
    ):
        other_holders = holders - {adv_id}
        if other_holders:
            return False, f'物品【{clean_name}】为全服唯一，已被冒险者 #{next(iter(other_holders))} 持有'
        return True, None
    if int(grant_quantity or 0) != 1:
        return False, f'物品【{clean_name}】为全服唯一，单次只能发放或转移 1 件'
    if adv_id in holders:
        return False, f'物品【{clean_name}】已在本冒险者持有，不可重复发放'
    if holders:
        return False, f'物品【{clean_name}】为全服唯一，已被冒险者 #{next(iter(holders))} 持有'
    return True, None


def _resolve_guard_item_rarity(
    clean_name: str,
    *,
    item_rarity: str | None,
    catalog_by_name: dict | None,
) -> str:
    rarity = str(item_rarity or '').strip().lower()
    if rarity in {'common', 'rare', 'epic', 'legendary'}:
        return rarity
    catalog = (catalog_by_name or {}).get(clean_name)
    if catalog is not None:
        catalog_rarity = str(getattr(catalog, 'rarity', None) or 'common').strip().lower()
        if catalog_rarity in {'common', 'rare', 'epic', 'legendary'}:
            return catalog_rarity
    return 'common'


def _adventurer_has_self_quantity_merge_stack(
    adventurer_id: int,
    *,
    name: str,
    item_type: str,
    rarity: str,
) -> bool:
    """本人已持有时，是否存在可 quantity 合并的同名堆（name+type+rarity）。"""
    from plugins.adventurer_s1.models import AdventurerS1Equipment, AdventurerS1WarehouseItem
    from plugins.adventurer_s1.s1_item_types import catalog_item_allows_quantity_stack_merge

    clean_name = str(name or '').strip()
    clean_type = str(item_type or '').strip()
    clean_rarity = str(rarity or 'common').strip().lower()
    if not clean_name or not catalog_item_allows_quantity_stack_merge(clean_type):
        return False
    for model in (AdventurerS1WarehouseItem, AdventurerS1Equipment):
        row = (
            model.query
            .filter_by(
                adventurer_id=int(adventurer_id),
                name=clean_name,
                type=clean_type,
                rarity=clean_rarity,
            )
            .order_by(model.id.asc())
            .first()
        )
        if row is not None:
            return True
    return False


def can_grant_item_to_adventurer(
    name: str,
    adventurer_id: int,
    *,
    party_holders_map: dict[str, set[int]] | None = None,
    party_transfers: dict[str, tuple] | None = None,
    catalog_by_name: dict[str, AdventurerS1Item] | None = None,
    holder_custom_desc_flags: dict[tuple[int, str], bool] | None = None,
    item_type: str | None = None,
    item_rarity: str | None = None,
    # 兼容旧参数名
    holders_map: dict[str, set[int]] | None = None,
) -> tuple[bool, str | None]:
    """返回 (允许, 拒绝原因)。探险结算须传 party_holders_map（仅本队）。"""
    clean_name = str(name or '').strip()
    if not clean_name:
        return True, None

    if party_holders_map is None and holders_map is not None:
        party_holders_map = holders_map

    if party_holders_map is None:
        return _can_grant_world_unique_catalog_item(clean_name, adventurer_id)

    party_holders = party_holders_map.get(clean_name, set())
    adv_id = int(adventurer_id)
    resolved_type = resolve_guard_item_type(
        clean_name, catalog_by_name=catalog_by_name, item_type=item_type,
    )
    if adv_id in party_holders:
        transfer = (party_transfers or {}).get(clean_name)
        if transfer and int(transfer[1]) == adv_id:
            return True, None
        if companion_allows_duplicate_name_instances(resolved_type):
            return True, None
        resolved_rarity = _resolve_guard_item_rarity(
            clean_name,
            item_rarity=item_rarity,
            catalog_by_name=catalog_by_name,
        )
        if _adventurer_has_self_quantity_merge_stack(
            adv_id,
            name=clean_name,
            item_type=resolved_type,
            rarity=resolved_rarity,
        ):
            return True, None
        return False, (
            f'物品【{clean_name}】已在本冒险者持有，不可重复发放'
            f'（同场转赠须 consumed+new 成对）'
        )

    other_holders = party_holders - {adv_id}
    if not other_holders:
        return True, None

    transfer = (party_transfers or {}).get(clean_name)
    if transfer:
        sender_id, receiver_id = transfer[:2]
        if int(receiver_id) == adv_id and other_holders == {int(sender_id)}:
            return True, None

    for holder_id in other_holders:
        if is_party_clone_blocked_item(
            clean_name,
            holder_id,
            catalog_by_name=catalog_by_name,
            holder_custom_desc=(holder_custom_desc_flags or {}).get((holder_id, clean_name)),
        ):
            return False, (
                f'物品【{clean_name}】已被本队冒险者 #{holder_id} 持有，'
                f'不可复制给其他成员（转赠须 consumed+new 成对）'
            )
    return True, None


can_grant_world_unique_item = can_grant_item_to_adventurer


def _consumed_equipment_names(update: dict) -> set[str]:
    names: set[str] = set()
    raw = update.get('consumed_equipments') or []
    if not isinstance(raw, list):
        return names
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get('name') or '').strip()
        else:
            continue
        if name:
            names.add(name)
    return names


def _new_equipment_names(update: dict) -> set[str]:
    raw = update.get('new_equipments') or {}
    if not isinstance(raw, dict):
        return set()
    return {str(name).strip() for name in raw.keys() if str(name).strip()}


def _new_equipment_quantity(update: dict, name: str) -> int:
    raw = update.get('new_equipments') or {}
    data = raw.get(name) if isinstance(raw, dict) else None
    return coerce_item_quantity(
        data.get('quantity') if isinstance(data, dict) else 1,
        1,
    )


def _declared_consumed_quantity(update: dict, name: str) -> int | None:
    raw = update.get('consumed_equipments') or []
    if not isinstance(raw, list):
        return None
    for entry in raw:
        if isinstance(entry, str) and entry.strip() == name:
            return None
        if not isinstance(entry, dict) or str(entry.get('name') or '').strip() != name:
            continue
        return coerce_item_quantity(entry.get('quantity'), 1)
    return None


def _build_party_guard_context(
    members,
    item_names: list[str],
    party_holders_map: dict[str, set[int]] | None = None,
    *,
    include_warehouse: bool = True,
):
    party_holders_map = party_holders_map or build_party_item_holders_map(
        members,
        item_names,
        include_warehouse=include_warehouse,
    )
    catalog_by_name = _load_catalog_by_name(item_names)
    pairs: list[tuple[int, str]] = []
    for name in item_names:
        for holder_id in party_holders_map.get(name, set()):
            pairs.append((holder_id, name))
    holder_custom_desc_flags = _load_holder_custom_desc_flags(
        pairs,
        include_warehouse=include_warehouse,
    )
    return party_holders_map, catalog_by_name, holder_custom_desc_flags


def build_party_equipment_transfers(
    member_updates: list[dict],
    *,
    party_holders_map: dict[str, set[int]] | None = None,
    holders_map: dict[str, set[int]] | None = None,
    lock_sources: bool = False,
) -> dict[str, tuple]:
    """识别转出 consumed + 转入 new，并冻结来源 ID、数量及机械字段。"""
    if party_holders_map is None:
        party_holders_map = holders_map or {}

    updates_by_id: dict[int, dict] = {}
    for update in member_updates:
        if not isinstance(update, dict):
            continue
        try:
            adv_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        updates_by_id[adv_id] = update

    candidate_names = {
        name
        for update in updates_by_id.values()
        for name in _new_equipment_names(update)
    }
    participant_ids = sorted(updates_by_id)
    equipment_query = (
        AdventurerS1Equipment.query
        .filter(
            AdventurerS1Equipment.adventurer_id.in_(participant_ids),
            AdventurerS1Equipment.name.in_(candidate_names),
        )
        .order_by(AdventurerS1Equipment.id.asc())
    )
    warehouse_query = (
        AdventurerS1WarehouseItem.query
        .filter(
            AdventurerS1WarehouseItem.adventurer_id.in_(participant_ids),
            AdventurerS1WarehouseItem.name.in_(candidate_names),
        )
        .order_by(AdventurerS1WarehouseItem.id.asc())
    )
    if lock_sources:
        equipment_query = equipment_query.populate_existing().with_for_update()
        warehouse_query = warehouse_query.populate_existing().with_for_update()
    else:
        equipment_query = equipment_query.populate_existing()
        warehouse_query = warehouse_query.populate_existing()
    equipment_rows = equipment_query.all() if participant_ids and candidate_names else []
    warehouse_rows = warehouse_query.all() if participant_ids and candidate_names else []
    equipment_by_owner_name: dict[tuple[int, str], list] = {}
    for row in equipment_rows:
        equipment_by_owner_name.setdefault((int(row.adventurer_id), row.name), []).append(row)
    warehouse_owner_names = {
        (int(row.adventurer_id), row.name)
        for row in warehouse_rows
    }

    transfers: dict[str, tuple] = {}
    for name in candidate_names:
        receivers = [
            adv_id for adv_id, update in updates_by_id.items()
            if name in _new_equipment_names(update)
        ]
        senders = [
            adv_id for adv_id, update in updates_by_id.items()
            if name in _consumed_equipment_names(update)
        ]
        if len(receivers) != 1 or len(senders) != 1:
            continue
        from_id, to_id = senders[0], receivers[0]
        if from_id == to_id:
            continue
        holders = party_holders_map.get(name, set())
        if holders and from_id not in holders:
            continue
        source_rows = equipment_by_owner_name.get((from_id, name), [])
        if len(source_rows) != 1 or (from_id, name) in warehouse_owner_names:
            continue
        source_quantity = item_quantity_of(source_rows[0])
        if _new_equipment_quantity(updates_by_id[to_id], name) != source_quantity:
            continue
        consumed_quantity = _declared_consumed_quantity(updates_by_id[from_id], name)
        if consumed_quantity is not None and consumed_quantity != source_quantity:
            continue
        source = source_rows[0]
        recovery_triggered = any(
            isinstance(trigger, dict)
            and trigger.get('asset_kind') == 'equipment'
            and str(trigger.get('asset_id') or '').strip().isascii()
            and str(trigger.get('asset_id') or '').strip().isdecimal()
            and int(str(trigger.get('asset_id')).strip()) == int(source.id)
            for trigger in updates_by_id[from_id].get('recovery_triggers') or []
        )
        transfers[name] = (
            from_id,
            to_id,
            int(source.id),
            source_quantity,
            {
                'type': source.type or '',
                'rarity': source.rarity or 'common',
                'quantity': source_quantity,
                'uses_left': source.uses_left,
                'evolution_stage': source.evolution_stage,
                'recovery_minutes': max(0, int(source.recovery_minutes or 0)),
                'recovery_started_at': source.recovery_started_at,
                'recovery_ends_at': source.recovery_ends_at,
                'recovery_triggered': recovery_triggered,
            },
        )
    return transfers


def enforce_duplicate_equipment_grants(
    member_updates: list[dict],
    members,
    *,
    allow_reviewed_self_gains: bool = False,
) -> list[str]:
    """从 member_updates 剥离不应发放的 new_equipments，返回被剥离的物品名列表。"""
    if not member_updates:
        return []

    all_names: list[str] = []
    for update in member_updates:
        if not isinstance(update, dict):
            continue
        raw = update.get('new_equipments') or {}
        if isinstance(raw, dict):
            all_names.extend(str(name).strip() for name in raw.keys() if str(name).strip())

    party_holders_map, catalog_by_name, holder_custom_desc_flags = _build_party_guard_context(
        members, all_names,
    )
    party_transfers = build_party_equipment_transfers(
        member_updates,
        party_holders_map=party_holders_map,
    )
    stripped: list[str] = []

    for update in member_updates:
        if not isinstance(update, dict):
            continue
        try:
            adv_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        raw = update.get('new_equipments')
        if not isinstance(raw, dict) or not raw:
            continue

        kept: dict[str, Any] = {}
        for name, data in raw.items():
            clean_name = str(name or '').strip()
            if not clean_name:
                continue
            allowed, block_reason = can_grant_item_to_adventurer(
                clean_name,
                adv_id,
                party_holders_map=party_holders_map,
                party_transfers=party_transfers,
                catalog_by_name=catalog_by_name,
                holder_custom_desc_flags=holder_custom_desc_flags,
                item_type=(data.get('type') if isinstance(data, dict) else None),
                item_rarity=(data.get('rarity') if isinstance(data, dict) else None),
            )
            if allowed:
                kept[clean_name] = data
                continue
            party_holders = party_holders_map.get(clean_name, set())
            catalog = catalog_by_name.get(clean_name)
            if (
                allow_reviewed_self_gains
                and adv_id in party_holders
                and not bool(getattr(catalog, 'world_unique', False))
            ):
                reviewed_data = dict(data) if isinstance(data, dict) else {}
                reviewed_data['_same_name_gain_reviewed'] = True
                kept[clean_name] = reviewed_data
                continue
            stripped.append(clean_name)
            current_app.logger.warning(
                'S1 拒绝队内重复发放物品 adv=%s name=%s: %s',
                adv_id, clean_name, block_reason,
            )

        if kept:
            update['new_equipments'] = kept
        else:
            update.pop('new_equipments', None)

    return stripped


def _collect_expedition_item_grant_conflicts(
    member_updates: list[dict],
    members,
    *,
    lock_catalog: bool = False,
) -> list[dict]:
    """返回结构化的新物品可发放冲突；不修改 AI 候选。"""
    grants_by_name: dict[str, list[dict[str, int | str]]] = {}
    for update in member_updates or []:
        if not isinstance(update, dict):
            continue
        try:
            adventurer_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        raw = update.get('new_equipments') or {}
        if not isinstance(raw, dict):
            continue
        for name, data in raw.items():
            clean_name = str(name or '').strip()
            if not clean_name:
                continue
            item_data = data if isinstance(data, dict) else {}
            grants_by_name.setdefault(clean_name, []).append({
                'adventurer_id': adventurer_id,
                'quantity': coerce_item_quantity(item_data.get('quantity'), 1),
                'item_type': str(item_data.get('type') or '').strip(),
                'desc': str(item_data.get('desc') or '').strip(),
            })
    if not grants_by_name:
        return []

    item_names = list(grants_by_name)
    if lock_catalog:
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            CatalogBusyError,
            lock_catalog_names,
        )
        if not lock_catalog_names(sorted(item_names), shared=False, wait=False):
            raise CatalogBusyError()

    catalog_by_name = _load_catalog_by_name(item_names)
    unique_names = []
    for name, grants in grants_by_name.items():
        catalog = catalog_by_name.get(name)
        is_world_unique = (
            bool(catalog.world_unique)
            if catalog is not None
            else any(
                infer_world_unique(name, grant['item_type'], grant['desc'])
                for grant in grants
            )
        )
        if is_world_unique:
            unique_names.append(name)
    if not unique_names:
        return []

    occupancy_by_name = load_world_unique_occupancy(unique_names)
    party_holders_map = build_party_item_holders_map(members, unique_names)
    party_transfers = build_party_equipment_transfers(
        member_updates,
        party_holders_map=party_holders_map,
    )
    conflicts: list[dict] = []

    def add_conflict(name: str, code: str, message: str) -> None:
        conflicts.append({
            'name': name,
            'code': code,
            'message': message,
            'adventurer_ids': sorted({
                int(grant['adventurer_id'])
                for grant in grants_by_name.get(name, [])
            }),
        })

    for name in unique_names:
        grants = grants_by_name[name]
        catalog = catalog_by_name.get(name)
        resolved_type = normalize_item_type(
            (getattr(catalog, 'type', '') or '')
            or str(grants[0]['item_type'] or '')
        )
        occupancy = occupancy_by_name.get(name, {
            'holders': set(),
            'escrowed': False,
            'quantity_total': 0,
        })
        holders = set(occupancy.get('holders') or set())
        quantity_total = int(occupancy.get('quantity_total') or 0)
        receiver_ids = {int(grant['adventurer_id']) for grant in grants}
        transfer = party_transfers.get(name)
        if transfer:
            sender_id, receiver_id, _, source_quantity = transfer[:4]
            holders.discard(int(sender_id))
            quantity_total = max(0, quantity_total - int(source_quantity or 0))
            if receiver_ids != {int(receiver_id)}:
                add_conflict(
                    name,
                    'transfer_receiver_mismatch',
                    f'物品【{name}】的同场转赠接收者与新增归属不一致',
                )
                continue

        if occupancy.get('escrowed'):
            add_conflict(
                name,
                'escrowed',
                f'物品【{name}】当前处于交易托管，不能作为本轮新收益发放',
            )
            continue

        if companion_allows_duplicate_name_instances(resolved_type):
            if holders - receiver_ids:
                add_conflict(
                    name,
                    'held_by_other',
                    f'全服唯一物品【{name}】已被其他冒险者持有',
                )
                continue
            if len(receiver_ids) != 1:
                add_conflict(
                    name,
                    'multiple_receivers',
                    f'全服唯一物品【{name}】不能在同一结算发给多个成员',
                )
            continue

        if quantity_total > 0:
            add_conflict(
                name,
                'existing_instance',
                f'全服唯一物品【{name}】当前已有实例',
            )
            continue
        if len(grants) != 1 or int(grants[0]['quantity']) != 1:
            add_conflict(
                name,
                'invalid_quantity',
                f'全服唯一物品【{name}】本轮只能发放一个实例',
            )

    return conflicts


def validate_expedition_item_grant_feasibility(
    member_updates: list[dict],
    members,
) -> list[str]:
    """在候选发布前批量验证本轮新物品能否通过最终统一发放链。

    这里只消费稳定名称、结构化 type、图鉴唯一标记和当前持有快照，不判断
    剧情语义。最终写回仍由事务内目录锁复核。
    """
    return [
        f"settlement_item_grant_feasibility: {conflict['message']}"
        for conflict in _collect_expedition_item_grant_conflicts(
            member_updates,
            members,
        )
    ]


def reconcile_expedition_item_grants_at_settlement(
    member_updates: list[dict],
    members,
) -> tuple[list[dict], list[str]]:
    """在最终事务内锁定名称并撤销无法到账的非破坏性旧候选。

    仅处理不伴随同名资产扣减的新增物。若同名还声明了 consumed，撤销新增会
    造成净资产损失，因此保持失败关闭并交由上游重新审核。
    """
    conflicts = _collect_expedition_item_grant_conflicts(
        member_updates,
        members,
        lock_catalog=True,
    )
    if not conflicts:
        return [], []

    updates_by_id: dict[int, dict] = {}
    consumed_names: set[str] = set()
    for update in member_updates or []:
        if not isinstance(update, dict):
            continue
        try:
            updates_by_id[int(update.get('id'))] = update
        except (TypeError, ValueError):
            continue
        consumed_names.update(_consumed_equipment_names(update))

    reconciled: list[dict] = []
    errors: list[str] = []
    for conflict in conflicts:
        name = str(conflict.get('name') or '').strip()
        if not name:
            continue
        if name in consumed_names or conflict.get('code') == 'transfer_receiver_mismatch':
            errors.append(
                f"settlement_item_grant_feasibility: {conflict['message']}"
            )
            continue
        for adventurer_id in conflict.get('adventurer_ids') or []:
            update = updates_by_id.get(int(adventurer_id))
            raw = (update or {}).get('new_equipments')
            if not isinstance(raw, dict) or name not in raw:
                continue
            data = raw.pop(name)
            item_data = data if isinstance(data, dict) else {}
            reconciled.append({
                'adventurer_id': int(adventurer_id),
                'name': name,
                'quantity': coerce_item_quantity(item_data.get('quantity'), 1),
                'reason': 'world_unique_conflict',
                'message': (
                    f'全服唯一物品「{name}」当前已无法发放，本次未能到账'
                ),
                'conflict_code': str(conflict.get('code') or ''),
            })

    return reconciled, errors


enforce_world_unique_equipment_grants = enforce_duplicate_equipment_grants


def build_item_duplicate_prompt_block(
    members,
    limit: int = 50,
    *,
    name_alias: dict[int, str] | None = None,
) -> str:
    """仅向 AI 注入本队随身携带、禁止 clone 给他人的物品。"""
    # 剧情可见性以出发随身栏为准；仓库只保留给最终防重与结算校验。
    party_holders_map = build_party_item_holders_map(
        members,
        include_warehouse=False,
    )
    if not party_holders_map:
        return ''

    item_names = list(party_holders_map.keys())
    catalog_by_name, holder_custom_desc_flags = _build_party_guard_context(
        members,
        item_names,
        party_holders_map,
        include_warehouse=False,
    )[1:]

    member_by_id = {}
    for member in members or []:
        try:
            member_by_id[int(member.id)] = member
        except (TypeError, ValueError):
            continue

    lines: list[str] = []
    for name in sorted(party_holders_map.keys()):
        holders = party_holders_map[name]
        blocked_holders = [
            holder_id for holder_id in holders
            if is_party_clone_blocked_item(
                name,
                holder_id,
                catalog_by_name=catalog_by_name,
                holder_custom_desc=holder_custom_desc_flags.get((holder_id, name)),
            )
        ]
        if not blocked_holders:
            continue
        for holder_id in sorted(blocked_holders):
            label = (name_alias or {}).get(holder_id)
            who = f'#{holder_id} {label}' if label else f'#{holder_id}'
            lines.append(
                f"- {name}（本队 {who} 持有，禁止复制给其他成员；转赠须 consumed+new 成对）"
            )
        if len(lines) >= limit:
            break

    if not lines:
        return ''
    return (
        '【本队不可复制的已持有物品（禁止 clone 给其他成员；同场转赠须 consumed+new 成对；'
        '图鉴可量产同名物可在副本正常再掉落）】\n'
        + '\n'.join(lines)
    )


build_world_unique_items_prompt_block = build_item_duplicate_prompt_block
