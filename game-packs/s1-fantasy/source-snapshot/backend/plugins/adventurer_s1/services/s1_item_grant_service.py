"""Grant / consume S1 items to/from an adventurer.

统一发放链路（系统 → 玩家）：
  grant_catalog_item / grant_raw_item → _grant_item_to_adventurer
    → 堆叠合并 + 随身/仓库路由 + ItemLog（玩家动态）+ 图鉴注册

业务代码禁止直接 new Equipment/WarehouseItem 发奖（创建冒险者初始装备等 bootstrap 除外）。
玩家主动搬运（随身↔仓库、整理）仍走 s1_item_instance_service.add_or_merge_stack。
"""
from dataclasses import dataclass
from typing import Optional, Tuple
from flask import current_app
from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Equipment, AdventurerS1Item, AdventurerS1ItemLog, AdventurerS1WarehouseItem
from plugins.adventurer_s1.seed import default_catalog_item_uses_left, is_herbalist_item
from plugins.adventurer_s1.s1_item_types import catalog_item_allows_quantity_stack_merge, coerce_evolution_stage, companion_allows_duplicate_name_instances
from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_ATTRIBUTE_UNSET, find_mergeable_stack, lock_warehouse_aggregate, normalize_stack_custom_desc
from plugins.adventurer_s1.services.s1_item_quantity_service import coerce_item_quantity, item_quantity_of, split_stack_suffix
from plugins.adventurer_s1.services.s1_item_catalog_lock_service import CatalogBusyError, lock_catalog_names
from plugins.adventurer_s1.services.s1_visible_name_policy import visible_name_pollution_reason
_ITEM_LOG_DESCRIPTION_MAX_LENGTH = int(AdventurerS1ItemLog.__table__.c.description.type.length)

def _normalize_item_log_description(value) -> str:
    """Keep timeline text within its DB column; full appeal evidence is stored elsewhere."""
    text = str(value or '').strip()
    if len(text) <= _ITEM_LOG_DESCRIPTION_MAX_LENGTH:
        return text
    return f'{text[:_ITEM_LOG_DESCRIPTION_MAX_LENGTH - 1]}…'

def _default_uses_left(item_name: str, item_type: str, catalog_use_condition: dict | None=None) -> Optional[int]:
    from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_default_uses_left, catalog_has_default_uses_left
    if catalog_has_default_uses_left(catalog_use_condition):
        return catalog_default_uses_left(catalog_use_condition)
    return default_catalog_item_uses_left(item_name, item_type or '')

def apply_missing_raw_item_uses_left_default(item_name: str, data: dict, *, catalog_use_condition: dict | None=None) -> dict:
    """复制原始发放数据，并只为真正缺失的 uses_left 应用默认值。"""
    resolved = dict(data or {})
    if 'uses_left' not in resolved:
        resolved['uses_left'] = _default_uses_left(item_name, str(resolved.get('type') or ''), catalog_use_condition)
    return resolved

def adventurer_holds_item_name(adventurer_id: int, name: str) -> bool:
    """冒险者随身或仓库是否已有同名物品行。"""
    clean_name = str(name or '').strip()
    if not clean_name:
        return False
    return clean_name in adventurer_held_item_names(adventurer_id, [clean_name])

def adventurer_carries_item_name(adventurer_id: int, name: str) -> bool:
    """冒险者随身是否携带同名物品（不含仓库）。"""
    clean_name = str(name or '').strip()
    if not clean_name:
        return False
    return clean_name in adventurer_carried_item_names(adventurer_id, [clean_name])

def adventurer_carried_item_names(adventurer_id: int, names: list) -> set:
    """批量查询冒险者随身已携带的物品名（1 次 SQL，不含仓库）。"""
    clean_names = list({str(n).strip() for n in names or [] if str(n).strip()})
    if not clean_names:
        return set()
    adv_id = int(adventurer_id)
    eq_rows = AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.name).filter(AdventurerS1Equipment.adventurer_id == adv_id, AdventurerS1Equipment.name.in_(clean_names)).all()
    return {row[0] for row in eq_rows}

def adventurer_held_item_names(adventurer_id: int, names: list) -> set:
    """批量查询冒险者随身/仓库已持有的物品名（2 次 SQL）。"""
    clean_names = list({str(n).strip() for n in names or [] if str(n).strip()})
    if not clean_names:
        return set()
    adv_id = int(adventurer_id)
    held: set = set()
    eq_rows = AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.name).filter(AdventurerS1Equipment.adventurer_id == adv_id, AdventurerS1Equipment.name.in_(clean_names)).all()
    held.update((row[0] for row in eq_rows))
    wh_rows = AdventurerS1WarehouseItem.query.with_entities(AdventurerS1WarehouseItem.name).filter(AdventurerS1WarehouseItem.adventurer_id == adv_id, AdventurerS1WarehouseItem.name.in_(clean_names)).all()
    held.update((row[0] for row in wh_rows))
    return held
_special_dungeons_ticket_cache = None

def _special_dungeons_ticket_cache_manager():
    global _special_dungeons_ticket_cache
    if _special_dungeons_ticket_cache is None:
        from utils.cache_manager import CacheManager
        _special_dungeons_ticket_cache = CacheManager(ttl_seconds=60, max_size=2000)
    return _special_dungeons_ticket_cache

def invalidate_special_dungeons_ticket_cache(adventurer_id: Optional[int]=None) -> None:
    """特殊副本持票状态缓存失效（物品增减后调用）。"""
    cache = _special_dungeons_ticket_cache_manager()
    if adventurer_id is None:
        cache.clear()
    else:
        cache.delete(int(adventurer_id))

def resolve_item_storage_destination(adv: AdventurerS1, item_name: str, *, item_type: str | None=None, batch_context: 'WarehouseGrantBatchContext | None'=None) -> Tuple[Optional[str], Optional[str]]:
    """决定新物品入随身还是仓库。返回 ('equipment'|'warehouse', None) 或 (None, 拒绝原因)。

    随身满、同名已携带或草药店类随身数量达到上限时，自动溢出到仓库（仓库满则拒绝）。
    companion 允许多实例同名，不占「同名不可再入随身」限制。
    """
    equip_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
    herb_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
    wh_cap = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
    equipment_rows = batch_context.equipment_rows if batch_context is not None else AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.name).filter_by(adventurer_id=adv.id).all()
    equipment_names = [row.name for row in equipment_rows]
    eq_count = len(equipment_names)
    has_existing_eq = item_name in equipment_names
    if not item_type:
        catalog = AdventurerS1Item.query.filter_by(name=item_name).first()
        item_type = catalog.type if catalog else ''
    allow_dup_name = companion_allows_duplicate_name_instances(item_type)
    is_herb = is_herbalist_item(item_name)
    herb_count = sum((1 for name in equipment_names if is_herbalist_item(name)))
    can_equip = eq_count < equip_limit and (allow_dup_name or not has_existing_eq) and (not (is_herb and herb_count >= herb_limit))
    if can_equip:
        return ('equipment', None)
    wh_count = batch_context.warehouse_count if batch_context is not None else AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adv.id).count()
    if wh_count >= wh_cap:
        return (None, f'仓库已满（{wh_cap} 件），请先清理仓库')
    return ('warehouse', None)

def warehouse_capacity_error(adventurer_id: int) -> Optional[str]:
    """返回冒险者仓库达到硬上限时的统一玩家提示。"""
    wh_cap = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
    wh_count = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adventurer_id).count()
    if wh_count >= wh_cap:
        return f'仓库已满（{wh_cap} 件），请先清理仓库'
    return None

def is_warehouse_capacity_error(adventurer_id: int, error: Optional[str]) -> bool:
    """机械识别统一发放返回的仓库容量错误，不靠关键词推断。"""
    capacity_error = warehouse_capacity_error(adventurer_id)
    return capacity_error is not None and error == capacity_error

def _find_quantity_stack_family(adventurer_id: int, *, name: str, item_type: str, for_update: bool=False):
    """查找同名同类型族；实例属性不同只能共存，不能据此合堆。"""
    for model in (AdventurerS1WarehouseItem, AdventurerS1Equipment):
        query = model.query.filter_by(adventurer_id=adventurer_id, name=name, type=item_type).order_by(model.id.asc())
        if for_update:
            query = query.populate_existing().with_for_update()
        row = query.first()
        if row is not None:
            return (model, row)
    return (None, None)

def _find_existing_mergeable_stack(adventurer_id: int, *, name: str, item_type: str, rarity: str, custom_desc, uses_left, evolution_stage: int=0, recovery_minutes: int=0, recovery_started_at=None, recovery_ends_at=None, damage_stage=ITEM_ATTRIBUTE_UNSET, damage_note=ITEM_ATTRIBUTE_UNSET, for_update: bool=False):
    """在随身/仓库查找可合并的同名堆叠，优先仓库。"""
    for model in (AdventurerS1WarehouseItem, AdventurerS1Equipment):
        row = find_mergeable_stack(model, adventurer_id, name=name, item_type=item_type, rarity=rarity, custom_desc=custom_desc, uses_left=uses_left, evolution_stage=evolution_stage, recovery_minutes=recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at, damage_stage=damage_stage, damage_note=damage_note, for_update=for_update)
        if row is not None:
            return (model, row)
    return (None, None)

def _invalidate_grant_side_effects(adv: AdventurerS1) -> None:
    """发放/合并后：持票缓存、玩家「我的动态」时间线缓存失效。"""
    invalidate_special_dungeons_ticket_cache(adv.id)
    owner_id = getattr(adv, 'owner_id', None)
    if owner_id:
        from plugins.adventurer_s1.services.adventurer_s1_service import invalidate_my_activity_cache
        invalidate_my_activity_cache(int(owner_id))

def _grant_stack_key(name: str, item_type: str, rarity: str, custom_desc, uses_left, evolution_stage, recovery_minutes=0, recovery_started_at=None, recovery_ends_at=None) -> tuple:
    from plugins.adventurer_s1.services.s1_asset_recovery_service import normalize_recovery_minutes, recovery_stack_state
    active_started_at, active_ends_at = recovery_stack_state(recovery_started_at, recovery_ends_at)
    return (str(name or '').strip(), str(item_type or ''), str(rarity or 'common'), normalize_stack_custom_desc(custom_desc), uses_left, coerce_evolution_stage(evolution_stage), normalize_recovery_minutes(recovery_minutes), active_started_at, active_ends_at)

@dataclass
class WarehouseGrantBatchContext:
    """Locked, transaction-local authority snapshot for repeated warehouse grants."""
    adventurer_id: int
    catalog_by_name: dict[str, AdventurerS1Item]
    warehouse_rows: list[AdventurerS1WarehouseItem]
    equipment_rows: list[AdventurerS1Equipment]
    world_unique_occupancy: dict[str, dict] | None = None

    def __post_init__(self) -> None:
        self.warehouse_count = len(self.warehouse_rows)
        self.held_names = {row.name for row in [*self.warehouse_rows, *self.equipment_rows]}
        self.equipment_exact = {}
        self.warehouse_exact = {}
        self.exact = {}
        self.stack_families = set()
        for row in self.warehouse_rows:
            self._index_row(row, warehouse=True)
        for row in self.equipment_rows:
            self._index_row(row, warehouse=False)

    def _index_row(self, row, *, warehouse: bool) -> None:
        self.stack_families.add((row.name, row.type))
        if bool(row.is_player_locked):
            return
        key = _grant_stack_key(row.name, row.type, row.rarity, row.custom_desc, row.uses_left, row.evolution_stage, row.recovery_minutes, row.recovery_started_at, row.recovery_ends_at)
        if warehouse:
            self.warehouse_exact.setdefault(key, row)
            self.exact[key] = self.warehouse_exact[key]
        else:
            self.equipment_exact.setdefault(key, row)
        self.exact.setdefault(key, row)

    def find_existing(self, *, name: str, item_type: str, rarity: str, custom_desc, uses_left, evolution_stage, recovery_minutes=0, recovery_started_at=None, recovery_ends_at=None, warehouse_only: bool, equipment_only: bool=False):
        key = _grant_stack_key(name, item_type, rarity, custom_desc, uses_left, evolution_stage, recovery_minutes, recovery_started_at, recovery_ends_at)
        exact_map = self.warehouse_exact if warehouse_only else self.equipment_exact if equipment_only else self.exact
        return exact_map.get(key)

    def has_stack_family(self, *, name: str, item_type: str) -> bool:
        return (name, item_type) in self.stack_families

    def add_warehouse_row(self, row: AdventurerS1WarehouseItem) -> None:
        self.warehouse_rows.append(row)
        self.warehouse_count += 1
        self.held_names.add(row.name)
        self._index_row(row, warehouse=True)

    def add_equipment_row(self, row: AdventurerS1Equipment) -> None:
        self.equipment_rows.append(row)
        self.held_names.add(row.name)
        self._index_row(row, warehouse=False)

    def record_grant(self, name: str, quantity: int) -> None:
        occupancy = (self.world_unique_occupancy or {}).get(name)
        if occupancy is not None:
            occupancy['holders'].add(self.adventurer_id)
            occupancy['quantity_total'] += quantity

    def record_consumption(self, row, quantity: int, *, deleted: bool) -> None:
        if deleted:
            rows = self.warehouse_rows if isinstance(row, AdventurerS1WarehouseItem) else self.equipment_rows
            rows.remove(row)
            self.__post_init__()
        occupancy = (self.world_unique_occupancy or {}).get(row.name)
        if occupancy is not None:
            occupancy['quantity_total'] -= quantity
            if row.name not in self.held_names:
                occupancy['holders'].discard(self.adventurer_id)

def prepare_warehouse_grant_batch(adv: AdventurerS1, item_names, *, preload_world_unique: bool=False) -> WarehouseGrantBatchContext:
    """Acquire all catalog-name locks and preload one buyer's grant state once."""
    names = sorted({str(name or '').strip() for name in item_names if str(name or '').strip()})
    lock_warehouse_aggregate(adv.id)
    if names and (not lock_catalog_names(names, shared=False, wait=False)):
        raise CatalogBusyError()
    catalog_rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).order_by(AdventurerS1Item.id.asc()).populate_existing().all() if names else []
    warehouse_rows = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adv.id).order_by(AdventurerS1WarehouseItem.id.asc()).populate_existing().with_for_update().all()
    equipment_rows = AdventurerS1Equipment.query.filter_by(adventurer_id=adv.id).order_by(AdventurerS1Equipment.id.asc()).populate_existing().with_for_update().all()
    occupancy = None
    if preload_world_unique:
        from plugins.adventurer_s1.services.s1_item_world_unique_service import load_world_unique_occupancy, resolve_world_unique_flag
        unique_names = [row.name for row in catalog_rows if resolve_world_unique_flag(row.name, item_type=row.type or '', desc=row.description or '', catalog_flag=bool(row.world_unique))]
        occupancy = load_world_unique_occupancy(unique_names)
    return WarehouseGrantBatchContext(adventurer_id=int(adv.id), catalog_by_name={row.name: row for row in catalog_rows}, warehouse_rows=warehouse_rows, equipment_rows=equipment_rows, world_unique_occupancy=occupancy)

def warehouse_grant_blocked_by_capacity(adventurer_id: int, *, name: str, item_type: str, rarity: str, custom_desc=None, uses_left=None, recovery_minutes: int=0, recovery_started_at=None, recovery_ends_at=None) -> bool:
    """仓库已满且无法合并进已有堆时为 True（市场成交/退回前校验）。"""
    if warehouse_capacity_error(adventurer_id) is None:
        return False
    clean_name = str(name or '').strip()
    item_type = str(item_type or '')
    rarity = str(rarity or 'common')
    if companion_allows_duplicate_name_instances(item_type):
        return True
    if catalog_item_allows_quantity_stack_merge(item_type):
        _, existing = _find_existing_mergeable_stack(adventurer_id, name=clean_name, item_type=item_type, rarity=rarity, custom_desc=custom_desc, uses_left=uses_left, recovery_minutes=recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at)
        return existing is None
    wh_row = find_mergeable_stack(AdventurerS1WarehouseItem, adventurer_id, name=clean_name, item_type=item_type, rarity=rarity, custom_desc=custom_desc, uses_left=uses_left, recovery_minutes=recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at)
    if wh_row is not None:
        return False
    return True

def _write_item_gain_log(adv: AdventurerS1, *, name: str, item_type: str, rarity: str, change_type: str, quantity: int, source_type: str, description: str, related_id: Optional[str]=None, related_type: Optional[str]=None) -> None:
    db_manager.add(AdventurerS1ItemLog(adventurer_id=adv.id, item_name=name, item_type=item_type, item_rarity=rarity, change_type=change_type, quantity=quantity, source_type=source_type, description=_normalize_item_log_description(description), related_id=related_id, related_type=related_type), flush=False)

def _merge_quantity_into_existing_stack(adv: AdventurerS1, row, *, quantity: int, name: str, item_type: str, rarity: str, source_type: str, description: str, silent: bool=False, related_id: Optional[str]=None, related_type: Optional[str]=None) -> Tuple[str, None]:
    qty = coerce_item_quantity(quantity, 1)
    row.quantity = item_quantity_of(row) + qty
    if not silent:
        in_warehouse = isinstance(row, AdventurerS1WarehouseItem)
        _write_item_gain_log(adv, name=name, item_type=item_type, rarity=rarity, change_type='overflow' if in_warehouse else 'gain', quantity=qty, source_type=source_type, description=description or f'获得「{name}」', related_id=related_id, related_type=related_type)
    _invalidate_grant_side_effects(adv)
    destination = 'warehouse' if isinstance(row, AdventurerS1WarehouseItem) else 'equipment'
    return (destination, None)

def _grant_item_to_adventurer(adv: AdventurerS1, name: str, item_type: str, rarity: str, *, quantity: int=1, uses_left: Optional[int]=None, custom_desc: Optional[str]=None, source_type: str, description: str='', register_catalog: bool=True, catalog_desc: Optional[str]=None, force_destination: Optional[str]=None, equipment_first: bool=False, allow_equipment_overflow: bool=True, allow_warehouse_capacity_overflow: bool=False, silent: bool=False, evolution_stage: Optional[int]=None, recovery_minutes: int=0, recovery_started_at=None, recovery_ends_at=None, damage_stage=ITEM_ATTRIBUTE_UNSET, damage_note=ITEM_ATTRIBUTE_UNSET, related_id: Optional[str]=None, related_type: Optional[str]=None, allow_same_name_stack_merge: bool=False, world_unique_transfer_from_adventurer_id: Optional[int]=None, world_unique_transfer_source_item_id: Optional[int]=None, world_unique_transfer_quantity: Optional[int]=None, world_unique_escrow_listing_id: Optional[int]=None, world_unique_escrow_quantity: Optional[int]=None, world_unique_escrow_lot_id: Optional[int]=None, world_unique_team_loot_lot_id: Optional[int]=None, warehouse_batch_context: Optional[WarehouseGrantBatchContext]=None, granted_item_out: Optional[dict]=None) -> Tuple[Optional[str], Optional[str]]:
    """统一发放物品。内部判断随身/仓库 + 写日志 + 注册图鉴。

    force_destination: 优先 'equipment' 或 'warehouse'；'equipment' 默认受随身上限约束，满则自动溢出到仓库。
    equipment_first: 合堆时先只查随身栏；随身满后才回落仓库。
    allow_equipment_overflow: False 时强制随身位置满则失败，供必须原路恢复的补偿使用。
    allow_warehouse_capacity_overflow: 补偿性返还可越过仓库容量上限，避免丢失原物。
    silent: True 时不写 ItemLog（GM 隐身操作）。
    granted_item_out: 可选事务内回执，成功时写入 destination 与实际新增/合堆行。
    Returns: (destination: 'equipment'|'warehouse', error)
    """
    if granted_item_out is not None:
        granted_item_out.clear()
    clean_name = str(name or '').strip()
    if not clean_name:
        return (None, '物品名称为空')
    resolved_recovery_minutes = recovery_minutes if type(recovery_minutes) is int and recovery_minutes >= 0 else 0
    resolved_evolution_stage = coerce_evolution_stage(evolution_stage) if evolution_stage is not None else 0
    from plugins.adventurer_s1.services.s1_item_world_unique_service import can_grant_world_unique_name, resolve_world_unique_flag
    if warehouse_batch_context is not None:
        if int(warehouse_batch_context.adventurer_id) != int(adv.id):
            raise RuntimeError('批量发放上下文与目标冒险者不一致')
        catalog = warehouse_batch_context.catalog_by_name.get(clean_name)
        catalog_exists = catalog is not None
        projected_world_unique = resolve_world_unique_flag(clean_name, item_type=(catalog.type if catalog is not None else item_type) or '', desc=(catalog.description if catalog is not None else catalog_desc or custom_desc or '') or '', catalog_flag=bool(catalog.world_unique) if catalog is not None else None)
    else:
        lock_warehouse_aggregate(adv.id)
        catalog_projection = AdventurerS1Item.query.with_entities(AdventurerS1Item.id, AdventurerS1Item.world_unique, AdventurerS1Item.type, AdventurerS1Item.description).filter_by(name=clean_name).first()
        catalog_exists = catalog_projection is not None
        projected_world_unique = resolve_world_unique_flag(clean_name, item_type=(catalog_projection.type if catalog_projection is not None else item_type) or '', desc=(catalog_projection.description if catalog_projection is not None else catalog_desc or custom_desc or '') or '', catalog_flag=bool(catalog_projection.world_unique) if catalog_projection is not None else None)
        if not lock_catalog_names([clean_name], shared=catalog_exists and (not projected_world_unique), wait=False):
            raise CatalogBusyError()
        catalog = AdventurerS1Item.query.filter_by(name=clean_name).populate_existing().first()
        if catalog_exists and catalog is None:
            raise CatalogBusyError()
    if uses_left is ITEM_ATTRIBUTE_UNSET:
        catalog_use_condition = catalog.use_condition if catalog is not None and isinstance(catalog.use_condition, dict) else None
        uses_left = _default_uses_left(clean_name, item_type, catalog_use_condition)
    resolved_world_unique = projected_world_unique
    if catalog is not None:
        resolved_world_unique = resolve_world_unique_flag(clean_name, item_type=catalog.type or '', desc=catalog.description or '', catalog_flag=bool(catalog.world_unique))
        if resolved_world_unique and (not projected_world_unique):
            raise CatalogBusyError()
    if resolved_world_unique:
        allowed, block_reason = can_grant_world_unique_name(clean_name, adv.id, item_type=(catalog.type if catalog is not None else item_type) or '', grant_quantity=quantity, transfer_from_adventurer_id=world_unique_transfer_from_adventurer_id, transfer_source_item_id=world_unique_transfer_source_item_id, transfer_quantity=world_unique_transfer_quantity, transfer_market_listing_id=world_unique_escrow_listing_id, transfer_market_listing_quantity=world_unique_escrow_quantity, exclude_secret_auction_lot_id=world_unique_escrow_lot_id, transfer_team_loot_lot_id=world_unique_team_loot_lot_id, occupancy_by_name=warehouse_batch_context.world_unique_occupancy if warehouse_batch_context is not None else None)
        if not allowed:
            return (None, block_reason)

    def find_preloaded_existing(*, warehouse_only: bool=False, equipment_only: bool=False):
        if warehouse_batch_context is not None:
            return warehouse_batch_context.find_existing(name=clean_name, item_type=item_type, rarity=rarity, custom_desc=custom_desc, uses_left=uses_left, evolution_stage=resolved_evolution_stage, recovery_minutes=resolved_recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at, warehouse_only=warehouse_only, equipment_only=equipment_only)
        if warehouse_only:
            return find_mergeable_stack(AdventurerS1WarehouseItem, adv.id, name=clean_name, item_type=item_type, rarity=rarity, custom_desc=custom_desc, uses_left=uses_left, evolution_stage=resolved_evolution_stage, recovery_minutes=resolved_recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at, damage_stage=damage_stage, damage_note=damage_note, for_update=True)
        if equipment_only:
            return find_mergeable_stack(AdventurerS1Equipment, adv.id, name=clean_name, item_type=item_type, rarity=rarity, custom_desc=custom_desc, uses_left=uses_left, evolution_stage=resolved_evolution_stage, recovery_minutes=resolved_recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at, damage_stage=damage_stage, damage_note=damage_note, for_update=True)
        _, existing = _find_existing_mergeable_stack(adv.id, name=clean_name, item_type=item_type, rarity=rarity, custom_desc=custom_desc, uses_left=uses_left, evolution_stage=resolved_evolution_stage, recovery_minutes=resolved_recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at, damage_stage=damage_stage, damage_note=damage_note, for_update=True)
        return existing

    def has_same_quantity_stack_family() -> bool:
        if warehouse_batch_context is not None:
            return warehouse_batch_context.has_stack_family(name=clean_name, item_type=item_type)
        _, existing = _find_quantity_stack_family(adv.id, name=clean_name, item_type=item_type, for_update=True)
        return existing is not None

    def merge_existing(row):
        destination, merge_err = _merge_quantity_into_existing_stack(adv, row, quantity=quantity, name=clean_name, item_type=item_type, rarity=rarity, source_type=source_type, description=description, silent=silent, related_id=related_id, related_type=related_type)
        if merge_err is None and warehouse_batch_context is not None:
            warehouse_batch_context.record_grant(clean_name, quantity)
        if merge_err is None and granted_item_out is not None:
            granted_item_out.clear()
            granted_item_out.update({'destination': destination, 'item': row})
        return (destination, merge_err)
    same_quantity_stack_family_exists = False
    if not companion_allows_duplicate_name_instances(item_type):
        if allow_same_name_stack_merge:
            existing = find_preloaded_existing(equipment_only=equipment_first and force_destination == 'equipment')
            if existing is not None:
                return merge_existing(existing)
        if catalog_item_allows_quantity_stack_merge(item_type):
            existing = find_preloaded_existing(equipment_only=equipment_first and force_destination == 'equipment')
            if existing is not None:
                return merge_existing(existing)
            same_quantity_stack_family_exists = has_same_quantity_stack_family()
        if force_destination == 'warehouse':
            wh_row = find_preloaded_existing(warehouse_only=True)
            if wh_row is not None:
                return merge_existing(wh_row)
        elif force_destination != 'equipment':
            wh_row = find_preloaded_existing(warehouse_only=True)
            if wh_row is not None:
                return merge_existing(wh_row)
        if force_destination != 'warehouse' and (not allow_same_name_stack_merge) and (not same_quantity_stack_family_exists) and (not (equipment_first and force_destination == 'equipment' and catalog_item_allows_quantity_stack_merge(item_type))) and (clean_name in warehouse_batch_context.held_names if warehouse_batch_context is not None else adventurer_holds_item_name(adv.id, clean_name)):
            return (None, f'物品【{clean_name}】已持有，不可重复发放')
    if force_destination:
        destination = force_destination
        if destination == 'equipment':
            equip_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
            eq_count = len(warehouse_batch_context.equipment_rows) if warehouse_batch_context is not None else AdventurerS1Equipment.query.filter_by(adventurer_id=adv.id).count()
            if eq_count >= equip_limit:
                if not allow_equipment_overflow:
                    return (None, f'随身已达上限 {equip_limit} 件')
                destination = 'warehouse'
                if equipment_first and (not companion_allows_duplicate_name_instances(item_type)):
                    wh_row = find_preloaded_existing(warehouse_only=True)
                    if wh_row is not None:
                        return merge_existing(wh_row)
    else:
        destination, dest_err = resolve_item_storage_destination(adv, name, item_type=item_type, batch_context=warehouse_batch_context)
        if dest_err:
            return (None, dest_err)
    if destination == 'warehouse':
        if warehouse_batch_context is not None:
            wh_cap = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
            wh_err = f'仓库已满（{wh_cap} 件），请先清理仓库' if warehouse_batch_context.warehouse_count >= wh_cap else None
        else:
            wh_err = warehouse_capacity_error(adv.id)
        if wh_err and (not allow_warehouse_capacity_overflow):
            return (None, wh_err)
    common = dict(adventurer_id=adv.id, name=clean_name, type=item_type, rarity=rarity, custom_desc=normalize_stack_custom_desc(custom_desc), quantity=quantity, uses_left=uses_left, recovery_minutes=resolved_recovery_minutes, recovery_started_at=recovery_started_at, recovery_ends_at=recovery_ends_at)
    if companion_allows_duplicate_name_instances(item_type) or evolution_stage is not None:
        common['evolution_stage'] = resolved_evolution_stage
    if destination == 'warehouse':
        warehouse_row = AdventurerS1WarehouseItem(**common)
        db_manager.add(warehouse_row, flush=False)
        granted_row = warehouse_row
        if warehouse_batch_context is not None:
            warehouse_batch_context.add_warehouse_row(warehouse_row)
    else:
        if damage_stage is not ITEM_ATTRIBUTE_UNSET:
            common['damage_stage'] = damage_stage
        if damage_note is not ITEM_ATTRIBUTE_UNSET:
            common['damage_note'] = damage_note
        equipment_row = AdventurerS1Equipment(**common)
        db_manager.add(equipment_row, flush=False)
        granted_row = equipment_row
        if warehouse_batch_context is not None:
            warehouse_batch_context.add_equipment_row(equipment_row)
    if not silent:
        change_type = 'overflow' if destination == 'warehouse' else 'gain'
        _write_item_gain_log(adv, name=name, item_type=item_type, rarity=rarity, change_type=change_type, quantity=quantity, source_type=source_type, description=description or f'获得「{name}」', related_id=related_id, related_type=related_type)
    if register_catalog and catalog is None:
        catalog = AdventurerS1Item(name=clean_name, type=item_type, description=catalog_desc or custom_desc or '', rarity=rarity, is_starter=False, recovery_minutes=resolved_recovery_minutes, world_unique=resolved_world_unique)
        db_manager.add(catalog, flush=False)
        if warehouse_batch_context is not None:
            warehouse_batch_context.catalog_by_name[clean_name] = catalog
    if warehouse_batch_context is not None:
        warehouse_batch_context.record_grant(clean_name, quantity)
    _invalidate_grant_side_effects(adv)
    if granted_item_out is not None:
        granted_item_out.clear()
        granted_item_out.update({'destination': destination, 'item': granted_row})
    return (destination, None)

def grant_raw_item(adv: AdventurerS1, name: str, data: dict, *, source_type: str, description: str='', force_destination: Optional[str]=None, equipment_first: bool=False, allow_equipment_overflow: bool=True, silent: bool=False, register_catalog: bool=True, related_id: Optional[str]=None, related_type: Optional[str]=None, allow_same_name_stack_merge: bool=False, world_unique_transfer_from_adventurer_id: Optional[int]=None, world_unique_transfer_source_item_id: Optional[int]=None, world_unique_transfer_quantity: Optional[int]=None, world_unique_escrow_listing_id: Optional[int]=None, world_unique_escrow_quantity: Optional[int]=None, world_unique_escrow_lot_id: Optional[int]=None, world_unique_team_loot_lot_id: Optional[int]=None, warehouse_batch_context: Optional[WarehouseGrantBatchContext]=None, granted_item_out: Optional[dict]=None) -> Tuple[Optional[str], Optional[str]]:
    """发放原始物品数据（AI 探险 / 铁匠铺 / GM / 市场换入 等场景）。

    data 字段：type, rarity, quantity, uses_left, desc, evolution_stage,
    recovery_minutes；可信恢复快照还可携带 recovery_started_at /
    recovery_ends_at / damage_stage / damage_note。
    uses_left 缺失时按图鉴声明或类型默认值初始化；显式 None 保持无次数语义。
    register_catalog=False 时跳过图鉴注册（市场/图鉴已有物）。
    Returns: (destination, error)
    """
    if granted_item_out is not None:
        granted_item_out.clear()
    name_pollution_reason = visible_name_pollution_reason(name)
    if name_pollution_reason:
        return (None, f'物品名称不合法：{name_pollution_reason}')
    evolution_stage = data.get('evolution_stage') if 'evolution_stage' in data else None
    if evolution_stage is not None:
        evolution_stage = coerce_evolution_stage(evolution_stage)
    return _grant_item_to_adventurer(adv, name, item_type=data.get('type', ''), rarity=data.get('rarity', 'common'), quantity=coerce_item_quantity(data.get('quantity'), 1), uses_left=data.get('uses_left') if 'uses_left' in data else ITEM_ATTRIBUTE_UNSET, custom_desc=data.get('desc') or None, source_type=source_type, description=description or f'获得「{name}」', register_catalog=register_catalog, catalog_desc=data.get('desc'), force_destination=force_destination, equipment_first=equipment_first, allow_equipment_overflow=allow_equipment_overflow, silent=silent, evolution_stage=evolution_stage, recovery_minutes=data.get('recovery_minutes', 0), recovery_started_at=data.get('recovery_started_at'), recovery_ends_at=data.get('recovery_ends_at'), damage_stage=data.get('damage_stage') if 'damage_stage' in data else ITEM_ATTRIBUTE_UNSET, damage_note=data.get('damage_note') if 'damage_note' in data else ITEM_ATTRIBUTE_UNSET, related_id=related_id, related_type=related_type, allow_same_name_stack_merge=allow_same_name_stack_merge, world_unique_transfer_from_adventurer_id=world_unique_transfer_from_adventurer_id, world_unique_transfer_source_item_id=world_unique_transfer_source_item_id, world_unique_transfer_quantity=world_unique_transfer_quantity, world_unique_escrow_listing_id=world_unique_escrow_listing_id, world_unique_escrow_quantity=world_unique_escrow_quantity, world_unique_escrow_lot_id=world_unique_escrow_lot_id, world_unique_team_loot_lot_id=world_unique_team_loot_lot_id, warehouse_batch_context=warehouse_batch_context, granted_item_out=granted_item_out)

def grant_catalog_item(adv: AdventurerS1, item_name: str, *, quantity: int=1, uses_left: Optional[int]=None, prefer_warehouse: bool=False, prefer_equipment: bool=False, allow_warehouse_capacity_overflow: bool=False, source_type: str='grant', description: str='', related_id: Optional[str]=None, related_type: Optional[str]=None, warehouse_batch_context: Optional[WarehouseGrantBatchContext]=None, granted_item_out: Optional[dict]=None) -> Tuple[list[str], Optional[str]]:
    """从图鉴发放物品。可优先入仓库，或优先随身并在满时回落仓库。"""
    if prefer_warehouse and prefer_equipment:
        return ([], '入仓库和随身优先不能同时指定')
    name, qty = split_stack_suffix(item_name, quantity)
    if not name:
        return ([], '物品名称为空')
    qty = coerce_item_quantity(qty, 1)
    if allow_warehouse_capacity_overflow and (not prefer_equipment or qty != 1 or source_type != 'special_ticket_refund' or (related_type != 'expedition_log') or (not str(related_id or '').isdigit())):
        return ([], '仅特殊副本门票原路退还可越过仓库容量上限')
    if warehouse_batch_context is not None:
        if int(warehouse_batch_context.adventurer_id) != int(adv.id):
            raise RuntimeError('批量发放上下文与目标冒险者不一致')
        catalog_projection = warehouse_batch_context.catalog_by_name.get(name)
    else:
        catalog_projection = AdventurerS1Item.query.with_entities(AdventurerS1Item.id, AdventurerS1Item.world_unique, AdventurerS1Item.type, AdventurerS1Item.description).filter_by(name=name).first()
    if not catalog_projection:
        return ([], f'物品不存在: {name}')
    from plugins.adventurer_s1.services.s1_item_world_unique_service import can_grant_item_to_adventurer, can_grant_world_unique_name, resolve_world_unique_flag
    projected_world_unique = resolve_world_unique_flag(name, item_type=catalog_projection.type or '', desc=catalog_projection.description or '', catalog_flag=bool(catalog_projection.world_unique))
    if warehouse_batch_context is not None:
        catalog = catalog_projection
    else:
        if not lock_catalog_names([name], shared=not projected_world_unique, wait=False):
            raise CatalogBusyError()
        catalog = AdventurerS1Item.query.filter_by(id=catalog_projection.id, name=name).populate_existing().first()
    if not catalog:
        raise CatalogBusyError()
    if allow_warehouse_capacity_overflow:
        use_condition = catalog.use_condition if isinstance(catalog.use_condition, dict) else {}
        if catalog.type != 'ticket' or use_condition.get('item_kind') != 'special_dungeon_ticket':
            return ([], '仅特殊副本门票可越过仓库容量上限')
    resolved_world_unique = resolve_world_unique_flag(name, item_type=catalog.type or '', desc=catalog.description or '', catalog_flag=bool(catalog.world_unique))
    if resolved_world_unique and (not projected_world_unique):
        raise CatalogBusyError()
    if resolved_world_unique:
        allowed, block_reason = can_grant_world_unique_name(name, adv.id, item_type=catalog.type or '', grant_quantity=qty, occupancy_by_name=warehouse_batch_context.world_unique_occupancy if warehouse_batch_context is not None else None)
    elif warehouse_batch_context is None:
        allowed, block_reason = can_grant_item_to_adventurer(name, adv.id)
    else:
        allowed, block_reason = (True, None)
    if not allowed:
        return ([], block_reason)
    resolved_uses_left = uses_left if uses_left is not None else _default_uses_left(catalog.name, catalog.type or '', catalog.use_condition)
    log_desc = description or f'获得「{catalog.name}」'
    if prefer_warehouse and '已存入仓库' not in log_desc:
        log_desc = f'{log_desc}（已存入仓库）'
    _, dest_err = _grant_item_to_adventurer(adv, catalog.name, item_type=catalog.type or '', rarity=catalog.rarity or 'common', quantity=qty, uses_left=resolved_uses_left, recovery_minutes=max(0, int(catalog.recovery_minutes or 0)), source_type=source_type, description=log_desc, register_catalog=False, force_destination='warehouse' if prefer_warehouse else 'equipment' if prefer_equipment else None, equipment_first=prefer_equipment, allow_warehouse_capacity_overflow=allow_warehouse_capacity_overflow, related_id=related_id, related_type=related_type, warehouse_batch_context=warehouse_batch_context, granted_item_out=granted_item_out)
    if dest_err:
        return ([], dest_err)
    return ([catalog.name] * qty, None)
