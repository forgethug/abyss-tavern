"""深渊酒馆 S1 业务逻辑：冒险者管理。

创建规则（数值/池均走 config，见 AdventurerS1Plugin.default_config）：
- 玩家选 1 个初始技能 + 2 件初始装备，提交预约排队
- 管理员设优先级（high/normal/low）后执行抽取，被抽中者创建冒险者
- 无职业（character_class 留空，后续探险中随机获得）
- 四项属性各在 [ATTR_MIN, ATTR_MAX] 随机，HP/最大HP 在 [HP_MIN, HP_MAX] 随机，等级 1
- 每个玩家最多拥有 MAX_PER_PLAYER 个（默认 1）
- 随身装备上限 ADVENTURER_S1_EQUIPMENT_LIMIT（默认 5），超出时商店购买改入仓库（草药店类除外）
"""
from runtime_core.time import utc_now as _portable_utc_now
from plugins.adventurer_s1.services.s1_player_error_service import S1PlayerVisibleError, player_database_result, player_operation_failure
import logging
import re
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List
from flask import current_app
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.exc import OperationalError, PendingRollbackError
from sqlalchemy.orm import joinedload, noload
from db_repository import db_manager
from db_repository.decorators import monitor_performance
from utils.db_operation_tracker import track_db_phase
from db_repository.s1_activity_repository import recent_activity_query
from db_repository.s1_role_admission_lock import release_s1_role_admission_lock as _release_bed_draw_lock, try_acquire_s1_role_admission_lock as _try_acquire_bed_draw_lock
from db_repository.s1_realm_repository import S1RealmRepository
from extensions import db
from utils.pagination import build_paginated_response
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Trait, AdventurerS1Skill, AdventurerS1Equipment, AdventurerS1SkillTemplate, AdventurerS1Item, AdventurerS1WarehouseItem, AdventurerS1ItemLog, AdventurerS1MaterialItem, AdventurerS1MaterialLog, AdventurerS1Reservation, AdventurerS1DormRoom, AdventurerS1Follow, AdventurerS1BedSwapRequest, AdventurerS1BuildingTemplate, AdventurerS1BuildingInstance, AdventurerS1BuildingHelper, AdventurerS1Currency, AdventurerS1ShopItem, AdventurerS1WorldActor, AdventurerS1WorldStoryThread
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of
from plugins.adventurer_s1.services.s1_limited_shop_service import LIMITED_SHOP_HANDLER, limited_shop_catalog, limited_shop_rejection
from plugins.adventurer_s1.services.adventurer_s1_building_upgrade_config import building_max_level, building_upgrade_stage_config
from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import AdventurerS1StateTransitionCoordinator
from plugins.adventurer_s1.services.s1_realm_service import REALM_1, REALM_2, S1RealmActiveSlotError, S1RealmAssignmentError, S1RealmService
from utils.cache_manager import CacheManager
logger = logging.getLogger(__name__)

def split_history_story_entries(text: str | None) -> list[str]:
    """把冒险史长文本按空行分段；每段对应一次（或一组）追加记录。"""
    if not text or not str(text).strip():
        return []
    return [part.strip() for part in str(text).split('\n\n') if part.strip()]
_building_completion_locks_guard = threading.Lock()
_building_completion_locks: dict[int, threading.Lock] = {}
_BUILDINGS_LIST_CACHE_TTL_SECONDS = 90
_buildings_response_cache = CacheManager(ttl_seconds=_BUILDINGS_LIST_CACHE_TTL_SECONDS, max_size=2000)
_buildings_shared_cache = CacheManager(ttl_seconds=_BUILDINGS_LIST_CACHE_TTL_SECONDS, max_size=8)
_buildings_shared_build_lock = threading.Lock()
_dormitory_cache = CacheManager(ttl_seconds=60, max_size=500)
_my_activity_cache = CacheManager(ttl_seconds=60, max_size=500)
_special_dungeons_list_cache = CacheManager(ttl_seconds=3600, max_size=1)
_ADV_DETAIL_CACHE_TTL_SECONDS = 45
_adventurer_detail_cache = CacheManager(ttl_seconds=_ADV_DETAIL_CACHE_TTL_SECONDS, max_size=2000)

def invalidate_adventurer_detail_cache(adventurer_id: Optional[int]=None) -> None:
    """冒险者详情 GET 缓存失效；写操作后调用。"""
    if adventurer_id is None:
        _adventurer_detail_cache.clear()
    else:
        _adventurer_detail_cache.delete_prefix(f'detail:{adventurer_id}:')

def invalidate_dormitory_cache() -> None:
    """宿舍区列表缓存失效（换床/捐献/升级后调用）。"""
    _dormitory_cache.clear()
    from plugins.adventurer_s1.services.adventurer_s1_home_service import invalidate_s1_init_cache
    invalidate_s1_init_cache()

def invalidate_dormitory_user_cache(user_id: int) -> None:
    """关注关系变化后，仅失效该玩家的宿舍排序分页缓存。"""
    _dormitory_cache.delete_prefix(f'{user_id}:')

def invalidate_my_activity_cache(user_id: Optional[int]=None) -> None:
    """我的动态时间线缓存失效。"""
    if user_id is None:
        _my_activity_cache.clear()
    else:
        _my_activity_cache.delete_prefix(f'{int(user_id)}:')

def invalidate_buildings_list_cache() -> None:
    """建筑列表 shared 缓存失效：状态变更或 scheduler tick 后调用。"""
    _buildings_response_cache.clear()
    _buildings_shared_cache.clear()
    from plugins.adventurer_s1.services.adventurer_s1_home_service import invalidate_s1_init_cache
    invalidate_s1_init_cache()
_NAME_PATTERN = re.compile('^[一-鿿a-zA-Z]+$')
_BUILDING_CODES_HIDDEN_FROM_PLAYER_LIST = frozenset({'tavern_hall'})
_DEFAULT_NAME_MAX_LENGTH = 7
_RESERVATION_PRIORITY_HIGH = 'high'
_RESERVATION_PRIORITY_ELEVATED = 'elevated'
_RESERVATION_PRIORITY_NORMAL = 'normal'
_RESERVATION_PRIORITY_LOW = 'low'
_RESERVATION_PRIORITIES = (_RESERVATION_PRIORITY_HIGH, _RESERVATION_PRIORITY_ELEVATED, _RESERVATION_PRIORITY_NORMAL, _RESERVATION_PRIORITY_LOW)
_DONATION_PURPOSE_BUILD = 'build'
_DONATION_PURPOSE_UPGRADE = 'upgrade'
_CONTRIBUTION_SRC_DONATION = 'donate_contrib'
_CONTRIBUTION_SRC_HELP = 'help_contrib'
_BUILDING_HELP_CONTRIBUTION_SOURCES = frozenset({_CONTRIBUTION_SRC_HELP, f'{_CONTRIBUTION_SRC_HELP}_{_DONATION_PURPOSE_BUILD}', f'{_CONTRIBUTION_SRC_HELP}_{_DONATION_PURPOSE_UPGRADE}', 'building_help_contribution', 'building_help_contribution_build', 'building_help_contribution_upgrade'})
_DORM_CAPACITY_TEMPLATE_CODE = {1: 'dormitory_single', 2: 'dormitory_double', 4: 'dormitory_quad'}

class AdventurerS1Service:
    FREE_STATUSES = AdventurerS1.DISPATCHABLE_STATUSES
    BUILDING_USAGE_EARLY_EXIT_HANDLERS = frozenset({'hospital', 'clinic', 'sanatorium'})
    INVENTORY_EDITABLE_STATUSES = ('resting', 'idle', 'in_party')

    @staticmethod
    def _is_lock_timeout_error(exc) -> bool:
        """识别 PostgreSQL lock_timeout 和 deadlock；列表类接口遇到它应降级而不是 500。"""
        current = exc
        for _ in range(8):
            if current is None:
                break
            orig = getattr(current, 'orig', None)
            pgcode = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
            if pgcode in ('55P03', '40P01'):
                return True
            text = str(current).lower()
            if 'lock timeout' in text or 'locknotavailable' in text or 'canceling statement due to lock timeout' in text or ('deadlock detected' in text):
                return True
            current = getattr(current, 'original_error', None) or getattr(current, '__cause__', None) or getattr(current, '__context__', None)
        return False

    @staticmethod
    def _rollback_building_tick_session() -> None:
        """tick 失败或提交失败后清理 session，避免读路径继续访问 poisoned ORM。"""
        try:
            db_manager.rollback()
        except Exception:
            pass
        try:
            db.session.expire_all()
        except Exception:
            pass

    @staticmethod
    def _tick_buildings_for_list(instances: list, helpers_by_inst: dict, *, stale_helper_adv_ids_by_inst: Optional[dict[int, set[int]]]=None) -> bool:
        """批量推进建造/升级进度（scheduler 或单建筑详情页调用）。锁冲突时跳过本轮。"""
        dorm_built_any = False
        db_changed = False
        commit_ok = True
        treasure_roll_stamina: dict[int, int] = {}
        dormitory_effects: dict = {}
        try:
            try:
                with db.session.no_autoflush:
                    for inst in sorted(instances, key=lambda row: row.id):
                        if db.session.new or db.session.dirty or db.session.deleted:
                            db.session.flush()
                        if inst.status in ('building', 'upgrading'):
                            db_changed = True
                            if AdventurerS1Service._tick_building_progress(inst, helpers=helpers_by_inst.get(inst.id, []), commit=False, treasure_roll_stamina=treasure_roll_stamina, dormitory_effects=dormitory_effects):
                                dorm_built_any = True
                        elif inst.status == 'built':
                            prefetched_adv_ids = None if stale_helper_adv_ids_by_inst is None else stale_helper_adv_ids_by_inst.get(inst.id, set())
                            if prefetched_adv_ids is None or prefetched_adv_ids:
                                db_changed = True
                            if AdventurerS1Service._sweep_stale_construction_occupants(inst, open_helper_adv_ids=prefetched_adv_ids, auto_commit=False):
                                db_changed = True
            except (OperationalError, PendingRollbackError) as e:
                commit_ok = False
                if isinstance(e, PendingRollbackError) or AdventurerS1Service._is_lock_timeout_error(e):
                    logger.info('建筑列表 tick 遇到行锁/死锁，已跳过本轮结算: %s', e)
                    return False
                raise
            except Exception as e:
                commit_ok = False
                if AdventurerS1Service._is_lock_timeout_error(e):
                    logger.info('建筑列表 tick 遇到行锁/死锁，已跳过本轮结算: %s', e)
                    return False
                raise
            if db_changed:
                commit_ok, err = db_manager.commit()
                if not commit_ok:
                    if AdventurerS1Service._is_lock_timeout_error(err):
                        logger.info('建筑列表 tick 提交遇到行锁/死锁，已跳过本轮结算: %s', err)
                    else:
                        logger.warning('建筑列表 tick 提交失败: %s', err)
                    return False
                if dormitory_effects.get('dirty'):
                    invalidate_dormitory_cache()
            if treasure_roll_stamina:
                from plugins.adventurer_s1.services.adventurer_s1_treasure_map_drop_service import AdventurerS1TreasureMapDropService
                AdventurerS1TreasureMapDropService.try_roll_building_stamina(treasure_roll_stamina)
            return dorm_built_any
        finally:
            if db_changed and (not commit_ok):
                AdventurerS1Service._rollback_building_tick_session()

    @staticmethod
    def tick_all_building_progress_job() -> dict:
        """Scheduler：批量推进全服建造中/升级中建筑，并清扫已建成实例上的残留占用。

        原先挂在 get_buildings / 宿舍状态等读接口上的全服 tick 已剥离至此，
        避免玩家刷新列表触发大量写库。
        """
        from sqlalchemy.orm import joinedload
        instances = AdventurerS1BuildingInstance.query.options(joinedload(AdventurerS1BuildingInstance.template), noload(AdventurerS1BuildingInstance.helpers)).filter(AdventurerS1BuildingInstance.disabled.is_(False), AdventurerS1BuildingInstance.status.in_(('building', 'upgrading', 'built'))).all()
        if not instances:
            return {'instances': 0, 'building_active': 0, 'dorm_built': False}
        active_ids = [i.id for i in instances if i.status in ('building', 'upgrading')]
        built_ids = [i.id for i in instances if i.status == 'built']
        helpers_by_inst: dict = {}
        if active_ids:
            for helper in AdventurerS1.query.filter(AdventurerS1.building_instance_id.in_(active_ids), AdventurerS1.status == 'building').all():
                helpers_by_inst.setdefault(helper.building_instance_id, []).append(helper)
        stale_helper_adv_ids_by_inst: dict[int, set[int]] = {}
        if built_ids:
            open_helper_rows = AdventurerS1BuildingHelper.query.with_entities(AdventurerS1BuildingHelper.building_instance_id, AdventurerS1BuildingHelper.adventurer_id).filter(AdventurerS1BuildingHelper.building_instance_id.in_(built_ids), AdventurerS1BuildingHelper.left_at.is_(None)).all()
            for building_instance_id, adventurer_id in open_helper_rows:
                stale_helper_adv_ids_by_inst.setdefault(int(building_instance_id), set()).add(int(adventurer_id))
        dorm_built_any = AdventurerS1Service._tick_buildings_for_list(instances, helpers_by_inst, stale_helper_adv_ids_by_inst=stale_helper_adv_ids_by_inst)
        if dorm_built_any:
            AdventurerS1Service._maybe_auto_draw_after_dormitory_built()
        ensure_stats = AdventurerS1Service.ensure_dorm_room_upgrade_instances_job()
        ensured_count = ensure_stats.get('ensured', 0)
        if active_ids or dorm_built_any or ensured_count:
            invalidate_buildings_list_cache()
        return {'instances': len(instances), 'building_active': len(active_ids), 'dorm_built': dorm_built_any, 'dorm_upgrade_instances_ensured': ensured_count}

    @staticmethod
    def _building_completion_lock(instance_id: int) -> threading.Lock:
        """同一建筑实例完工流程的进程内互斥锁（防并发 tick 重复建房）。"""
        with _building_completion_locks_guard:
            lock = _building_completion_locks.get(instance_id)
            if lock is None:
                lock = threading.Lock()
                _building_completion_locks[instance_id] = lock
            return lock

    @staticmethod
    def build_adventurer_building_info_map(adventurers: list, *, session=None) -> dict:
        """批量构建冒险者当前建筑状态，避免列表页逐行懒加载 building_instance/template。"""
        result = {adv.id: {'status': None, 'handler_key': None} for adv in adventurers}
        inst_ids = {adv.building_instance_id for adv in adventurers if adv.building_instance_id}
        if not inst_ids:
            return result
        rows = (session if session is not None else db.session).query(AdventurerS1BuildingInstance.id, AdventurerS1BuildingInstance.status, AdventurerS1BuildingTemplate.handler_key).outerjoin(AdventurerS1BuildingTemplate, AdventurerS1BuildingTemplate.id == AdventurerS1BuildingInstance.template_id).filter(AdventurerS1BuildingInstance.id.in_(inst_ids)).all()
        by_instance_id = {row.id: {'status': row.status, 'handler_key': row.handler_key} for row in rows}
        for adv in adventurers:
            if adv.building_instance_id:
                result[adv.id] = by_instance_id.get(adv.building_instance_id, {'status': None, 'handler_key': None})
        return result

    @staticmethod
    def resolve_active_adventurer_id(user_id: int) -> Tuple[Optional[int], Optional[str]]:
        """返回 owner 名下首个非死的玩家自建冒险者 ID。

        MAX_PER_PLAYER 默认 1，UI 不再让玩家选冒险者；上层接口在 body 没传
        adventurer_id 时调用此 helper 自动解析。多冒险者场景按 ID 升序取第一个。
        """
        adv_id_row = db_manager.query(AdventurerS1.id).filter(AdventurerS1.owner_id == user_id, AdventurerS1.is_player_created == True, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).order_by(AdventurerS1.id).first()
        if not adv_id_row:
            return (None, '你没有可用的冒险者')
        return (int(adv_id_row[0]), None)

    @staticmethod
    def count_tavern_active_owned(user_id: int) -> int:
        """统计玩家名下仍在酒馆编制内的自建冒险者数（不含 dead/archived）。"""
        return AdventurerS1.query.filter(AdventurerS1.owner_id == user_id, AdventurerS1.is_player_created == True, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).count()

    @staticmethod
    def playable_status_error(status: str) -> Optional[str]:
        if status == 'archived':
            return '该冒险者已封存清退，无法再参与酒馆活动'
        if status == 'dead':
            return '该冒险者已死亡'
        return None

    @staticmethod
    def exploring_action_error(status: str) -> Optional[str]:
        if status == 'exploring':
            return '探险中无法进行此操作'
        return None

    @staticmethod
    def _purpose_from_building_status(status: str) -> str:
        return _DONATION_PURPOSE_UPGRADE if status == 'upgrading' else _DONATION_PURPOSE_BUILD

    @staticmethod
    def _helper_record_purpose(helper: 'AdventurerS1BuildingHelper') -> str:
        return getattr(helper, 'purpose', None) or _DONATION_PURPOSE_BUILD

    @staticmethod
    def _contribution_source_type(base: str, purpose: str) -> str:
        out = f'{base}_{purpose}'
        if len(out) > 30:
            raise ValueError(f'currency source_type 超过 30 字符: {out!r} ({len(out)})')
        return out

    @staticmethod
    def _phase_total_seconds(inst: 'AdventurerS1BuildingInstance', purpose: str) -> float:
        if purpose == _DONATION_PURPOSE_UPGRADE:
            current_level = int(inst.upgrade_level or 1)
            config_level = current_level if inst.status == 'upgrading' else max(1, current_level - 1)
            root_cfg = inst.template.upgrade_config if inst.template else None
            upgrade_cfg = building_upgrade_stage_config(root_cfg, config_level)
            upgrade_hours = int(upgrade_cfg.get('upgrade_hours', 4))
            return float(upgrade_hours * 3600)
        return float(inst.template.build_hours * 3600 if inst.template else 3600)

    @staticmethod
    def handle_death(adv: 'AdventurerS1', now: Optional[datetime]=None) -> None:
        """冒险者死亡时的统一清理：释放占用资源，并没收死亡角色名下残留资产。

        调用方需自行设置 adv.status = 'dead' 和 adv.hp = 0（本方法不改状态，只做资源释放）。
        """
        now = now or _portable_utc_now()
        if adv.dorm_room_id is not None:
            adv.dorm_room_id = None
            adv.bed_index = None
        if adv.building_instance_id is not None:
            from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
            S1SkillSchoolService.release_usage_slot(adv.id, now=now)
            helper = AdventurerS1BuildingHelper.query.filter_by(building_instance_id=adv.building_instance_id, adventurer_id=adv.id, left_at=None).first()
            if helper:
                helper.left_at = now
            adv.building_instance_id = None
            adv.building_assigned_at = None
            adv.building_usage_extra = None
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        AdventurerS1TavernSeatService.clear_seats_for_adventurer(adv.id, locked_adv=adv)
        from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import AdventurerS1TeaHouseSeatService
        AdventurerS1TeaHouseSeatService.clear_seats_for_adventurer(adv.id)
        from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import AdventurerS1HotSpringSeatService
        AdventurerS1HotSpringSeatService.clear_seats_for_adventurer(adv.id)
        from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
        S1SecretAuctionService.cancel_held_bids_for_adventurer(adv.id)
        AdventurerS1Service.confiscate_dead_adventurer_assets(adv.id, now=now)
        AdventurerS1BedSwapRequest.query.filter(AdventurerS1BedSwapRequest.status == 'pending', db.or_(AdventurerS1BedSwapRequest.requester_adv_id == adv.id, AdventurerS1BedSwapRequest.target_adv_id == adv.id)).update({'status': 'cancelled'}, synchronize_session=False)
        from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
        S1CommissionService.release_for_adventurer(adv.id)
        if adv.is_player_created and adv.owner_id is not None:
            if S1RealmService.current_runtime_realm() == REALM_1:
                S1RealmService.release_active_slot(adv.owner_id, realm_code=REALM_1, adventurer_id=adv.id)
            else:
                client = current_app.extensions.get('s1_active_slot_client')
                marker = getattr(client, 'mark_active_slot_reconcile_needed', None)
                if callable(marker):
                    marker()

    @staticmethod
    def confiscate_dead_adventurer_assets(adventurer_id: int, now: Optional[datetime]=None) -> dict:
        """没收死亡冒险者残留资产；边界必须是 adventurer_id，不能按 owner_id 清理。"""
        now = now or _portable_utc_now()
        from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
        active_market_listings = S1MarketService.confiscate_dead_adventurer_residuals(adventurer_id)
        equipment_items = AdventurerS1Equipment.query.filter_by(adventurer_id=adventurer_id).all()
        equipment_logs = []
        for item in equipment_items:
            equipment_logs.append(AdventurerS1ItemLog(adventurer_id=adventurer_id, item_name=item.name, item_type=item.type, item_rarity=item.rarity, change_type='consume', quantity=item_quantity_of(item), source_type='death_confiscate', description='冒险者死亡，系统没收随身物品', created_at=now))
        if equipment_logs:
            db.session.add_all(equipment_logs)
        equipment_count = AdventurerS1Equipment.query.filter_by(adventurer_id=adventurer_id).delete(synchronize_session=False)
        warehouse_items = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adventurer_id).all()
        item_logs = []
        for item in warehouse_items:
            item_logs.append(AdventurerS1ItemLog(adventurer_id=adventurer_id, item_name=item.name, item_type=item.type, item_rarity=item.rarity, change_type='consume', quantity=item_quantity_of(item), source_type='death_confiscate', description='冒险者死亡，系统没收仓库物品', created_at=now))
        if item_logs:
            db.session.add_all(item_logs)
        warehouse_count = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adventurer_id).delete(synchronize_session=False)
        material_rows = AdventurerS1MaterialItem.query.filter_by(adventurer_id=adventurer_id).all()
        material_logs = []
        for row in material_rows:
            quantity = int(row.quantity or 0)
            if quantity <= 0:
                continue
            material_logs.append(AdventurerS1MaterialLog(adventurer_id=adventurer_id, name=row.name, change_amount=-quantity, balance_after=0, source_type='death_confiscate', description='冒险者死亡，系统没收材料', created_at=now))
        if material_logs:
            db.session.add_all(material_logs)
        material_count = AdventurerS1MaterialItem.query.filter_by(adventurer_id=adventurer_id).delete(synchronize_session=False)
        return {'market_listings_confiscated': int(active_market_listings or 0), 'equipment_items_confiscated': int(equipment_count or 0), 'warehouse_items_confiscated': int(warehouse_count or 0), 'material_rows_confiscated': int(material_count or 0)}

    @staticmethod
    def backfill_dead_adventurer_confiscations(batch_size: int=500) -> int:
        """幂等回填历史死亡冒险者残留资产；只处理仍有残留的 dead 角色。"""
        from plugins.adventurer_s1.models import AdventurerS1MarketListing, AdventurerS1MarketOffer, AdventurerS1MarketBuyOrder, AdventurerS1MarketBuyOffer
        residual_ids = set((row[0] for row in AdventurerS1Equipment.query.join(AdventurerS1, AdventurerS1.id == AdventurerS1Equipment.adventurer_id).with_entities(AdventurerS1Equipment.adventurer_id).filter(AdventurerS1.status == 'dead').distinct().limit(batch_size).all()))
        residual_ids.update((row[0] for row in AdventurerS1WarehouseItem.query.join(AdventurerS1, AdventurerS1.id == AdventurerS1WarehouseItem.adventurer_id).with_entities(AdventurerS1WarehouseItem.adventurer_id).filter(AdventurerS1.status == 'dead').distinct().limit(batch_size).all()))
        residual_ids.update((row[0] for row in AdventurerS1MaterialItem.query.join(AdventurerS1, AdventurerS1.id == AdventurerS1MaterialItem.adventurer_id).with_entities(AdventurerS1MaterialItem.adventurer_id).filter(AdventurerS1.status == 'dead').distinct().limit(batch_size).all()))
        residual_ids.update((row[0] for row in AdventurerS1MarketListing.query.join(AdventurerS1, AdventurerS1.id == AdventurerS1MarketListing.seller_adventurer_id).with_entities(AdventurerS1MarketListing.seller_adventurer_id).filter(AdventurerS1.status == 'dead', AdventurerS1MarketListing.status == 'active').distinct().limit(batch_size).all()))
        residual_ids.update((row[0] for row in AdventurerS1MarketOffer.query.join(AdventurerS1, AdventurerS1.id == AdventurerS1MarketOffer.buyer_adventurer_id).with_entities(AdventurerS1MarketOffer.buyer_adventurer_id).filter(AdventurerS1.status == 'dead', AdventurerS1MarketOffer.status == 'pending').distinct().limit(batch_size).all()))
        residual_ids.update((row[0] for row in AdventurerS1MarketBuyOrder.query.join(AdventurerS1, AdventurerS1.id == AdventurerS1MarketBuyOrder.buyer_adventurer_id).with_entities(AdventurerS1MarketBuyOrder.buyer_adventurer_id).filter(AdventurerS1.status == 'dead', AdventurerS1MarketBuyOrder.status == 'active').distinct().limit(batch_size).all()))
        residual_ids.update((row[0] for row in AdventurerS1MarketBuyOffer.query.join(AdventurerS1, AdventurerS1.id == AdventurerS1MarketBuyOffer.seller_adventurer_id).with_entities(AdventurerS1MarketBuyOffer.seller_adventurer_id).filter(AdventurerS1.status == 'dead', AdventurerS1MarketBuyOffer.status == 'pending').distinct().limit(batch_size).all()))
        if not residual_ids:
            return 0
        changed = 0
        with db_manager.transaction():
            for adventurer_id in sorted(residual_ids):
                result = AdventurerS1Service.confiscate_dead_adventurer_assets(adventurer_id)
                changed += sum((int(v or 0) for v in result.values()))
        ok, err = db_manager.commit()
        if not ok:
            raise RuntimeError(err or '死亡冒险者残留资产没收提交失败')
        return changed

    @staticmethod
    def check_owner_playable(adv: Optional['AdventurerS1'], user_id: int, *, is_admin: bool=False) -> Optional[str]:
        if not adv:
            return '冒险者不存在'
        if not is_admin and adv.owner_id != user_id:
            return '无权操作此冒险者'
        if not is_admin:
            return AdventurerS1Service.playable_status_error(adv.status)
        return None

    @staticmethod
    def _is_tavern_lv2_building_locked(inst: Optional['AdventurerS1BuildingInstance']) -> bool:
        """Lv2 锁定建筑（iron_mine/smeltery/clinic/clue_bureau 等）在酒馆未升 Lv2 时是否应禁止建造/使用。

        正常 Lv1 时这些建筑无实例（seed 跳过创建），但 GM force-set-level 降级 / 手动数据 /
        reset 可能残留实例。捐献(donate)/使用(start)/帮工(help)三处统一用此防御，
        避免普通玩家绕过前端直接打接口操作残留的 Lv2 建筑实例。
        """
        if not inst or not inst.template:
            return False
        from plugins.adventurer_s1.seed import _TAVERN_LV2_LOCKED_CODES
        if inst.template.code not in _TAVERN_LV2_LOCKED_CODES:
            return False
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        return int(S1SettingsService.get('tavern_level') or 1) < 2

    @staticmethod
    def _check_inventory_operation_allowed(adv: 'AdventurerS1', user_id: int, is_admin: bool=False) -> Optional[str]:
        """装备/仓库/特质/技能操作前置校验：resting/idle/in_party 允许；exploring 等禁止。管理员绕过。"""
        if not adv:
            return '冒险者不存在'
        if not is_admin and adv.owner_id != user_id:
            return '无权操作此冒险者'
        if not is_admin and adv.status not in AdventurerS1Service.INVENTORY_EDITABLE_STATUSES:
            if adv.status == 'exploring':
                return '探险中无法操作装备、特质和技能'
            if adv.status == 'building':
                return '执行任务中无法操作装备、特质和技能'
            if adv.status == 'dead':
                return '冒险者已死亡'
            return '当前状态无法操作装备、特质和技能'
        return None

    @staticmethod
    def record_owner_action_for_user(user_id: int, now: Optional[datetime]=None) -> None:
        """玩家手动操作后刷新不活跃判定时间。自动组队/下本等后台任务不得调用。"""
        if not user_id:
            return
        action_at = now or _portable_utc_now()
        adv = AdventurerS1.query.filter(AdventurerS1.owner_id == user_id, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).order_by(AdventurerS1.id.asc()).first()
        if adv:
            AdventurerS1.query.filter(AdventurerS1.id == adv.id, or_(AdventurerS1.last_owner_action_at.is_(None), AdventurerS1.last_owner_action_at < action_at)).update({'last_owner_action_at': action_at}, synchronize_session=False)
            return
        reservation = AdventurerS1Reservation.query.filter_by(user_id=user_id, status='pending').order_by(AdventurerS1Reservation.id.desc()).first()
        if reservation:
            AdventurerS1Reservation.query.filter(AdventurerS1Reservation.id == reservation.id, or_(AdventurerS1Reservation.last_owner_action_at.is_(None), AdventurerS1Reservation.last_owner_action_at < action_at)).update({'last_owner_action_at': action_at}, synchronize_session=False)

    @staticmethod
    def backfill_last_owner_action_at() -> None:
        """上线回填：用现有登录、创建及临时提升证据初始化玩家操作时间。"""
        from models.user import User
        adventurer_rows = AdventurerS1.query.filter(AdventurerS1.owner_id.isnot(None), AdventurerS1.last_owner_action_at.is_(None), AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).with_entities(AdventurerS1.id, AdventurerS1.owner_id, AdventurerS1.created_at).all()
        reservation_rows = AdventurerS1Reservation.query.filter(AdventurerS1Reservation.status == 'pending', AdventurerS1Reservation.last_owner_action_at.is_(None)).with_entities(AdventurerS1Reservation.id, AdventurerS1Reservation.user_id, AdventurerS1Reservation.created_at, AdventurerS1Reservation.priority_boost_until).all()
        if not adventurer_rows and (not reservation_rows):
            return
        owner_ids = {owner_id for _, owner_id, _ in adventurer_rows if owner_id}
        owner_ids.update((user_id for _, user_id, _, _ in reservation_rows if user_id))
        login_map = {}
        if owner_ids:
            login_map = dict(User.query.with_entities(User.id, User.last_login_at).filter(User.id.in_(owner_ids)).all())
        adventurer_updates = []
        for adv_id, owner_id, created_at in adventurer_rows:
            candidates = [value for value in (login_map.get(owner_id), created_at) if value is not None]
            if not candidates:
                continue
            adventurer_updates.append({'id': adv_id, 'last_owner_action_at': max(candidates)})
        if adventurer_updates:
            db.session.bulk_update_mappings(AdventurerS1, adventurer_updates)
        boost_hours = AdventurerS1Service._get_reservation_boost_hours()
        reservation_updates = []
        for reservation_id, user_id, created_at, boost_until in reservation_rows:
            candidates = [value for value in (login_map.get(user_id), created_at, boost_until - timedelta(hours=boost_hours) if boost_until else None) if value is not None]
            if not candidates:
                continue
            reservation_updates.append({'id': reservation_id, 'last_owner_action_at': max(candidates)})
        if reservation_updates:
            db.session.bulk_update_mappings(AdventurerS1Reservation, reservation_updates)

    @staticmethod
    def enter_rest(adv: AdventurerS1, now: Optional[datetime]=None) -> None:
        """让冒险者回到床位休息态：任务结束（探险/建造）后自动调用，从此刻起按房间等级回精力。

        精力恢复是惰性的（get_current_stamina 按 stamina_updated_at 算时间差），
        故进入休息只需把状态置 resting 并把计时起点刷成当前时刻。

        若已在 resting 且 rest_start_time 已存在，则视为同一段休息，只补全缺失字段，
        不重置 rest_start_time（避免误触 enter_rest 抹掉当前休息段计时）。
        """
        rest_now = now or _portable_utc_now()
        if adv.status == 'building' or adv.building_instance_id is not None:
            from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
            S1SkillSchoolService.release_usage_slot(adv.id, now=rest_now)
        AdventurerS1StateTransitionCoordinator.enter_rest(adv, rest_now)

    @staticmethod
    def leave_rest(adv: AdventurerS1, new_status: str, now: Optional[datetime]=None) -> None:
        """离开休息态去做任务（入队/建造）：先把已惰性恢复的精力落账冻结，再切到目标状态。

        必须在改状态「之前」结算精力——get_current_stamina 只在 resting 时恢复，
        一旦状态变了就不再增长，所以这里先读出当前值写回 stamina 字段。
        """
        AdventurerS1StateTransitionCoordinator.leave_rest(adv, new_status, now)

    @staticmethod
    def leave_rest_batch(adventurers: list[AdventurerS1], new_status: str, now: Optional[datetime]=None, *, tavern_seated_ids: Optional[set[int]]=None) -> None:
        """批量离开休息态；普通休息者走纯内存结算，特殊座位仅对命中者走完整单人清理。"""
        AdventurerS1StateTransitionCoordinator.leave_rest_batch(adventurers, new_status, now, tavern_seated_ids=tavern_seated_ids)

    @staticmethod
    def _flush_building_rest_like_recovery(adv: AdventurerS1, now: Optional[datetime]=None) -> None:
        """把 rest_like_recovery 建筑期间的精力/血量恢复落账。

        仅处理精力和血量，不累计/结算特质恢复；夜间睡岗这类建筑需要像初级宿舍一样
        回精力回血，但不能替代宿舍床位或疗养所推进特质恢复周期。
        """
        if adv.status != 'building' or not adv.building_instance_id:
            return
        inst = adv.building_instance
        tpl = getattr(inst, 'template', None) if inst else None
        cfg = getattr(tpl, 'settlement_config', None) or {}
        if not cfg.get('rest_like_recovery'):
            return
        now = now or _portable_utc_now()
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import flush_recovery_to_db
        flush_recovery_to_db(adv, now=now)

    @staticmethod
    def _resolve_per_hp_missing(adv: AdventurerS1) -> int:
        """按当前惰性 HP 计算缺失生命值（医院计费等场景共用）。"""
        hp_recovery_minutes = int(current_app.config.get('ADVENTURER_S1_DORM_HP_RECOVERY_MINUTES', 10))
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_hp
        current_hp = get_live_hp(adv, recovery_minutes_per_point=hp_recovery_minutes)
        return max(0, int(adv.max_hp or 100) - current_hp)

    @staticmethod
    def _resolve_hospital_treatment(config: dict, extra_params: Optional[dict], missing_hp: int) -> Tuple[Optional[Tuple[float, int, int]], Optional[str]]:
        """解析医院治疗金额 → (gold_cost, duration_minutes, heal_hp), error。

        treatment_gold 按 gold_cost_per_hp 向下取整到整点 HP；未传则按缺失 HP 全额治疗。
        """
        gold_per_hp = int(config.get('gold_cost_per_hp') or 0)
        duration_per_hp = int(config.get('usage_duration_minutes_per_hp') or 0)
        if missing_hp <= 0:
            return (None, '生命值已满，无需治疗')
        if gold_per_hp <= 0:
            return (None, '医院配置错误')
        raw = (extra_params or {}).get('treatment_gold')
        if raw is not None:
            try:
                treatment_gold = float(raw)
            except (TypeError, ValueError):
                return (None, '治疗金额无效')
            if treatment_gold <= 0:
                return (None, '治疗金额须大于 0')
            heal_hp = min(missing_hp, int(treatment_gold // gold_per_hp))
            if heal_hp < 1:
                return (None, f'治疗金额不足（至少 {gold_per_hp} 金币恢复 1 点生命值）')
        else:
            heal_hp = missing_hp
        gold_cost = heal_hp * gold_per_hp
        duration_minutes = max(1, heal_hp * duration_per_hp) if duration_per_hp > 0 and heal_hp > 0 else 0
        return ((gold_cost, duration_minutes, heal_hp), None)

    @staticmethod
    def _building_has_item_treatment(inst) -> bool:
        """治疗所是否已升至 Lv2 并解锁治疗物品辅助（handler_effects.item_treatment）。"""
        level = int(getattr(inst, 'upgrade_level', 1) or 1)
        if level <= 1 or not inst.template or (not inst.template.upgrade_config):
            return False
        effects = inst.template.upgrade_config.get('handler_effects') or {}
        return bool(effects.get('item_treatment'))

    @staticmethod
    def _resolve_healing_item_ref(adv, raw_ref) -> Tuple[Optional[dict], Optional[str]]:
        """校验并规整治疗物品 item_ref（归属当前冒险者）。

        返回 (规整后的 {source, id, name} 或 None, error)。物品无效时报错而非静默，
        给玩家即时反馈（异步 AI 阶段另有"物品无效不阻断治疗"的兜底）。
        """
        if not isinstance(raw_ref, dict):
            return (None, None)
        from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_OPERATION_PLAYER_DESTRUCTIVE, resolve_item_ref, validate_item_operation
        item, err, resolved_source = resolve_item_ref(adv, raw_ref)
        if err:
            return (None, f'治疗物品无效：{err}')
        protection_err = validate_item_operation(item, ITEM_OPERATION_PLAYER_DESTRUCTIVE, action_label='治疗所辅助治疗')
        if protection_err:
            return (None, protection_err)
        return ({'source': resolved_source, 'id': item.id, 'name': item.name}, None)

    @staticmethod
    def _resolve_sanatorium_rental(config: dict, extra_params: Optional[dict]) -> Tuple[Optional[Tuple[float, int]], Optional[str]]:
        """解析疗养所租用 → (gold_cost, duration_minutes), error。按小时计费。"""
        gold_per_hour = int(config.get('gold_cost_per_hour') or 0)
        if gold_per_hour <= 0:
            return (None, '疗养所配置错误')
        raw_hours = (extra_params or {}).get('rental_hours')
        if raw_hours is not None:
            try:
                rental_hours = int(raw_hours)
            except (TypeError, ValueError):
                return (None, '租用时长无效')
            if rental_hours < 1:
                return (None, '租用时长至少 1 小时')
            if rental_hours > 72:
                return (None, '单次租用最多 72 小时')
        else:
            rental_hours = 1
        gold_cost = rental_hours * gold_per_hour
        duration_minutes = rental_hours * 60
        return ((gold_cost, duration_minutes), None)

    @staticmethod
    def _resolve_clinic_rental(config: dict, extra_params: Optional[dict], adv: AdventurerS1) -> Tuple[Optional[Tuple[float, int, int, int]], Optional[str]]:
        """深渊手术室：自选驻留分钟，每分钟扣 gold_cost_per_minute 金币、回 hp_percent% 最大HP。

        返回 ((gold_cost, duration_minutes, heal_hp, hp_per_minute), error)。
        驻留分钟超过回满所需时自动截断；未传 rental_minutes 时默认回满。
        """
        import math
        gold_per_minute = int(config.get('gold_cost_per_minute') or 0)
        hp_percent = float(config.get('hp_percent_per_minute') or 0)
        if gold_per_minute <= 0 or hp_percent <= 0:
            return (None, '手术室配置错误')
        missing_hp = AdventurerS1Service._resolve_per_hp_missing(adv)
        if missing_hp <= 0:
            return (None, '生命值已满，无需治疗')
        from plugins.adventurer_s1.services.adventurer_s1_building_handler import ClinicHandler
        max_hp = int(adv.max_hp or 100)
        hp_per_minute_rate = max_hp * hp_percent / 100
        minutes_to_full = max(1, math.ceil(missing_hp / hp_per_minute_rate))
        raw_minutes = (extra_params or {}).get('rental_minutes')
        if raw_minutes is not None:
            try:
                rental_minutes = int(raw_minutes)
            except (TypeError, ValueError):
                return (None, '驻留时长无效')
            if rental_minutes < 1:
                return (None, '驻留时长至少 1 分钟')
            effective_minutes = min(rental_minutes, minutes_to_full)
        else:
            effective_minutes = minutes_to_full
        gold_cost = gold_per_minute * effective_minutes
        heal_hp = min(missing_hp, ClinicHandler.heal_for_elapsed_minutes(max_hp, hp_percent, effective_minutes))
        hp_per_minute = max(1, int(round(hp_per_minute_rate)))
        return ((gold_cost, effective_minutes, heal_hp, hp_per_minute), None)

    @staticmethod
    def _effective_usage_slots(inst, config: dict) -> int:
        """使用岗位上限：settlement_config.usage_slots 优先，否则 template.helper_slots，叠加升级加成。

        unlimited_usage=True 时返回 0（不限并发使用）；
        建造帮工位仍由 template.helper_slots 约束，与本函数无关。
        """
        if config.get('unlimited_usage'):
            return 0
        usage_slots = int(config.get('usage_slots') or 0)
        if usage_slots > 0:
            base = usage_slots
        elif inst.template and inst.template.helper_slots > 0:
            base = inst.template.helper_slots
        else:
            base = 0
        upgrade_level = int(getattr(inst, 'upgrade_level', 1) or 1)
        if upgrade_level > 1 and inst.template and inst.template.upgrade_config:
            bonus = int(inst.template.upgrade_config.get('usage_slots_bonus', 0) or 0)
            base += bonus
        return base

    @staticmethod
    def _effective_building_cooldown_hours(inst, config: dict) -> int:
        """建筑升级加成生效后的实际个人冷却小时数。"""
        cooldown_hours = max(0, int(config.get('cooldown_hours') or 0))
        upgrade_level = int(getattr(inst, 'upgrade_level', 1) or 1)
        if cooldown_hours <= 0 or upgrade_level <= 1 or (not inst.template) or (not inst.template.upgrade_config):
            return cooldown_hours
        effects = inst.template.upgrade_config.get('handler_effects') or {}
        cooldown_reduction = max(0, int(effects.get('cooldown_reduction_hours', 0) or 0))
        return max(0, cooldown_hours - cooldown_reduction)

    @staticmethod
    def _effective_usage_duration_minutes(adv: AdventurerS1, config: dict) -> int:
        """实际使用时长：优先读派遣时写入 building_usage_extra 的动态值。"""
        stored = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
        if stored.get('usage_duration_minutes') is not None:
            return max(0, int(stored['usage_duration_minutes']))
        duration_per_hp = int(config.get('usage_duration_minutes_per_hp') or 0)
        if duration_per_hp > 0:
            missing = AdventurerS1Service._resolve_per_hp_missing(adv)
            return max(1, missing * duration_per_hp) if missing > 0 else 0
        return int(config.get('usage_duration_minutes') or 0)

    @staticmethod
    def _effective_usage_start_at(adv: AdventurerS1, handler_key: str | None=None) -> datetime | None:
        """建筑使用计时起点。

        疗养所每完成一个特质恢复周期会重置 building_assigned_at（用于精力/血量惰性恢复），
        租用到期须用 building_usage_extra.rental_start_at。
        """
        if handler_key is None:
            inst = getattr(adv, 'building_instance', None)
            tpl = getattr(inst, 'template', None) if inst is not None else None
            handler_key = getattr(tpl, 'handler_key', None)
        if handler_key == 'sanatorium':
            extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
            raw = extra.get('rental_start_at')
            if raw:
                try:
                    return datetime.fromisoformat(raw)
                except (TypeError, ValueError):
                    pass
        return adv.building_assigned_at

    @staticmethod
    def _gender_pool() -> list:
        return list(current_app.config.get('ADVENTURER_S1_GENDERS', ('男', '女')))

    @staticmethod
    def _personality_pool() -> list:
        return list(current_app.config.get('ADVENTURER_S1_PERSONALITIES', ('勇毅', '怯懦', '鲁莽', '明哲', '狡诈', '贪婪', '悲悯', '好奇', '重诺', '傲慢', '多疑', '虔信')))

    @staticmethod
    def get_creation_options(user_id: Optional[int]=None) -> Tuple[Optional[dict], Optional[str]]:
        """返回创建冒险者所需的可选项：技能、装备、性别池、性格池、创建上限。"""
        skills = [{'id': t.id, 'name': t.name, 'description': t.description} for t in AdventurerS1SkillTemplate.query.filter_by(is_active=True).order_by(AdventurerS1SkillTemplate.id).all()]
        items = [{'id': i.id, 'name': i.name, 'type': i.type, 'description': i.description, 'rarity': i.rarity} for i in AdventurerS1Item.query.filter_by(is_starter=True).order_by(AdventurerS1Item.id).all()]
        try:
            realm_assignment = S1RealmService.get_assignment_preview(user_id)
        except S1RealmAssignmentError as exc:
            return (None, str(exc))
        return ({'skills': skills, 'items': items, 'genders': AdventurerS1Service._gender_pool(), 'personalities': AdventurerS1Service._personality_pool(), 'max_per_player': int(current_app.config.get('ADVENTURER_S1_MAX_PER_PLAYER', 1)), 'name_max_length': int(current_app.config.get('ADVENTURER_S1_NAME_MAX_LENGTH', _DEFAULT_NAME_MAX_LENGTH)), 'realm_assignment': realm_assignment}, None)

    @staticmethod
    def validate_adventurer_name(name: str) -> Tuple[Optional[str], Optional[str]]:
        """校验并规范化冒险者姓名。返回 (normalized_name, error)。"""
        normalized = (name or '').strip()
        if not normalized:
            return (None, '请输入冒险者姓名')
        name_max = int(current_app.config.get('ADVENTURER_S1_NAME_MAX_LENGTH', _DEFAULT_NAME_MAX_LENGTH))
        if len(normalized) > name_max:
            return (None, f'姓名过长（最多 {name_max} 个字）')
        if not _NAME_PATTERN.match(normalized):
            return (None, '姓名只能包含中文或英文字母，不能有数字、空格或符号')
        return (normalized, None)

    @staticmethod
    def is_adventurer_name_taken(name: str, *, exclude_adventurer_id: Optional[int]=None, exclude_reservation_id: Optional[int]=None) -> bool:
        """全服冒险者姓名是否已被占用（含 pending 预约、dead/archived）。"""
        adv_q = AdventurerS1.query.filter(AdventurerS1.name == name)
        if exclude_adventurer_id is not None:
            adv_q = adv_q.filter(AdventurerS1.id != exclude_adventurer_id)
        if adv_q.first():
            return True
        res_q = AdventurerS1Reservation.query.filter(AdventurerS1Reservation.name == name, AdventurerS1Reservation.status == 'pending')
        if exclude_reservation_id is not None:
            res_q = res_q.filter(AdventurerS1Reservation.id != exclude_reservation_id)
        return res_q.first() is not None

    @staticmethod
    def _reserved_world_entity_names(names: set[str]) -> set[str]:
        """返回候选中已被世界情报实体或长期主体称谓占用的精确名称。"""
        candidates = {str(name).strip() for name in names if str(name).strip()}
        if not candidates:
            return set()
        reserved = {title for title, in db_manager.query(AdventurerS1WorldStoryThread.title).filter(AdventurerS1WorldStoryThread.thread_type == 'entity', AdventurerS1WorldStoryThread.title.in_(candidates)).all()}
        actor_rows = db_manager.query(AdventurerS1WorldActor.canonical_name, AdventurerS1WorldActor.aliases).all()
        for canonical_name, aliases in actor_rows:
            if canonical_name in candidates:
                reserved.add(canonical_name)
            if not isinstance(aliases, list):
                continue
            reserved.update((alias.strip() for alias in aliases if isinstance(alias, str) and alias.strip() in candidates))
        return reserved

    @staticmethod
    def assert_adventurer_name_available(name: str, *, exclude_adventurer_id: Optional[int]=None, exclude_reservation_id: Optional[int]=None) -> Optional[str]:
        """姓名可用则返回 None，否则返回玩家可读错误。"""
        if AdventurerS1Service.is_adventurer_name_taken(name, exclude_adventurer_id=exclude_adventurer_id, exclude_reservation_id=exclude_reservation_id):
            return f'名字【{name}】已被使用，请换一个名字！'
        if name in AdventurerS1Service._reserved_world_entity_names({name}):
            return f'名字【{name}】已被世界情报实体使用，请换一个名字！'
        return None

    @staticmethod
    def submit_reservation(user_id: int, name: str, gender: str, personality: str, skill_template_id, item_ids) -> Tuple[Optional[dict], Optional[str]]:
        """玩家提交冒险者预约（不直接创建）。返回 (预约dict, 错误信息)。"""
        name, err = AdventurerS1Service.validate_adventurer_name(name)
        if err:
            return (None, err)
        dup_err = AdventurerS1Service.assert_adventurer_name_available(name)
        if dup_err:
            return (None, dup_err)
        if gender not in AdventurerS1Service._gender_pool():
            return (None, '性别选择无效')
        if personality not in AdventurerS1Service._personality_pool():
            return (None, '性格选择无效')
        try:
            skill_template_id = int(skill_template_id)
        except (TypeError, ValueError):
            return (None, '技能参数无效')
        if not isinstance(item_ids, list) or len(item_ids) != 2:
            return (None, '请选择 2 件初始装备')
        try:
            item_ids = [int(x) for x in item_ids]
        except (TypeError, ValueError):
            return (None, '装备参数无效')
        if item_ids[0] == item_ids[1]:
            return (None, '请选择 2 件不同的装备')
        max_per = int(current_app.config.get('ADVENTURER_S1_MAX_PER_PLAYER', 1))
        owned = AdventurerS1Service.count_tavern_active_owned(user_id)
        if owned >= max_per:
            return (None, f'每人最多拥有 {max_per} 个冒险者，你已达上限')
        archived = AdventurerS1.query.filter(AdventurerS1.owner_id == user_id, AdventurerS1.is_player_created == True, AdventurerS1.status == 'archived').first()
        if archived:
            return (None, '你的冒险者已封存清退，请通过「申请解封」提交理由，等待管理员审核')
        existing = AdventurerS1Reservation.query.filter_by(user_id=user_id, status='pending').first()
        if existing:
            return (None, '你已有一个预约在排队，请等待抽取结果')
        tpl = AdventurerS1SkillTemplate.query.filter_by(id=skill_template_id, is_active=True).first()
        if not tpl:
            return (None, '所选初始技能不存在')
        starter_items = AdventurerS1Item.query.filter(AdventurerS1Item.id.in_(item_ids), AdventurerS1Item.is_starter == True).all()
        if len(starter_items) != 2:
            return (None, '所选初始装备无效')
        try:
            with db_manager.transaction():
                if S1RealmRepository.lock_user(user_id) is None:
                    raise S1PlayerVisibleError('用户不存在，无法提交预约')
                S1RealmService.require_free_active_slot(user_id)
                if S1RealmRepository.find_archived_player_adventurer(user_id):
                    raise S1PlayerVisibleError('你的冒险者已封存清退，请通过「申请解封」提交理由，等待管理员审核')
                if S1RealmRepository.find_pending_reservation(user_id):
                    raise S1PlayerVisibleError('你已有一个预约在排队，请等待抽取结果')
                r = AdventurerS1Reservation(user_id=user_id, name=name, gender=gender, personality=personality, skill_template_id=skill_template_id, item_ids=item_ids, priority='normal', status='pending', realm_code=S1RealmService.current_runtime_realm(), last_owner_action_at=_portable_utc_now())
                ok, err = player_database_result(db_manager.add(r))
                if not ok:
                    raise RuntimeError(err)
                new_id = r.id
        except Exception as e:
            return (None, player_operation_failure(e, '预约失败'))
        r = AdventurerS1Reservation.query.session.get(AdventurerS1Reservation, new_id)
        return (r.to_player_dict(), None)

    @staticmethod
    def _get_reservation_boost_hours() -> int:
        return int(current_app.config.get('ADVENTURER_S1_RESERVATION_BOOST_HOURS', 6))

    @staticmethod
    def _expire_reservation_boost_if_needed(r: AdventurerS1Reservation) -> bool:
        """玩家临时提升过期后恢复为 normal。返回是否发生了变更。"""
        if r.priority != _RESERVATION_PRIORITY_ELEVATED:
            return False
        if r.priority_boost_until and r.priority_boost_until > _portable_utc_now():
            return False
        r.priority = _RESERVATION_PRIORITY_NORMAL
        r.priority_boost_until = None
        return True

    @staticmethod
    def _expire_pending_reservation_boosts(reservations) -> None:
        """批量清理已过期的玩家临时提升（抽取前调用）。"""
        changed = False
        for r in reservations:
            if AdventurerS1Service._expire_reservation_boost_if_needed(r):
                changed = True
        if changed:
            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or '清理过期临时提升失败')

    @staticmethod
    def get_user_reservation_status(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """查询当前用户的 pending 预约状态。"""
        r = AdventurerS1Reservation.query.filter_by(user_id=user_id, status='pending').order_by(AdventurerS1Reservation.created_at.desc()).first()
        if not r:
            return (None, None)
        try:
            with db_manager.transaction():
                AdventurerS1Service._expire_reservation_boost_if_needed(r)
        except Exception as e:
            return (None, player_operation_failure(e, '查询失败'))
        return (r.to_player_dict(), None)

    @staticmethod
    def boost_reservation_priority(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """玩家临时提高 pending 预约优先级（6 小时内有效，到期后可再次使用）。"""
        r = AdventurerS1Reservation.query.filter_by(user_id=user_id, status='pending').first()
        if not r:
            return (None, '你没有待处理的预约')
        reservation_id = int(r.id)
        try:
            from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
            linked = False
            if S1SettingsService.get('reservation_alt_auto_low_priority'):
                context = AdventurerS1Service._get_reservation_alt_context(24)
                linked = any((user_id in members and len(members) >= 2 for members in context[2].values()))
            with db_manager.transaction():
                r = S1RealmRepository.lock_reservation(reservation_id)
                if r is None or r.user_id != int(user_id) or r.status != 'pending':
                    raise S1PlayerVisibleError('预约状态已经变化，请刷新页面')
                AdventurerS1Service._expire_reservation_boost_if_needed(r)
                now = _portable_utc_now()
                if r.priority == _RESERVATION_PRIORITY_ELEVATED and r.priority_boost_until and (r.priority_boost_until > now):
                    raise S1PlayerVisibleError('优先级提升仍在生效中，请待结束后再试')
                if r.priority != _RESERVATION_PRIORITY_NORMAL:
                    raise S1PlayerVisibleError('当前预约无法使用此功能')
                if linked and (not S1SettingsService.get('reservation_alt_auto_low_priority')):
                    linked = False
                hours = AdventurerS1Service._get_reservation_boost_hours()
                r.priority = _RESERVATION_PRIORITY_LOW if linked else _RESERVATION_PRIORITY_ELEVATED
                r.priority_boost_until = None if linked else now + timedelta(hours=hours)
                r.last_owner_action_at = now
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        return (r.to_player_dict(), None)

    @staticmethod
    def cancel_reservation(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """玩家取消自己的 pending 预约。"""
        r = AdventurerS1Reservation.query.filter_by(user_id=user_id, status='pending').first()
        if not r:
            return (None, '你没有待处理的预约')
        try:
            with db_manager.transaction():
                r = S1RealmRepository.lock_reservation(int(r.id))
                if r is None or r.user_id != int(user_id) or r.status != 'pending':
                    raise S1PlayerVisibleError('预约状态已经变化，请刷新页面')
                r.status = 'cancelled'
                if r.restore_adventurer_id:
                    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import AdventurerS1ArchiveAppealService
                    AdventurerS1ArchiveAppealService.mark_cancelled_for_reservation(r.id)
        except Exception as e:
            return (None, player_operation_failure(e, '取消失败'))
        return ({'cancelled': True}, None)

    @staticmethod
    def update_reservation(user_id: int, name: str=None, gender: str=None, personality: str=None, skill_template_id=None, item_ids=None) -> Tuple[Optional[dict], Optional[str]]:
        """玩家修改自己的 pending 预约（只更新传入的字段）。"""
        r = AdventurerS1Reservation.query.filter_by(user_id=user_id, status='pending').first()
        if not r:
            return (None, '你没有待处理的预约')
        if r.restore_adventurer_id:
            return (None, '解封排队中的预约不可修改，请等待管理员抽取')
        if name is not None:
            name, err = AdventurerS1Service.validate_adventurer_name(name)
            if err:
                return (None, err)
            dup_err = AdventurerS1Service.assert_adventurer_name_available(name, exclude_reservation_id=r.id)
            if dup_err:
                return (None, dup_err)
        if gender is not None:
            if gender not in AdventurerS1Service._gender_pool():
                return (None, '性别选择无效')
        if personality is not None:
            if personality not in AdventurerS1Service._personality_pool():
                return (None, '性格选择无效')
        if skill_template_id is not None:
            try:
                skill_template_id = int(skill_template_id)
            except (TypeError, ValueError):
                return (None, '技能参数无效')
            tpl = AdventurerS1SkillTemplate.query.filter_by(id=skill_template_id, is_active=True).first()
            if not tpl:
                return (None, '所选初始技能不存在')
        if item_ids is not None:
            if not isinstance(item_ids, list) or len(item_ids) != 2:
                return (None, '请选择 2 件初始装备')
            try:
                item_ids = [int(x) for x in item_ids]
            except (TypeError, ValueError):
                return (None, '装备参数无效')
            if item_ids[0] == item_ids[1]:
                return (None, '请选择 2 件不同的装备')
            starter_items = AdventurerS1Item.query.filter(AdventurerS1Item.id.in_(item_ids), AdventurerS1Item.is_starter == True).all()
            if len(starter_items) != 2:
                return (None, '所选初始装备无效')
        try:
            with db_manager.transaction():
                r = S1RealmRepository.lock_reservation(int(r.id))
                if r is None or r.user_id != int(user_id) or r.status != 'pending':
                    raise S1PlayerVisibleError('预约状态已经变化，请刷新页面')
                if r.restore_adventurer_id:
                    raise S1PlayerVisibleError('解封排队中的预约不可修改，请等待管理员抽取')
                if name is not None:
                    r.name = name
                if gender is not None:
                    r.gender = gender
                if personality is not None:
                    r.personality = personality
                if skill_template_id is not None:
                    r.skill_template_id = skill_template_id
                if item_ids is not None:
                    r.item_ids = item_ids
                r.last_owner_action_at = _portable_utc_now()
        except Exception as e:
            return (None, player_operation_failure(e, '修改失败'))
        return (r.to_player_dict(), None)

    @staticmethod
    def rename_initial_adventurer(adventurer_id: int, user_id: int, name: str) -> Tuple[Optional[dict], Optional[str]]:
        """使用一次抽中后初始命名机会；失败不消耗，成功后永久关闭。"""
        normalized_name, err = AdventurerS1Service.validate_adventurer_name(name)
        if err:
            return (None, err)
        bed_draw_lock = _try_acquire_bed_draw_lock()
        if bed_draw_lock is None:
            return (None, '酒馆正在处理其他抽取或命名，请稍后再试')
        try:
            renamed_id = None
            domain_error = None
            try:
                with db_manager.transaction():
                    adv = AdventurerS1.query.filter_by(id=int(adventurer_id)).with_for_update().first()
                    if not adv:
                        domain_error = '冒险者不存在'
                    elif adv.owner_id != user_id:
                        domain_error = '只能为自己的冒险者命名'
                    elif not adv.initial_rename_available:
                        domain_error = '该冒险者没有可用的初始命名机会'
                    else:
                        domain_error = AdventurerS1Service.assert_adventurer_name_available(normalized_name, exclude_adventurer_id=adv.id)
                        if not domain_error:
                            old_name = adv.name
                            adv.name = normalized_name
                            adv.initial_rename_available = False
                            reservation = AdventurerS1Reservation.query.filter_by(user_id=user_id, status='drawn', name=old_name).order_by(AdventurerS1Reservation.id.desc()).with_for_update().first()
                            if reservation:
                                reservation.name = normalized_name
                                reservation.reject_reason = None
                            renamed_id = adv.id
            except Exception as exc:
                return (None, player_operation_failure(exc, '命名失败'))
            if domain_error:
                return (None, domain_error)
            invalidate_adventurer_detail_cache(renamed_id)
            invalidate_dormitory_cache()
            from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import invalidate_matchmaking_read_cache
            from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
            from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import invalidate_tea_house_seats_cache
            invalidate_matchmaking_read_cache()
            invalidate_parties_board_cache()
            invalidate_tea_house_seats_cache()
            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                socketio.emit('adventurer_changed', {'action': 'initial_name_changed', 'ids': [renamed_id]}, namespace='/')
            except Exception:
                current_app.logger.warning('S1 初始命名成功后推送刷新失败 adventurer_id=%s', renamed_id, exc_info=True)
            adv = AdventurerS1.query.session.get(AdventurerS1, renamed_id)
            item_dict, clue_meta = AdventurerS1Service._build_item_catalog_context(adv)
            return (adv.to_dict(item_dict=item_dict, item_clue_meta=clue_meta, current_user_id=user_id, is_admin=False), None)
        finally:
            _release_bed_draw_lock(bed_draw_lock)

    @staticmethod
    def _room_allows_adventurer(room: Optional['AdventurerS1DormRoom'], adv: Optional['AdventurerS1']) -> bool:
        return room is not None and adv is not None and (room.room_key is None)

    @staticmethod
    def _has_valid_bed(adv: 'AdventurerS1', room_map: Optional[dict]=None) -> bool:
        """冒险者是否占用了有效且属于其床位池的床位。"""
        if adv.dorm_room_id is None or adv.bed_index is None:
            return False
        if room_map is not None:
            room = room_map.get(adv.dorm_room_id)
        else:
            room = AdventurerS1DormRoom.query.session.get(AdventurerS1DormRoom, adv.dorm_room_id)
        if not room:
            return False
        return AdventurerS1Service._room_allows_adventurer(room, adv) and 0 <= adv.bed_index < room.capacity

    @staticmethod
    def _is_bed_slot_occupied(room_id: int, bed_index: int, exclude_adv_id: Optional[int]=None) -> bool:
        """床位槽是否已被编制内冒险者占用（用于分配前校验）。"""
        q = AdventurerS1.query.filter(AdventurerS1.dorm_room_id == room_id, AdventurerS1.bed_index == bed_index, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES))
        if exclude_adv_id is not None:
            q = q.filter(AdventurerS1.id != exclude_adv_id)
        return q.first() is not None

    @staticmethod
    def _remove_bed_from_pool(free_beds: list, room_id: int, bed_index: int) -> None:
        """从共享空床池移除指定槽位（解封/批量分配后同步池状态）。"""
        slot = (room_id, bed_index)
        for i, item in enumerate(free_beds):
            if item == slot:
                free_beds.pop(i)
                return

    @staticmethod
    def _claim_bed_slot(adv: 'AdventurerS1', room_id: int, bed_index: int) -> bool:
        """在已有 transaction 内原子占床：锁房间行 + 复查槽位空闲后写入。

        调用方须已在 db_manager.transaction() 内；并发时后到的请求读到 occupied 返回 False。
        """
        room = AdventurerS1DormRoom.query.filter_by(id=room_id).with_for_update().first()
        if not room or not AdventurerS1Service._room_allows_adventurer(room, adv) or bed_index < 0 or (bed_index >= room.capacity):
            return False
        occupant_q = AdventurerS1.query.filter(AdventurerS1.dorm_room_id == room_id, AdventurerS1.bed_index == bed_index, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES))
        if adv.id is not None:
            occupant_q = occupant_q.filter(AdventurerS1.id != adv.id)
        if occupant_q.with_for_update().first():
            return False
        adv.dorm_room_id = room_id
        adv.bed_index = bed_index
        return True

    @staticmethod
    def _find_duplicate_bed_groups() -> List[dict]:
        """查找重复占床：同一 (room_id, bed_index) 上有多名编制内冒险者。"""
        advs = AdventurerS1.query.filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES), AdventurerS1.dorm_room_id.isnot(None), AdventurerS1.bed_index.isnot(None)).order_by(AdventurerS1.id).all()
        if not advs:
            return []
        room_ids = {adv.dorm_room_id for adv in advs if adv.dorm_room_id}
        room_map = {r.id: r for r in AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.id.in_(room_ids)).all()}
        grouped: dict = {}
        for adv in advs:
            if not AdventurerS1Service._has_valid_bed(adv, room_map=room_map):
                continue
            key = (adv.dorm_room_id, adv.bed_index)
            grouped.setdefault(key, []).append(adv.id)
        result = []
        for (room_id, bed_index), adv_ids in grouped.items():
            if len(adv_ids) < 2:
                continue
            room = room_map.get(room_id)
            result.append({'room_id': room_id, 'room_name': room.name if room else f'#{room_id}', 'bed_index': bed_index, 'adventurer_ids': adv_ids})
        return result

    @staticmethod
    def _count_duplicate_bed_slots() -> int:
        """重复占床人数（每组超出 1 人的部分之和）。"""
        return sum((len(g['adventurer_ids']) - 1 for g in AdventurerS1Service._find_duplicate_bed_groups()))

    @staticmethod
    def repair_duplicate_bed_assignments() -> Tuple[Optional[dict], Optional[str]]:
        """管理员修复重复占床：每组保留 id 最小者，其余挪到真实空床。"""
        groups = AdventurerS1Service._find_duplicate_bed_groups()
        if not groups:
            return ({'repaired_count': 0, 'details': [], 'capacity': AdventurerS1Service.get_draw_capacity_stats()}, None)
        free_beds = AdventurerS1Service._collect_free_bed_slots()
        random.shuffle(free_beds)
        details = []
        try:
            with db_manager.transaction():
                for group in groups:
                    adv_ids = sorted(group['adventurer_ids'])
                    keeper_id = adv_ids[0]
                    for adv_id in adv_ids[1:]:
                        adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
                        if not adv:
                            continue
                        old_room_id = adv.dorm_room_id
                        old_bed_index = adv.bed_index
                        assigned = AdventurerS1Service._assign_random_bed(adv, free_beds=free_beds)
                        if not assigned:
                            raise RuntimeError(f"空床不足，无法为「{adv.name}」分配床位（重复床位：{group['room_name']} 第 {group['bed_index'] + 1} 号）")
                        details.append({'adventurer_id': adv.id, 'adventurer_name': adv.name, 'from_room_id': old_room_id, 'from_bed_index': old_bed_index, 'to_room_id': adv.dorm_room_id, 'to_bed_index': adv.bed_index, 'kept_adventurer_id': keeper_id})
        except Exception as e:
            logger.exception('修复重复占床失败')
            return (None, str(e))
        invalidate_dormitory_cache()
        return ({'repaired_count': len(details), 'details': details, 'capacity': AdventurerS1Service.get_draw_capacity_stats()}, None)

    @staticmethod
    def _collect_free_bed_slots() -> List[tuple]:
        """收集普通宿舍空床；系统专属房间不进入抽取或容量统计。"""
        rooms = db.session.query(AdventurerS1DormRoom.id, AdventurerS1DormRoom.capacity).filter(AdventurerS1DormRoom.room_key.is_(None)).order_by(AdventurerS1DormRoom.id).all()
        room_capacity = {int(room_id): int(capacity) for room_id, capacity in rooms}
        occupied = {(int(room_id), int(bed_index)) for room_id, bed_index in db.session.query(AdventurerS1.dorm_room_id, AdventurerS1.bed_index).filter(AdventurerS1.dorm_room_id.isnot(None), AdventurerS1.bed_index.isnot(None)).all() if int(room_id) in room_capacity and 0 <= int(bed_index) < room_capacity[int(room_id)]}
        return [(int(room_id), idx) for room_id, capacity in rooms for idx in range(int(capacity)) if (int(room_id), idx) not in occupied]

    @staticmethod
    def _fill_homeless_adventurers(free_beds: list) -> int:
        """给无有效床位的冒险者补床。"""
        if not free_beds:
            return 0
        to_fill = db.session.query(AdventurerS1).outerjoin(AdventurerS1DormRoom, AdventurerS1.dorm_room_id == AdventurerS1DormRoom.id).filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES), or_(AdventurerS1.dorm_room_id.is_(None), AdventurerS1.bed_index.is_(None), AdventurerS1DormRoom.id.is_(None), AdventurerS1DormRoom.room_key.isnot(None), AdventurerS1.bed_index < 0, AdventurerS1.bed_index >= AdventurerS1DormRoom.capacity)).options(noload('*')).order_by(AdventurerS1.id).limit(len(free_beds)).all()
        if not to_fill:
            return 0
        filled = 0
        try:
            with db_manager.transaction():
                for adv in to_fill:
                    if not free_beds:
                        break
                    if AdventurerS1Service._assign_random_bed(adv, free_beds=free_beds):
                        filled += 1
        except Exception:
            logger.exception('补分配无床位冒险者失败')
            return 0
        if filled:
            invalidate_dormitory_cache()
        return filled

    @staticmethod
    def get_draw_capacity_stats() -> dict:
        """管理员抽取面板：存活冒险者数与宿舍床位容量（与 draw 空床计算一致）。"""
        from sqlalchemy import func
        alive_count = AdventurerS1.query.filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).count()
        total_beds = int(db.session.query(func.coalesce(func.sum(AdventurerS1DormRoom.capacity), 0)).filter(AdventurerS1DormRoom.room_key.is_(None)).scalar() or 0)
        free_slots = AdventurerS1Service._collect_free_bed_slots()
        return {'alive_count': alive_count, 'total_beds': total_beds, 'empty_beds': len(free_slots), 'duplicate_beds': AdventurerS1Service._count_duplicate_bed_slots()}

    @staticmethod
    def list_reservations(page: int, per_page: int, status: Optional[str]=None, search: Optional[str]=None, priority: Optional[str]=None) -> Tuple[Optional[dict], Optional[str]]:
        """管理员分页浏览预约列表。可按 status / priority 筛选、按冒险者名字/玩家名字/玩家ID 搜索。"""
        q = AdventurerS1Reservation.query.filter(AdventurerS1Reservation.status != 'cancelled')
        if status and status in ('pending', 'drawn', 'rejected'):
            q = q.filter(AdventurerS1Reservation.status == status)
        if priority and priority in _RESERVATION_PRIORITIES:
            q = q.filter(AdventurerS1Reservation.priority == priority)
        if search:
            search = search.strip()
            from extensions import db
            from models.user import User
            if search.isdigit():
                q = q.filter(AdventurerS1Reservation.user_id == int(search))
            else:
                like = f'%{search}%'
                q = q.join(User, AdventurerS1Reservation.user_id == User.id).filter(db.or_(AdventurerS1Reservation.name.ilike(like), User.username.ilike(like), User.nickname.ilike(like)))
        q = q.order_by(AdventurerS1Reservation.created_at.desc())
        total = q.count()
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
        items = [r.to_dict() for r in rows]
        if items:
            from models.user import User
            from utils.time_utils import to_utc_iso
            user_ids = {item['user_id'] for item in items}
            cutoff = AdventurerS1Service._get_draw_online_cutoff()
            now = _portable_utc_now()
            login_map = dict(User.query.with_entities(User.id, User.last_login_at).filter(User.id.in_(user_ids)).all())
            for r, item in zip(rows, items):
                last_login_at = login_map.get(r.user_id)
                item['last_login_at'] = to_utc_iso(last_login_at) if last_login_at else None
                boost_active = AdventurerS1Service._has_active_player_priority_boost(r, now=now)
                item['priority_boost_active'] = boost_active
                item['draw_eligible'] = AdventurerS1Service._is_reservation_draw_eligible(r, last_login_at, now=now, cutoff=cutoff)
                item['draw_eligible_via_boost'] = boost_active and (not (last_login_at and last_login_at >= cutoff))
        result = build_paginated_response(items, total, page, per_page)
        result['capacity'] = AdventurerS1Service.get_draw_capacity_stats()
        return (result, None)

    @staticmethod
    def _get_ip_segment(ip_str: Optional[str], segment_bits: int=24) -> Optional[str]:
        """与 admin/security.py 一致的 IPv4 网段划分（/24 或 /16）。"""
        if not ip_str or ip_str == 'unknown':
            return None
        try:
            parts = ip_str.split('.')
            if len(parts) == 4:
                if segment_bits == 24:
                    return f'{parts[0]}.{parts[1]}.{parts[2]}.0/24'
                if segment_bits == 16:
                    return f'{parts[0]}.{parts[1]}.0.0/16'
        except (ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _build_ip_alt_link_maps(user_ids, user_map, segment_bits: int):
        """为 IP 关联检测构建注册/登录 IP 与网段索引。"""
        from collections import defaultdict
        reg_ip_map = defaultdict(list)
        reg_seg_map = defaultdict(list)
        login_ip_map = defaultdict(list)
        login_seg_map = defaultdict(list)
        for uid in user_ids:
            u = user_map.get(uid)
            if not u:
                continue
            if u.register_ip and u.register_ip not in ('', 'unknown'):
                reg_ip_map[u.register_ip].append(uid)
            reg_seg = AdventurerS1Service._get_ip_segment(u.register_ip, segment_bits)
            if reg_seg:
                reg_seg_map[reg_seg].append(uid)
            if u.last_login_ip and u.last_login_ip not in ('', 'unknown'):
                login_ip_map[u.last_login_ip].append(uid)
            login_seg = AdventurerS1Service._get_ip_segment(u.last_login_ip, segment_bits)
            if login_seg:
                login_seg_map[login_seg].append(uid)
        return (reg_ip_map, reg_seg_map, login_ip_map, login_seg_map)

    @staticmethod
    def _cluster_user_ids_by_ip(user_ids, user_map, segment_bits: int):
        """Union-Find：按注册/登录 IP 与网段聚类用户。"""
        from collections import defaultdict
        reg_ip_map, reg_seg_map, login_ip_map, login_seg_map = AdventurerS1Service._build_ip_alt_link_maps(user_ids, user_map, segment_bits)
        parent = {uid: uid for uid in user_ids}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = (find(a), find(b))
            if ra != rb:
                parent[rb] = ra

        def link_by_key(key_to_uids: dict) -> None:
            for uids in key_to_uids.values():
                if len(uids) >= 2:
                    base = uids[0]
                    for other in uids[1:]:
                        union(base, other)
        link_by_key(reg_ip_map)
        link_by_key(reg_seg_map)
        link_by_key(login_ip_map)
        link_by_key(login_seg_map)
        clusters = defaultdict(list)
        for uid in user_ids:
            clusters[find(uid)].append(uid)
        return (clusters, reg_ip_map, reg_seg_map, login_ip_map, login_seg_map)

    @staticmethod
    def _summarize_ip_alt_group(members: list, reg_ip_map, reg_seg_map, login_ip_map, login_seg_map, *, register_segment_label: str='同注册IP段', login_segment_label: str='同登录IP段') -> tuple:
        """返回 (link_summary, link_types) 按优先级取最强关联依据。"""
        member_set = set(members)
        for ip, uids in reg_ip_map.items():
            overlap = [u for u in uids if u in member_set]
            if len(overlap) >= 2:
                return (f'同注册IP {ip}', ['same_register_ip'])
        for seg, uids in reg_seg_map.items():
            overlap = [u for u in uids if u in member_set]
            if len(overlap) >= 2:
                return (f'{register_segment_label} {seg}（{len(overlap)} 人）', ['register_segment'])
        for ip, uids in login_ip_map.items():
            overlap = [u for u in uids if u in member_set]
            if len(overlap) >= 2:
                return (f'同登录IP {ip}', ['same_login_ip'])
        for seg, uids in login_seg_map.items():
            overlap = [u for u in uids if u in member_set]
            if len(overlap) >= 2:
                return (f'{login_segment_label} {seg}（{len(overlap)} 人）', ['login_segment'])
        return ('多账号关联', ['linked'])

    @staticmethod
    def _get_reservation_alt_context(segment_bits: int):
        """Detached queue snapshot plus current authoritative account IP evidence."""
        from db_repository.s1_reservation_repository import S1ReservationRepository
        from plugins.adventurer_s1.services.s1_reservation_ip_service import load_ip_users
        rows = S1ReservationRepository.list_alt_candidates(S1RealmService.current_runtime_realm())
        res_by_user = {}
        for row in rows:
            res_by_user.setdefault(row.user_id, row)
        user_ids = set(res_by_user)
        user_map = load_ip_users(user_ids)
        clusters, *maps = AdventurerS1Service._cluster_user_ids_by_ip(user_ids, user_map, segment_bits)
        return (res_by_user, user_map, clusters, *maps)

    @staticmethod
    def auto_lower_alt_reservation_priorities() -> Tuple[Optional[dict], Optional[str]]:
        """Only pending elevated rows may change; a replay is a conditional no-op."""
        from db_repository.s1_reservation_repository import S1ReservationRepository
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        try:
            enabled = bool(S1SettingsService.get('reservation_alt_auto_low_priority'))
            result = {'enabled': enabled, 'lowered_count': 0}
            realm = S1RealmService.current_runtime_realm()
            if not enabled or not S1ReservationRepository.has_elevated_reservation(realm):
                return (result, None)
            res_by_user, _users, clusters, *_maps = AdventurerS1Service._get_reservation_alt_context(24)
            linked_users = {uid for members in clusters.values() if len(members) >= 2 for uid in members}
            targets = [row.id for uid, row in res_by_user.items() if uid in linked_users and row.status == 'pending' and (row.priority == 'elevated')]
            if targets:
                with db_manager.transaction():
                    result['enabled'] = bool(S1SettingsService.get('reservation_alt_auto_low_priority'))
                    if result['enabled']:
                        result['lowered_count'] = S1ReservationRepository.lower_elevated_priorities(targets, realm)
            return (result, None)
        except Exception:
            current_app.logger.exception('S1 reservation automatic alt priority check failed')
            return (None, '预约同伙自动降优先级检查失败，请稍后重试')

    @staticmethod
    def run_reservation_alt_priority_job() -> None:
        result, err = AdventurerS1Service.auto_lower_alt_reservation_priorities()
        if err:
            current_app.logger.warning('%s', err)
        elif result and result['lowered_count']:
            current_app.logger.info('S1 automatically lowered %s linked reservations', result['lowered_count'])

    @staticmethod
    def detect_reservation_alts(segment_bits: int=24) -> Tuple[Optional[dict], Optional[str]]:
        """检测 pending / drawn 预约中疑似同一人多号（IP 关联分组，仅标记不做处理）。"""
        from collections import defaultdict
        if segment_bits not in (16, 24):
            return (None, 'segment_bits 仅支持 16 或 24')
        try:
            res_by_user, user_map, clusters, reg_ip_map, reg_seg_map, login_ip_map, login_seg_map = AdventurerS1Service._get_reservation_alt_context(segment_bits)
        except Exception:
            current_app.logger.exception('S1 reservation IP detection evidence read failed')
            return (None, '预约同伙检测暂不可用，请稍后重试')
        user_ids = set(res_by_user.keys())
        pending_count = sum((1 for r in res_by_user.values() if r.status == 'pending'))
        drawn_count = sum((1 for r in res_by_user.values() if r.status == 'drawn'))
        if not user_ids:
            return ({'groups': [], 'user_group_map': {}, 'total_pending': 0, 'total_drawn': 0, 'total_scanned': 0, 'flagged_members': [], 'group_count': 0, 'flagged_count': 0}, None)
        drawn_user_ids = {uid for uid, r in res_by_user.items() if r.status == 'drawn'}
        adv_by_owner: dict[int, list] = defaultdict(list)
        if drawn_user_ids:
            adv_rows = AdventurerS1.query.filter(AdventurerS1.owner_id.in_(drawn_user_ids), AdventurerS1.is_player_created == True, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).order_by(AdventurerS1.id.asc()).all()
            for adv in adv_rows:
                adv_by_owner[adv.owner_id].append(adv)
        groups = []
        user_group_map = {}
        group_id = 0
        for _root, members in sorted(clusters.items(), key=lambda x: (-len(x[1]), min(x[1]))):
            if len(members) < 2:
                continue
            group_id += 1
            members_sorted = sorted(members)
            link_summary, link_types = AdventurerS1Service._summarize_ip_alt_group(members_sorted, reg_ip_map, reg_seg_map, login_ip_map, login_seg_map, register_segment_label='预约队列同注册IP段', login_segment_label='预约队列同登录IP段')
            group_members = []
            group_pending = 0
            group_drawn = 0
            for uid in members_sorted:
                u = user_map.get(uid)
                r = res_by_user.get(uid)
                if not u or not r:
                    continue
                user_group_map[uid] = group_id
                if r.status == 'pending':
                    group_pending += 1
                elif r.status == 'drawn':
                    group_drawn += 1
                advs = adv_by_owner.get(uid) or []
                member = {'user_id': uid, 'username': u.username, 'nickname': u.nickname or u.username, 'register_ip': u.register_ip, 'last_login_ip': u.last_login_ip, 'is_frozen': bool(u.is_frozen), 'reservation_id': r.id, 'adventurer_name': r.name, 'priority': r.priority, 'status': r.status, 'active_adventurer_count': len(advs), 'adventurers': [{'id': adv.id, 'name': adv.name, 'status': adv.status, 'level': adv.level} for adv in advs]}
                group_members.append(member)
            if len(group_members) >= 2:
                groups.append({'group_id': group_id, 'member_count': len(group_members), 'pending_count': group_pending, 'drawn_count': group_drawn, 'link_summary': link_summary, 'link_types': link_types, 'members': group_members})
        flagged_count = len(user_group_map)
        flagged_members = []
        for g in groups:
            for m in g['members']:
                flagged_members.append({**m, 'group_id': g['group_id'], 'link_summary': g['link_summary']})
        flagged_members.sort(key=lambda row: (row['group_id'], 0 if row['status'] == 'drawn' else 1, row['user_id']))
        return ({'groups': groups, 'user_group_map': user_group_map, 'flagged_members': flagged_members, 'total_pending': pending_count, 'total_drawn': drawn_count, 'total_scanned': len(res_by_user), 'group_count': len(groups), 'flagged_count': flagged_count}, None)

    @staticmethod
    def detect_adventurer_alts(segment_bits: int=24) -> Tuple[Optional[dict], Optional[str]]:
        """检测在册冒险者中疑似同一人多号（IP 关联分组，仅标记不做处理）。"""
        from collections import defaultdict
        from db_repository.s1_reservation_repository import S1ReservationRepository
        from plugins.adventurer_s1.services.s1_reservation_ip_service import load_ip_users
        if segment_bits not in (16, 24):
            return (None, 'segment_bits 仅支持 16 或 24')
        realm = S1RealmService.current_runtime_realm()
        adv_rows = S1ReservationRepository.list_adventurer_alt_candidates(realm)
        adv_by_owner: dict[int, list] = defaultdict(list)
        for adv in adv_rows:
            adv_by_owner[adv.owner_id].append(adv)
        user_ids = set(adv_by_owner.keys())
        if not user_ids:
            return ({'groups': [], 'user_group_map': {}, 'same_account_multi': [], 'total_owners': 0, 'total_adventurers': 0, 'group_count': 0, 'flagged_count': 0}, None)
        res_by_user = {row.user_id: row for row in S1ReservationRepository.list_latest_reservation_statuses(user_ids, realm)}
        try:
            user_map = load_ip_users(user_ids)
        except Exception:
            current_app.logger.exception('S1 adventurer IP detection evidence read failed')
            return (None, '多号冒险者检测暂不可用，请稍后重试')
        same_account_multi = []
        for uid, advs in adv_by_owner.items():
            if len(advs) <= 1:
                continue
            u = user_map.get(uid)
            same_account_multi.append({'user_id': uid, 'username': u.username if u else '', 'nickname': u.nickname or u.username if u else '', 'active_adventurer_count': len(advs), 'adventurers': [{'id': adv.id, 'name': adv.name, 'status': adv.status, 'level': adv.level} for adv in advs]})
        same_account_multi.sort(key=lambda row: (-row['active_adventurer_count'], row['user_id']))
        clusters, reg_ip_map, reg_seg_map, login_ip_map, login_seg_map = AdventurerS1Service._cluster_user_ids_by_ip(user_ids, user_map, segment_bits)
        groups = []
        user_group_map = {}
        group_id = 0
        for _root, members in sorted(clusters.items(), key=lambda x: (-len(x[1]), min(x[1]))):
            if len(members) < 2:
                continue
            group_id += 1
            members_sorted = sorted(members)
            link_summary, link_types = AdventurerS1Service._summarize_ip_alt_group(members_sorted, reg_ip_map, reg_seg_map, login_ip_map, login_seg_map, register_segment_label='在册冒险者同注册IP段', login_segment_label='在册冒险者同登录IP段')
            group_members = []
            active_adv_count = 0
            for uid in members_sorted:
                u = user_map.get(uid)
                advs = adv_by_owner.get(uid) or []
                if not u or not advs:
                    continue
                user_group_map[uid] = group_id
                active_adv_count += len(advs)
                primary = advs[0]
                res = res_by_user.get(uid)
                group_members.append({'user_id': uid, 'username': u.username, 'nickname': u.nickname or u.username, 'register_ip': u.register_ip, 'last_login_ip': u.last_login_ip, 'is_frozen': bool(u.is_frozen), 'active_adventurer_count': len(advs), 'adventurer_id': primary.id, 'adventurer_name': primary.name, 'adventurer_status': primary.status, 'adventurer_level': primary.level, 'adventurers': [{'id': adv.id, 'name': adv.name, 'status': adv.status, 'level': adv.level} for adv in advs], 'reservation_status': res.status if res else None, 'reservation_drawn': bool(res and res.status == 'drawn')})
            if len(group_members) >= 2:
                groups.append({'group_id': group_id, 'member_count': len(group_members), 'active_adventurer_count': active_adv_count, 'link_summary': link_summary, 'link_types': link_types, 'members': group_members})
        flagged_count = len(user_group_map)
        return ({'groups': groups, 'user_group_map': user_group_map, 'same_account_multi': same_account_multi, 'total_owners': len(user_ids), 'total_adventurers': len(adv_rows), 'group_count': len(groups), 'flagged_count': flagged_count}, None)

    @staticmethod
    def set_priority(reservation_id: int, priority: str) -> Tuple[Optional[dict], Optional[str]]:
        """管理员设置预约优先级。"""
        if priority not in _RESERVATION_PRIORITIES:
            return (None, '优先级无效，可选：high / elevated / normal / low')
        r = AdventurerS1Reservation.query.session.get(AdventurerS1Reservation, reservation_id)
        if not r:
            return (None, '预约不存在')
        if r.status != 'pending':
            return (None, f'该预约状态为 {r.status}，无法修改优先级')
        try:
            with db_manager.transaction():
                r = S1RealmRepository.lock_reservation(int(r.id))
                if r is None or r.status != 'pending':
                    raise RuntimeError('预约状态已经变化，请刷新页面')
                r.priority = priority
                if priority != _RESERVATION_PRIORITY_ELEVATED:
                    r.priority_boost_until = None
        except Exception as e:
            return (None, f'操作失败：{e}')
        return (r.to_dict(), None)

    @staticmethod
    def reject_reservation(reservation_id: int, reason: str='') -> Tuple[Optional[dict], Optional[str]]:
        """管理员拒绝预约。"""
        r = AdventurerS1Reservation.query.session.get(AdventurerS1Reservation, reservation_id)
        if not r:
            return (None, '预约不存在')
        if r.status != 'pending':
            return (None, f'该预约状态为 {r.status}，无法拒绝')
        try:
            with db_manager.transaction():
                r = S1RealmRepository.lock_reservation(int(r.id))
                if r is None or r.status != 'pending':
                    raise RuntimeError('预约状态已经变化，请刷新页面')
                r.status = 'rejected'
                r.reject_reason = reason or '管理员拒绝'
                if r.restore_adventurer_id:
                    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import AdventurerS1ArchiveAppealService
                    AdventurerS1ArchiveAppealService.mark_cancelled_for_reservation(r.id)
        except Exception as e:
            return (None, f'操作失败：{e}')
        return (r.to_dict(), None)

    @staticmethod
    def restore_reservation(reservation_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员恢复误拒绝的预约，重新放入抽取队列。"""
        r = AdventurerS1Reservation.query.session.get(AdventurerS1Reservation, reservation_id)
        if not r:
            return (None, '预约不存在')
        if r.status != 'rejected':
            return (None, f'该预约状态为 {r.status}，无法恢复')
        other_pending = AdventurerS1Reservation.query.filter(AdventurerS1Reservation.user_id == r.user_id, AdventurerS1Reservation.status == 'pending', AdventurerS1Reservation.id != r.id).first()
        if other_pending:
            return (None, '该玩家已有其他等待中的预约，无法恢复')
        if r.restore_adventurer_id:
            adv = AdventurerS1.query.session.get(AdventurerS1, r.restore_adventurer_id)
            if not adv:
                return (None, '关联的封存冒险者不存在，无法恢复解封排队')
            if adv.owner_id != r.user_id:
                return (None, '冒险者归属与预约玩家不一致')
            if adv.status != 'archived':
                return (None, f'关联冒险者状态为 {adv.status}，无法恢复解封排队')
        try:
            with db_manager.transaction():
                if S1RealmRepository.lock_user(r.user_id) is None:
                    raise RuntimeError('玩家不存在')
                r = S1RealmRepository.lock_reservation(int(r.id))
                if r is None or r.status != 'rejected':
                    raise RuntimeError('预约已不在可恢复状态')
                if S1RealmRepository.find_pending_reservation(r.user_id, exclude_reservation_id=r.id):
                    raise RuntimeError('该玩家已有其他等待中的预约，无法恢复')
                S1RealmService.require_free_active_slot(r.user_id)
                if r.restore_adventurer_id:
                    adv = S1RealmRepository.lock_adventurer(r.restore_adventurer_id)
                    if not adv or adv.owner_id != r.user_id or adv.status != 'archived':
                        raise RuntimeError('关联的封存冒险者状态或归属已变化')
                    if S1RealmService._validate_realm_code(adv.realm_code) != S1RealmService._validate_realm_code(r.realm_code):
                        raise RuntimeError('解封预约与封存冒险者区服不一致')
                r.status = 'pending'
                r.reject_reason = None
                if r.restore_adventurer_id:
                    from plugins.adventurer_s1.services.adventurer_s1_archive_appeal_service import AdventurerS1ArchiveAppealService
                    ok, appeal_err = AdventurerS1ArchiveAppealService.restore_approved_for_reservation(r)
                    if not ok:
                        raise RuntimeError(appeal_err or '恢复解封申请失败')
        except Exception as e:
            return (None, f'操作失败：{e}')
        return (r.to_dict(), None)

    @staticmethod
    def _get_draw_online_cutoff() -> datetime:
        """预约抽取：申请者须在此时间点之后登录或手动操作过预约。"""
        hours = int(current_app.config.get('ADVENTURER_S1_DRAW_ONLINE_HOURS', 24))
        return _portable_utc_now() - timedelta(hours=hours)

    @staticmethod
    def _has_active_player_priority_boost(r: 'AdventurerS1Reservation', *, now: datetime=None) -> bool:
        """玩家手动「临时提高优先级」仍在 6 小时有效期内。"""
        now = now or _portable_utc_now()
        return r.priority == _RESERVATION_PRIORITY_ELEVATED and r.priority_boost_until is not None and (r.priority_boost_until > now)

    @staticmethod
    def _is_reservation_draw_eligible(r: 'AdventurerS1Reservation', last_login_at, *, now: datetime=None, cutoff: datetime=None) -> bool:
        """抽取资格：窗口内登录或手动操作过预约；有效临时提升继续兼容放行。"""
        now = now or _portable_utc_now()
        cutoff = cutoff if cutoff is not None else AdventurerS1Service._get_draw_online_cutoff()
        if last_login_at and last_login_at >= cutoff:
            return True
        if r.last_owner_action_at and r.last_owner_action_at >= cutoff:
            return True
        return AdventurerS1Service._has_active_player_priority_boost(r, now=now)

    @staticmethod
    def _filter_reservations_by_recent_login(reservations) -> Tuple[List, int]:
        """按登录或预约手动操作过滤活跃预约。返回 (eligible, skipped_inactive_count)。"""
        if not reservations:
            return ([], 0)
        from models.user import User
        user_ids = {r.user_id for r in reservations}
        cutoff = AdventurerS1Service._get_draw_online_cutoff()
        now = _portable_utc_now()
        login_map = dict(User.query.with_entities(User.id, User.last_login_at).filter(User.id.in_(user_ids), User.is_deleted == False).all())
        eligible = [r for r in reservations if AdventurerS1Service._is_reservation_draw_eligible(r, login_map.get(r.user_id), now=now, cutoff=cutoff)]
        return (eligible, len(reservations) - len(eligible))

    @staticmethod
    def _select_draw_reservations(priority_groups: List[list], count: int, ordinary_bed_count: int) -> list:
        remaining = min(max(0, int(count)), max(0, int(ordinary_bed_count)))
        drawn = []
        for group in priority_groups:
            for reservation in group:
                if len(drawn) >= remaining:
                    return drawn
                drawn.append(reservation)
        return drawn

    @staticmethod
    def draw_adventurers(count: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员抽取 N 名冒险者：从 pending 预约中按优先级选取并创建。

        抽取资格：申请者须在 ADVENTURER_S1_DRAW_ONLINE_HOURS（默认 24）内登录过，
        或玩家「临时提高优先级」仍在 ADVENTURER_S1_RESERVATION_BOOST_HOURS（默认 6）有效期内。

        优先级规则：
        - high：必定抽取（优先保证）
        - elevated：在 high 全部抽中后、normal 之前随机抽取（含玩家临时提升）
        - normal：在 high、elevated 全部抽中后，从 normal 中随机抽取
        - low：同一床位池内更高优先级均已满足后才抽取；另一独立池已满不互相阻塞
        同一优先级内部随机打乱；确定整批抽中名单后，再随机打乱批内入场顺序。
        """
        if count < 1:
            return (None, '抽取数量至少为 1')
        _priority_result, priority_err = AdventurerS1Service.auto_lower_alt_reservation_priorities()
        if priority_err:
            return (None, priority_err)
        bed_draw_lock = _try_acquire_bed_draw_lock()
        if bed_draw_lock is None:
            return (None, '已有抽取任务进行中，请稍后再试')
        try:
            pending_all = AdventurerS1Reservation.query.filter_by(status='pending').all()
            if not pending_all:
                return (None, '没有待抽取的预约')
            AdventurerS1Service._expire_pending_reservation_boosts(pending_all)
            pending, skipped_inactive = AdventurerS1Service._filter_reservations_by_recent_login(pending_all)
            skipped_waiting_npc_in_story = 0
            if not pending:
                login_hours = int(current_app.config.get('ADVENTURER_S1_DRAW_ONLINE_HOURS', 24))
                boost_hours = int(current_app.config.get('ADVENTURER_S1_RESERVATION_BOOST_HOURS', 6))
                return (None, f'没有符合抽取条件的预约（申请者需 {login_hours} 小时内上线，或 {boost_hours} 小时内手动临时提升仍有效）')
            free_beds = AdventurerS1Service._collect_free_bed_slots()
            random.shuffle(free_beds)
            AdventurerS1Service._fill_homeless_adventurers(free_beds)
            high = [r for r in pending if r.priority == _RESERVATION_PRIORITY_HIGH]
            elevated = [r for r in pending if r.priority == _RESERVATION_PRIORITY_ELEVATED]
            normal = [r for r in pending if r.priority == _RESERVATION_PRIORITY_NORMAL]
            low = [r for r in pending if r.priority == _RESERVATION_PRIORITY_LOW]
            random.shuffle(high)
            random.shuffle(elevated)
            random.shuffle(normal)
            random.shuffle(low)
            drawn = AdventurerS1Service._select_draw_reservations([high, elevated, normal, low], count=count, ordinary_bed_count=len(free_beds))
            if not drawn:
                return (None, '没有与待抽取预约匹配的空床位，无法抽取')
            random.shuffle(drawn)
            created_list = []
            errors = []
            if drawn:
                from sqlalchemy import func
                from extensions import db
                drawn_user_ids = {r.user_id for r in drawn}
                drawn_skill_ids = {r.skill_template_id for r in drawn}
                drawn_item_ids = {iid for r in drawn for iid in r.item_ids or []}
                owned_count_map = dict(db.session.query(AdventurerS1.owner_id, func.count(AdventurerS1.id)).filter(AdventurerS1.owner_id.in_(drawn_user_ids), AdventurerS1.is_player_created == True, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).group_by(AdventurerS1.owner_id).all())
                tpl_map = {t.id: t for t in AdventurerS1SkillTemplate.query.filter(AdventurerS1SkillTemplate.id.in_(drawn_skill_ids), AdventurerS1SkillTemplate.is_active == True).all()}
                item_map = {i.id: i for i in AdventurerS1Item.query.filter(AdventurerS1Item.id.in_(drawn_item_ids), AdventurerS1Item.is_starter == True).all()}
                drawn_names = {r.name for r in drawn}
                drawn_ids = {r.id for r in drawn}
                existing_names = {name for name, in AdventurerS1.query.with_entities(AdventurerS1.name).filter(AdventurerS1.name.in_(drawn_names)).all()}
                reserved_names = {name for name, in AdventurerS1Reservation.query.with_entities(AdventurerS1Reservation.name).filter(AdventurerS1Reservation.status == 'pending', AdventurerS1Reservation.name.in_(drawn_names), AdventurerS1Reservation.id.notin_(drawn_ids)).all()}
                world_entity_names = AdventurerS1Service._reserved_world_entity_names(drawn_names)
                prevalidated_name_ids = set()
                seen_names = set()
                for r in drawn:
                    if r.name not in existing_names and r.name not in reserved_names and (r.name not in world_entity_names) and (r.name not in seen_names):
                        prevalidated_name_ids.add(r.id)
                    seen_names.add(r.name)
                from plugins.adventurer_s1.services.s1_item_use_condition_service import build_clue_attach_meta
                catalog_desc = {item.name: item.description or '' for item in item_map.values()}
                catalog_clue_meta = {}
                for item in item_map.values():
                    clue_fields = build_clue_attach_meta(item)
                    if clue_fields:
                        catalog_clue_meta[item.name] = clue_fields
                catalog_context = (catalog_desc, catalog_clue_meta)
                for r in drawn:
                    adv, err = AdventurerS1Service._create_adventurer_from_reservation(r, owned_count_map=owned_count_map, tpl_map=tpl_map, item_map=item_map, free_beds=free_beds, name_prevalidated=r.id in prevalidated_name_ids, catalog_context=catalog_context)
                    if err:
                        errors.append(f'预约#{r.id}({r.name})创建失败：{err}')
                    else:
                        created_list.append(adv)
                        owned_count_map[r.user_id] = owned_count_map.get(r.user_id, 0) + 1
            if created_list:
                invalidate_dormitory_cache()
            return ({'drawn_count': len(created_list), 'temporary_name_count': sum((1 for adv in created_list if adv.get('initial_rename_available'))), 'total_pending': len(pending_all), 'eligible_pending': len(pending), 'skipped_inactive': skipped_inactive, 'skipped_waiting_npc_in_story': skipped_waiting_npc_in_story, 'created': created_list, 'errors': errors}, None)
        finally:
            _release_bed_draw_lock(bed_draw_lock)

    @staticmethod
    def _maybe_auto_draw_after_dormitory_built() -> None:
        """宿舍完工自动抽取；数量只取普通宿舍当前空床数。"""
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        if not S1SettingsService.get('auto_draw_on_dormitory_complete'):
            return
        empty_beds = len(AdventurerS1Service._collect_free_bed_slots())
        if empty_beds < 1:
            return
        try:
            result, err = AdventurerS1Service.draw_adventurers(empty_beds)
            if err:
                logger.warning('宿舍完工自动抽取失败：%s', err)
            elif result and result.get('errors'):
                logger.warning('宿舍完工自动抽取部分失败：%s', result['errors'])
        except Exception:
            logger.exception('宿舍完工自动抽取异常（empty_beds=%s）', empty_beds)

    @staticmethod
    def run_daily_reservation_draw_job() -> dict:
        """每天 20:00 按普通空床数执行预约抽取。"""
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        if not S1SettingsService.get('daily_reservation_draw_enabled'):
            return {'enabled': False, 'drawn_count': 0, 'skipped': 'disabled'}
        empty_beds = len(AdventurerS1Service._collect_free_bed_slots())
        if empty_beds < 1:
            return {'enabled': True, 'drawn_count': 0, 'skipped': 'no_empty_beds'}
        try:
            result, err = AdventurerS1Service.draw_adventurers(empty_beds)
            ok, commit_err = db_manager.commit()
            if not ok:
                raise RuntimeError(commit_err or '每日预约抽取提交失败')
            if err:
                logger.info('每日 20:00 预约自动抽取跳过：%s', err)
                return {'enabled': True, 'drawn_count': 0, 'skipped': 'draw_unavailable', 'message': err}
            result = result or {}
            if result.get('errors'):
                logger.warning('每日 20:00 预约自动抽取部分失败：%s', result['errors'])
            return {'enabled': True, **result}
        except Exception:
            db.session.rollback()
            logger.exception('每日 20:00 预约自动抽取异常（empty_beds=%s）', empty_beds)
            return {'enabled': True, 'drawn_count': 0, 'skipped': 'error'}

    @staticmethod
    def _restore_adventurer_from_reservation(r: 'AdventurerS1Reservation', free_beds: Optional[list]=None) -> Tuple[Optional[dict], Optional[str]]:
        """从解封排队预约恢复封存冒险者（抽中后调用）。"""
        from plugins.adventurer_s1.services.adventurer_s1_admin_service import AdventurerS1AdminService
        adv_id = r.restore_adventurer_id
        adv = AdventurerS1.query.session.get(AdventurerS1, adv_id) if adv_id else None
        if not adv:
            try:
                with db_manager.transaction():
                    r.status = 'rejected'
                    r.reject_reason = '关联的封存冒险者不存在'
            except Exception:
                pass
            return (None, '关联的封存冒险者不存在')
        if adv.owner_id != r.user_id:
            try:
                with db_manager.transaction():
                    r.status = 'rejected'
                    r.reject_reason = '冒险者归属与预约玩家不一致'
            except Exception:
                pass
            return (None, '冒险者归属与预约玩家不一致')
        if adv.status != 'archived':
            try:
                with db_manager.transaction():
                    r.status = 'rejected'
                    r.reject_reason = '冒险者已不是封存状态'
            except Exception:
                pass
            return (None, '冒险者已不是封存状态')
        if free_beds is not None:
            if len(free_beds) < 1:
                return (None, '没有普通空床位，无法解封')
        result, err = AdventurerS1AdminService.admin_unarchive_adventurer(adv_id, admin_user_id=0, reservation_id=r.id)
        if err:
            return (None, err)
        if free_beds is not None:
            adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
            if adv and adv.dorm_room_id is not None and (adv.bed_index is not None):
                AdventurerS1Service._remove_bed_from_pool(free_beds, adv.dorm_room_id, adv.bed_index)
        AdventurerS1Service._notify_reservation_drawn(user_id=r.user_id, adventurer_name=adv.name, adventurer_id=adv_id, reservation_id=r.id, is_unarchive=True)
        adv = db_manager.get(AdventurerS1, adv_id)
        item_dict, clue_meta = AdventurerS1Service._build_item_catalog_context(adv)
        out = adv.to_dict(item_dict=item_dict, item_clue_meta=clue_meta, current_user_id=r.user_id, is_admin=False)
        if result and result.get('message'):
            out['unarchive_message'] = result['message']
        return (out, None)

    @staticmethod
    def _create_adventurer_from_reservation(r: 'AdventurerS1Reservation', owned_count_map: Optional[dict]=None, tpl_map: Optional[dict]=None, item_map: Optional[dict]=None, free_beds: Optional[list]=None, name_prevalidated: bool=False, catalog_context: Optional[tuple[dict, dict]]=None) -> Tuple[Optional[dict], Optional[str]]:
        """从预约记录创建冒险者（内部方法）。

        批量场景（draw_adventurers）预取以下依赖传入以避免循环查询：
          - owned_count_map: {user_id: 已拥有数}
          - tpl_map: {skill_template_id: SkillTemplate}
          - item_map: {item_id: AdventurerS1Item}
          - free_beds: 共享的剩余空床位列表 [(room_id, bed_index), ...]，分一个 pop 一个
          - name_prevalidated: 姓名已由批量入口校验，无需再执行两次逐条查询
          - catalog_context: 全批复用的初始装备图鉴描述与线索字段
        未传时回退到独立查询，单条创建路径仍可用。
        """
        if r.restore_adventurer_id:
            return AdventurerS1Service._restore_adventurer_from_reservation(r, free_beds=free_beds)
        max_per = int(current_app.config.get('ADVENTURER_S1_MAX_PER_PLAYER', 1))
        if owned_count_map is not None:
            owned = owned_count_map.get(r.user_id, 0)
        else:
            owned = AdventurerS1Service.count_tavern_active_owned(r.user_id)
        if owned >= max_per:
            try:
                with db_manager.transaction():
                    r.status = 'rejected'
                    r.reject_reason = f'该玩家已拥有 {max_per} 个冒险者'
            except Exception:
                pass
            return (None, f'该玩家已拥有 {max_per} 个冒险者')
        if tpl_map is not None:
            tpl = tpl_map.get(r.skill_template_id)
        else:
            tpl = AdventurerS1SkillTemplate.query.session.get(AdventurerS1SkillTemplate, r.skill_template_id)
            if tpl and (not tpl.is_active):
                tpl = None
        if not tpl:
            try:
                with db_manager.transaction():
                    r.status = 'rejected'
                    r.reject_reason = '所选技能模板已失效'
            except Exception:
                pass
            return (None, '所选技能模板已失效')
        if item_map is not None:
            item_by_id = {iid: item_map[iid] for iid in r.item_ids or [] if iid in item_map}
        else:
            starter_items = AdventurerS1Item.query.filter(AdventurerS1Item.id.in_(r.item_ids), AdventurerS1Item.is_starter == True).all()
            item_by_id = {i.id: i for i in starter_items}
        if len(item_by_id) != 2:
            try:
                with db_manager.transaction():
                    r.status = 'rejected'
                    r.reject_reason = '所选装备已失效'
            except Exception:
                pass
            return (None, '所选装备已失效')
        dup_err = None if name_prevalidated else AdventurerS1Service.assert_adventurer_name_available(r.name, exclude_reservation_id=r.id)
        requested_name = r.name
        initial_rename_available = bool(dup_err)
        adventurer_name = f'未命名{int(r.id)}' if initial_rename_available else requested_name
        attr_min = int(current_app.config.get('ADVENTURER_S1_CREATE_ATTR_MIN', 1))
        attr_max = int(current_app.config.get('ADVENTURER_S1_CREATE_ATTR_MAX', 20))
        hp_min = int(current_app.config.get('ADVENTURER_S1_CREATE_HP_MIN', 60))
        hp_max = int(current_app.config.get('ADVENTURER_S1_CREATE_HP_MAX', 200))
        initial_gold = round(float(current_app.config.get('ADVENTURER_S1_INITIAL_GOLD', 0.0)), 2)
        hp = random.randint(hp_min, hp_max)
        reservation_id = int(r.id)
        owner_user_id = int(r.user_id)
        runtime_realm = S1RealmService.current_runtime_realm()
        try:
            reservation_realm = S1RealmService._validate_realm_code(r.realm_code)
        except S1RealmAssignmentError as exc:
            return (None, f'创建失败：{exc}')
        if reservation_realm != runtime_realm:
            return (None, '创建失败：预约不属于当前区服')
        remote_claim_token = None
        if runtime_realm == REALM_2:
            db_manager.remove()
            try:
                remote_claim_token = S1RealmService.claim_remote_active_slot(owner_user_id, reservation_id=reservation_id)
            except S1RealmActiveSlotError as exc:
                try:
                    with db_manager.transaction():
                        rejected = S1RealmRepository.lock_reservation(reservation_id)
                        if rejected is not None and rejected.status == 'pending':
                            rejected.status = 'rejected'
                            rejected.reject_reason = '另一区已生成存活冒险者，本区预约自动失效'
                except Exception:
                    logger.warning('二区全局槽位冲突后标记预约失败 reservation=%s', reservation_id, exc_info=True)
                return (None, f'创建失败：{exc}')
            except S1RealmAssignmentError as exc:
                return (None, f'创建失败：{exc}')
        new_id = None
        local_claim_token = None
        try:
            with db_manager.transaction():
                membership = None
                if runtime_realm == REALM_1:
                    membership = S1RealmService.claim_active_slot(owner_user_id, realm_code=runtime_realm, reservation_id=reservation_id)
                    local_claim_token = str(membership.claim_token or '')
                r = S1RealmRepository.lock_reservation(reservation_id)
                if r is None:
                    raise S1PlayerVisibleError('预约不存在')
                if r.status != 'pending':
                    raise S1PlayerVisibleError('预约已不在待抽取状态')
                if S1RealmService._validate_realm_code(r.realm_code) != runtime_realm:
                    raise S1PlayerVisibleError('预约不属于当前区服')
                S1RealmService.require_free_active_slot(r.user_id)
                from plugins.adventurer_s1.services.s1_item_catalog_lock_service import CATALOG_BUSY_ERROR, lock_catalog_names
                starter_names = {item.name for item in item_by_id.values()}
                if not lock_catalog_names(starter_names, shared=True, wait=False):
                    raise S1PlayerVisibleError(CATALOG_BUSY_ERROR)
                refreshed_items = AdventurerS1Item.query.filter(AdventurerS1Item.id.in_(r.item_ids), AdventurerS1Item.is_starter == True).populate_existing().all()
                refreshed_names = {item.name for item in refreshed_items}
                if len(refreshed_items) != 2 or not lock_catalog_names(starter_names | refreshed_names, shared=True, wait=False):
                    raise S1PlayerVisibleError(CATALOG_BUSY_ERROR)
                item_by_id = {item.id: item for item in refreshed_items}
                create_now = _portable_utc_now()
                adv = AdventurerS1(name=adventurer_name, gender=r.gender, personality=r.personality, character_class=None, is_player_created=True, owner_id=r.user_id, realm_code=runtime_realm, initial_rename_available=initial_rename_available, level=1, exp=0, hp=hp, max_hp=hp, strength=random.randint(attr_min, attr_max), agility=random.randint(attr_min, attr_max), intelligence=random.randint(attr_min, attr_max), luck=random.randint(attr_min, attr_max), gold=initial_gold, status='resting', party_preference='manual', last_owner_action_at=create_now)
                ok, err = db_manager.add(adv)
                if not ok:
                    raise RuntimeError(err)
                eq_list = [AdventurerS1Skill(adventurer_id=adv.id, name=tpl.name, description=tpl.description)]
                for iid in r.item_ids:
                    it = item_by_id[iid]
                    eq_list.append(AdventurerS1Equipment(adventurer_id=adv.id, name=it.name, type=it.type, rarity=it.rarity, custom_desc=None, recovery_minutes=max(0, int(it.recovery_minutes or 0))))
                ok, err = db_manager.add_all(eq_list)
                if not ok:
                    raise RuntimeError(err)
                if not AdventurerS1Service._assign_random_bed(adv, free_beds=free_beds):
                    raise S1PlayerVisibleError('没有符合该玩家房间权限的空床位')
                if adv.rest_start_time is None:
                    adv.rest_start_time = _portable_utc_now()
                from plugins.adventurer_s1.services.s1_waiting_player_npc_service import apply_waiting_npc_origin_to_adventurer, materialize_waiting_npc_actor_without_inheritance
                origin_savepoint = db_manager.begin_nested()
                try:
                    apply_waiting_npc_origin_to_adventurer(int(r.id), adv, existing_equipment_names={item_by_id[iid].name for iid in r.item_ids}, existing_skill_names={tpl.name})
                    origin_savepoint.commit()
                except Exception as origin_exc:
                    origin_savepoint.rollback()
                    current_app.logger.warning('S1 正式抽取已忽略 NPC 来源继承失败 reservation=%s: %s', r.id, origin_exc)
                    seal_savepoint = db_manager.begin_nested()
                    try:
                        materialize_waiting_npc_actor_without_inheritance(int(r.id), int(adv.id))
                        seal_savepoint.commit()
                    except Exception as seal_exc:
                        seal_savepoint.rollback()
                        current_app.logger.warning('S1 正式抽取已忽略 NPC actor 封存失败 reservation=%s: %s', r.id, seal_exc)
                r.status = 'drawn'
                r.name = adventurer_name
                r.reject_reason = dup_err
                r.priority_boost_until = None
                new_id = adv.id
                if membership is not None:
                    S1RealmService.finalize_active_slot(owner_user_id, realm_code=runtime_realm, reservation_id=reservation_id, claim_token=local_claim_token, adventurer_id=new_id)
        except Exception as e:
            original_error = getattr(e, 'original_error', e)
            if remote_claim_token:
                try:
                    S1RealmService.abort_remote_active_slot_claim(owner_user_id, reservation_id=reservation_id, claim_token=remote_claim_token)
                except Exception:
                    logger.warning('二区创建失败后释放中央 pending 槽位失败 reservation=%s', reservation_id, exc_info=True)
            if isinstance(original_error, S1RealmActiveSlotError):
                try:
                    with db_manager.transaction():
                        rejected = S1RealmRepository.lock_reservation(reservation_id)
                        if rejected is not None and rejected.status == 'pending':
                            rejected.status = 'rejected'
                            rejected.reject_reason = '另一区已生成存活冒险者，本区预约自动失效'
                except Exception:
                    logger.warning('全局槽位冲突后标记预约失败 reservation=%s', reservation_id, exc_info=True)
            return (None, player_operation_failure(e, '创建失败'))
        if remote_claim_token:
            try:
                S1RealmService.finalize_remote_active_slot(owner_user_id, reservation_id=reservation_id, claim_token=remote_claim_token, adventurer_id=new_id)
            except Exception:
                logger.exception('二区角色已创建但中央槽位 finalize 暂时失败 reservation=%s adv=%s', reservation_id, new_id)
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        _, currency_err = S1CurrencyService.init_currency(new_id)
        if currency_err:
            current_app.logger.warning(f'初始化冒险者货币失败: {currency_err}')
        adv = db_manager.get(AdventurerS1, new_id)
        AdventurerS1Service._notify_reservation_drawn(user_id=r.user_id, adventurer_name=adventurer_name, adventurer_id=new_id, reservation_id=r.id, requested_name=requested_name if initial_rename_available else None, temporary_name_reason=dup_err)
        if catalog_context is None:
            item_dict, clue_meta = AdventurerS1Service._build_item_catalog_context(adv)
        else:
            item_dict, clue_meta = catalog_context
        return (adv.to_dict(item_dict=item_dict, item_clue_meta=clue_meta, current_user_id=r.user_id, is_admin=False), None)

    @staticmethod
    def _notify_reservation_drawn(user_id: int, adventurer_name: str, adventurer_id: int, reservation_id: int, *, is_unarchive: bool=False, requested_name: Optional[str]=None, temporary_name_reason: Optional[str]=None) -> None:
        """预约抽中后向玩家发送邮箱通知（失败不影响创建/解封流程）。"""
        try:
            from services.mailbox_service import MailboxService
            if is_unarchive:
                title = '深渊酒馆 · 解封抽中'
                content = f'恭喜！你的解封申请已被管理员抽取，冒险者「{adventurer_name}」已恢复酒馆编制。\n\n请前往深渊酒馆继续你的探险之旅！'
            elif requested_name and temporary_name_reason:
                title = '深渊酒馆 · 已入驻，请为冒险者命名'
                content = f'恭喜！你的冒险者预约「{requested_name}」已经被抽中。由于该名字目前无法使用，酒馆先以「{adventurer_name}」为他完成登记并安排了床位。\n\n冒险者已经正常进入酒馆，不会浪费本次抽中机会。请前往“我的冒险者”，点击“命名一次”按钮提交新名字；无效名字不会消耗机会，首次成功后不可再次改名。'
            else:
                title = '深渊酒馆 · 预约抽中'
                content = f'恭喜！你提交的冒险者「{adventurer_name}」已被管理员抽取，冒险者已成功创建。\n\n请前往深渊酒馆查看你的冒险者，开始你的探险之旅！'
            MailboxService.send_mail(user_id=user_id, title=title, content=content, sender_id=None, sender_name='深渊酒馆', mail_type='adventurer_s1_draw', reference_id=adventurer_id)
        except Exception as e:
            current_app.logger.warning('S1 预约抽中邮件发送失败 user_id=%s reservation_id=%s: %s', user_id, reservation_id, e)

    @staticmethod
    def _assign_random_bed(adv: 'AdventurerS1', free_beds: Optional[list]=None) -> bool:
        """按玩家身份从严格隔离的床位池分配空床，返回是否成功。

        free_beds 是批量创建场景传入的宿舍共享池；未传时扫描普通空床并随机挑选。
        须在 transaction 内调用（批量抽取）或自行包短事务（独立补位）。
        """
        if free_beds is not None:
            candidate_beds = free_beds
            while candidate_beds:
                room_id, bed_idx = candidate_beds.pop()
                if AdventurerS1Service._claim_bed_slot(adv, room_id, bed_idx):
                    return True
            return False
        empty = AdventurerS1Service._collect_free_bed_slots()
        random.shuffle(empty)
        adv_id = adv.id
        for room_id, bed_idx in empty:
            try:
                with db_manager.transaction():
                    locked = adv
                    if adv_id is not None:
                        locked = AdventurerS1.query.filter_by(id=adv_id).with_for_update().first()
                        if not locked:
                            return False
                    if AdventurerS1Service._claim_bed_slot(locked, room_id, bed_idx):
                        adv.dorm_room_id = locked.dorm_room_id
                        adv.bed_index = locked.bed_index
                        return True
            except Exception:
                logger.exception('独立补位占床失败 adv_id=%s room=%s bed=%s', adv_id, room_id, bed_idx)
        return False

    @staticmethod
    def _adventurer_list_query_options():
        """列表卡片不需要的子表：跳过 selectin 以减轻 init / list SQL。"""
        from sqlalchemy.orm import defer, joinedload, noload
        return (defer(AdventurerS1.history_story), noload(AdventurerS1.skill_items), noload(AdventurerS1.investor_items), noload(AdventurerS1.materials), noload(AdventurerS1.warehouse_items), noload(AdventurerS1.building_instance), joinedload(AdventurerS1.dorm_room).load_only(AdventurerS1DormRoom.room_key).noload(AdventurerS1DormRoom.occupants), noload(AdventurerS1.story_threads))

    @staticmethod
    def _adventurer_card_query_options():
        """Shared list-only projection; detached cards must not lazy-load details."""
        from sqlalchemy.orm import defer, selectinload
        return (*AdventurerS1Service._adventurer_list_query_options(), selectinload(AdventurerS1.traits.and_(AdventurerS1Trait.is_archived.is_(False))).load_only(AdventurerS1Trait.id, AdventurerS1Trait.name, AdventurerS1Trait.is_archived, AdventurerS1Trait.equipped, raiseload=True), selectinload(AdventurerS1.equipment_items).load_only(AdventurerS1Equipment.id, AdventurerS1Equipment.name, AdventurerS1Equipment.rarity, AdventurerS1Equipment.type, AdventurerS1Equipment.evolution_stage, AdventurerS1Equipment.quantity, AdventurerS1Equipment.recovery_minutes, AdventurerS1Equipment.recovery_ends_at, raiseload=True), defer(AdventurerS1.history_story), defer(AdventurerS1.closure_resolution), defer(AdventurerS1.exp), defer(AdventurerS1.closure_choice), defer(AdventurerS1.closure_choice_at), defer(AdventurerS1.recovery_time), defer(AdventurerS1.rest_start_time), defer(AdventurerS1.trait_recovery_rest_seconds), defer(AdventurerS1.archived_by_user_id), defer(AdventurerS1.archive_reason), defer(AdventurerS1.last_owner_action_at), defer(AdventurerS1.market_extra_slots), defer(AdventurerS1.market_extra_slots_expires_at), defer(AdventurerS1.last_bed_swap_at), defer(AdventurerS1.building_usage_extra))

    @staticmethod
    def _dorm_recovery_paused_adventurer_ids(adventurers, *, session=None) -> set[int]:
        """批量找出床位所属宿舍正在升级的冒险者，供前端冻结惰性显示。"""
        occupants = [adv for adv in adventurers if adv.dorm_room_id is not None and adv.bed_index is not None]
        room_ids = {adv.dorm_room_id for adv in occupants}
        if not room_ids:
            return set()
        upgrading_room_ids = {row[0] for row in (session if session is not None else db.session).query(AdventurerS1DormRoom.id).join(AdventurerS1BuildingInstance, AdventurerS1BuildingInstance.id == AdventurerS1DormRoom.building_instance_id).filter(AdventurerS1DormRoom.id.in_(room_ids), AdventurerS1BuildingInstance.status == 'upgrading').all()}
        return {adv.id for adv in occupants if adv.dorm_room_id in upgrading_room_ids}

    @staticmethod
    def ensure_dorm_room_upgrade_instances_job() -> dict:
        """Scheduler：为已解锁宿舍升级但缺 building_instance 的房间建占位实例。"""
        from sqlalchemy.orm import load_only, raiseload
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import AdventurerS1TavernService
        progress, err = AdventurerS1TavernService.get_tavern_upgrade_progress()
        if err or not progress.get('building_upgrade_unlocked'):
            return {'ensured': 0, 'errors': 0}
        candidates = AdventurerS1DormRoom.query.options(load_only(AdventurerS1DormRoom.id, AdventurerS1DormRoom.name, AdventurerS1DormRoom.capacity, AdventurerS1DormRoom.level, AdventurerS1DormRoom.building_instance_id, AdventurerS1DormRoom.room_key), raiseload('*')).filter(or_(AdventurerS1DormRoom.level < 2, AdventurerS1DormRoom.level.is_(None)), AdventurerS1DormRoom.building_instance_id.is_(None), AdventurerS1DormRoom.room_key.is_(None)).order_by(AdventurerS1DormRoom.id).all()
        if not candidates:
            return {'ensured': 0, 'errors': 0}
        ensured, errors = AdventurerS1Service._batch_ensure_dorm_room_building_instances(candidates)
        if ensured:
            invalidate_dormitory_cache()
        return {'ensured': len(ensured), 'errors': len(errors)}

    @staticmethod
    def _build_dormitory_page_payload(rooms: list[AdventurerS1DormRoom]) -> dict:
        """构建当前页宿舍共享数据（不含用户排序标记和个人升级捐献）。"""
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import AdventurerS1TavernService
        progress, err = AdventurerS1TavernService.get_tavern_upgrade_progress()
        if err:
            raise RuntimeError(err)
        building_upgrade_unlocked = bool(progress.get('building_upgrade_unlocked'))
        occupants = [adv for room in rooms for adv in room.occupants]
        occupant_ids = [adv.id for adv in occupants]
        seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(occupant_ids)
        building_info_map = AdventurerS1Service.build_adventurer_building_info_map(occupants)
        room_inst_map: dict[int, AdventurerS1BuildingInstance] = {}
        for room in rooms:
            inst = room.building_instance
            if not inst and room.building_instance_id:
                inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, room.building_instance_id)
            if inst:
                room_inst_map[room.id] = inst
        upgrade_inst_ids = [inst.id for inst in room_inst_map.values() if int(getattr(inst, 'upgrade_level', 1) or 1) < 2]
        upgrade_donation_totals = AdventurerS1Service._batch_summarize_donation_totals(upgrade_inst_ids, _DONATION_PURPOSE_UPGRADE)
        upgrading_inst_ids = [inst.id for inst in room_inst_map.values() if inst.status == 'upgrading']
        helpers_by_inst = AdventurerS1Service._batch_building_helpers_by_inst(upgrading_inst_ids, for_list=True)
        items = []
        for room in rooms:
            beds = [None] * room.capacity
            for adv in room.occupants:
                if adv.bed_index is not None and 0 <= adv.bed_index < room.capacity:
                    building_info = building_info_map.get(adv.id, {})
                    bed = {'adventurer_id': adv.id, 'adventurer_name': adv.name, 'owner_id': adv.owner_id, 'status': adv.status, 'building_instance_status': building_info.get('status')}
                    if adv.id in seated_ids:
                        bed['tavern_seated'] = True
                    beds[adv.bed_index] = bed
            item = room.to_dict(beds=beds)
            upgrade_level = int(item.get('upgrade_level') or 1)
            if building_upgrade_unlocked and upgrade_level < 2:
                inst = room_inst_map.get(room.id)
                if not inst:
                    item['upgrade_error'] = '宿舍升级实例不可用（等待系统同步）'
                else:
                    required = AdventurerS1Service._dorm_upgrade_cost_for_instance(inst)
                    totals = upgrade_donation_totals.get(inst.id, {})
                    donations_complete = AdventurerS1Service._donations_meet_required(required, totals)
                    item['upgrade_level'] = int(getattr(inst, 'upgrade_level', 1) or 1)
                    item['building_status'] = inst.status
                    item['building_instance_id'] = inst.id
                    item['upgrade_required_materials'] = required
                    item['upgrade_donations_summary'] = totals
                    item['upgrade_donations_complete'] = donations_complete
                    item['can_start_upgrade'] = donations_complete and inst.status == 'built'
                    if inst.status == 'upgrading':
                        upgrade_cfg = inst.template.upgrade_config if inst.template else None
                        upgrade_hours = int((upgrade_cfg or {}).get('upgrade_hours', 24))
                        total_seconds = upgrade_hours * 3600
                        from utils.time_utils import to_utc_iso
                        item['upgrade_progress_percent'] = min(100, round(inst.accumulated_progress_seconds / total_seconds * 100)) if total_seconds > 0 else 0
                        item['upgrade_remaining_seconds'] = max(0, total_seconds - inst.accumulated_progress_seconds)
                        item['upgrade_total_seconds'] = total_seconds
                        item['upgrade_complete_at'] = to_utc_iso(inst.upgrade_complete_at) if inst.upgrade_complete_at else None
                        item['upgrade_speedup_minutes'] = int(inst.template.speedup_minutes if inst.template and inst.template.speedup_minutes else 60)
                        helpers = helpers_by_inst.get(inst.id, [])
                        item['upgrade_helpers'] = [{'adventurer_id': h.id, 'adventurer_name': h.name, 'owner_id': h.owner_id} for h in helpers]
                        slots = inst.template.helper_slots if inst.template else 2
                        item['upgrade_helper_slots'] = int(slots or 2)
            items.append(item)
        return {'building_upgrade_unlocked': building_upgrade_unlocked, 'upgrade_inst_ids': upgrade_inst_ids, 'rooms': items}

    @staticmethod
    def get_dormitory(user_id: Optional[int]=None, page: int=1, per_page: int=12) -> Tuple[Optional[dict], Optional[str]]:
        """分页读取宿舍；我的房间优先、关注冒险者所在房间其次。"""
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 12)))
        cache_key = f'{user_id or 0}:{page}:{per_page}'
        cached = _dormitory_cache.get(cache_key)
        if cached is not None:
            return (cached, None)
        from sqlalchemy.orm import joinedload, noload, selectinload
        query = AdventurerS1DormRoom.query.options(joinedload(AdventurerS1DormRoom.building_instance).joinedload(AdventurerS1BuildingInstance.template), selectinload(AdventurerS1DormRoom.occupants).options(noload(AdventurerS1.traits), noload(AdventurerS1.equipment_items), noload(AdventurerS1.skill_items), noload(AdventurerS1.investor_items), noload(AdventurerS1.warehouse_items), noload(AdventurerS1.materials), noload(AdventurerS1.story_threads), noload(AdventurerS1.dorm_room), noload(AdventurerS1.building_instance)))
        owned_room_expr = None
        followed_room_expr = None
        if user_id:
            followed_adventurer_ids = select(AdventurerS1Follow.adventurer_id).where(AdventurerS1Follow.user_id == user_id)
            owned_room_expr = exists().where((AdventurerS1.dorm_room_id == AdventurerS1DormRoom.id) & (AdventurerS1.owner_id == user_id))
            followed_room_expr = exists().where((AdventurerS1.dorm_room_id == AdventurerS1DormRoom.id) & AdventurerS1.id.in_(followed_adventurer_ids))
            room_order = case((owned_room_expr, 0), (followed_room_expr, 1), (AdventurerS1DormRoom.room_key.is_(None), 2), else_=3)
            query = query.order_by(room_order, AdventurerS1DormRoom.id)
        else:
            query = query.order_by(case((AdventurerS1DormRoom.room_key.is_(None), 0), else_=1), AdventurerS1DormRoom.id)
        total = query.order_by(None).count()
        page_query = query.offset((page - 1) * per_page).limit(per_page)
        room_flags: dict[int, tuple[bool, bool]] = {}
        if owned_room_expr is not None and followed_room_expr is not None:
            page_rows = page_query.add_columns(owned_room_expr.label('is_mine'), followed_room_expr.label('has_followed')).all()
            rooms = [row[0] for row in page_rows]
            room_flags = {row[0].id: (bool(row[1]), bool(row[2])) for row in page_rows}
        else:
            rooms = page_query.all()
        try:
            shared = AdventurerS1Service._build_dormitory_page_payload(rooms)
        except RuntimeError as exc:
            return (None, player_operation_failure(exc, '操作失败'))
        upgrade_inst_ids = shared.get('upgrade_inst_ids') or []
        my_upgrade_donations = AdventurerS1Service._batch_summarize_donation_totals(upgrade_inst_ids, _DONATION_PURPOSE_UPGRADE, user_id=user_id) if upgrade_inst_ids else {}
        rooms = []
        for item in shared['rooms']:
            row = dict(item)
            is_mine, has_followed = room_flags.get(row['id'], (False, False))
            can_move_in = True
            row['is_mine'] = bool(is_mine)
            row['has_followed'] = has_followed
            row['can_move_in'] = can_move_in
            inst_id = row.get('building_instance_id')
            if inst_id is not None and 'upgrade_required_materials' in row:
                row['my_upgrade_donations'] = my_upgrade_donations.get(inst_id, {})
            rooms.append(row)
        result = build_paginated_response(rooms, total, page, per_page)
        result['rooms'] = result.pop('items')
        _dormitory_cache.set(cache_key, result)
        return (result, None)

    @staticmethod
    def name_dorm_room(user_id: int, room_id: int, new_name: str, naming_kind: str='build') -> Tuple[Optional[dict], Optional[str]]:
        """最大贡献者行使一次性命名权，重命名宿舍房间。

        naming_kind:
        - build：初次建造完工时授予（naming_user_id / named_at）
        - upgrade：Lv2 升级完工时在住客中授予（upgrade_naming_user_id / upgrade_named_at）
        """
        kind = (naming_kind or 'build').strip().lower()
        if kind not in (_DONATION_PURPOSE_BUILD, _DONATION_PURPOSE_UPGRADE):
            return (None, '无效的命名类型')
        new_name = (new_name or '').strip()
        if not new_name:
            return (None, '名字不能为空')
        if len(new_name) > 20:
            return (None, '名字最长 20 个字符')
        try:
            with db_manager.transaction():
                room = AdventurerS1DormRoom.query.filter_by(id=room_id).with_for_update().first()
                if not room:
                    raise S1PlayerVisibleError('房间不存在')
                if kind == _DONATION_PURPOSE_BUILD:
                    if room.naming_user_id != user_id:
                        raise S1PlayerVisibleError('你没有这间房的命名权')
                    if room.named_at is not None:
                        raise S1PlayerVisibleError('建造命名权已经用过了')
                else:
                    if room.upgrade_naming_user_id != user_id:
                        raise S1PlayerVisibleError('你没有这间房的升级命名权')
                    if room.upgrade_named_at is not None:
                        raise S1PlayerVisibleError('升级命名权已经用过了')
                duplicate = AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.name == new_name, AdventurerS1DormRoom.id != room.id).first()
                if duplicate:
                    raise S1PlayerVisibleError('已有同名房间，换一个名字吧')
                room.name = new_name
                now = _portable_utc_now()
                if kind == _DONATION_PURPOSE_BUILD:
                    room.named_at = now
                else:
                    room.upgrade_named_at = now
        except RuntimeError as e:
            return (None, player_operation_failure(e, '操作失败'))
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'dorm_room_named', 'room_id': room_id, 'new_name': new_name}, namespace='/')
        except Exception:
            pass
        invalidate_dormitory_cache()
        return (room.to_dict(), None)

    @staticmethod
    def _get_bed_swap_cooldown_hours() -> int:
        return int(current_app.config.get('ADVENTURER_S1_BED_SWAP_COOLDOWN_HOURS', 48))

    @staticmethod
    def _get_bed_swap_request_expiry_hours() -> int:
        return int(current_app.config.get('ADVENTURER_S1_BED_SWAP_REQUEST_EXPIRY_HOURS', 24))

    @staticmethod
    def _check_bed_swap_cooldown(adv: 'AdventurerS1', cooldown_hours: int) -> Optional[str]:
        """检查冒险者换床位冷却，返回错误信息或 None。"""
        if not adv.last_bed_swap_at:
            return None
        now = _portable_utc_now()
        cooldown = timedelta(hours=cooldown_hours)
        if now - adv.last_bed_swap_at < cooldown:
            remaining = cooldown - (now - adv.last_bed_swap_at)
            hours = int(remaining.total_seconds() // 3600)
            minutes = int(remaining.total_seconds() % 3600 // 60)
            return f'{adv.name} 换床位冷却中，还需 {hours}小时{minutes}分钟'
        return None

    @staticmethod
    def _bed_swap_respects_room_pools(requester: 'AdventurerS1', target: 'AdventurerS1') -> bool:
        room_ids = {requester.dorm_room_id, target.dorm_room_id}
        if None in room_ids:
            return False
        room_map = {room.id: room for room in AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.id.in_(room_ids)).all()}
        return AdventurerS1Service._room_allows_adventurer(room_map.get(target.dorm_room_id), requester) and AdventurerS1Service._room_allows_adventurer(room_map.get(requester.dorm_room_id), target)

    @staticmethod
    def move_to_empty_bed(user_id: int, adv_id: int, target_room_id: int, target_bed_index: int) -> Tuple[Optional[dict], Optional[str]]:
        """将自己的冒险者搬到空床位。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
        if not adv:
            return (None, '冒险者不存在')
        if adv.owner_id != user_id:
            return (None, '只能操作自己的冒险者')
        if adv.dorm_room_id is None or adv.bed_index is None:
            return (None, f'{adv.name} 未分配床位')
        cooldown_hours = AdventurerS1Service._get_bed_swap_cooldown_hours()
        err = AdventurerS1Service._check_bed_swap_cooldown(adv, cooldown_hours)
        if err:
            return (None, err)
        room = AdventurerS1DormRoom.query.session.get(AdventurerS1DormRoom, target_room_id)
        if not room:
            return (None, '目标房间不存在')
        if not AdventurerS1Service._room_allows_adventurer(room, adv):
            return (None, '该房间暂不可入住')
        if target_bed_index < 0 or target_bed_index >= room.capacity:
            return (None, '目标床位编号无效')
        now = _portable_utc_now()
        try:
            with db_manager.transaction():
                locked_adv = AdventurerS1.query.filter_by(id=adv_id).with_for_update().first()
                if not locked_adv:
                    return (None, '冒险者不存在')
                if locked_adv.owner_id != user_id:
                    return (None, '只能操作自己的冒险者')
                if not AdventurerS1Service._claim_bed_slot(locked_adv, target_room_id, target_bed_index):
                    return (None, '目标床位已被占用')
                locked_adv.last_bed_swap_at = now
        except Exception as e:
            return (None, player_operation_failure(e, '搬迁失败'))
        invalidate_dormitory_cache()
        return ({'message': f'{adv.name} 已搬到「{room.name}」第 {target_bed_index + 1} 号床位'}, None)

    @staticmethod
    def request_bed_swap(user_id: int, requester_adv_id: int, target_adv_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """向对方冒险者发送床位互换请求。"""
        if requester_adv_id == target_adv_id:
            return (None, '不能和自己互换')
        requester = AdventurerS1.query.session.get(AdventurerS1, requester_adv_id)
        target = AdventurerS1.query.session.get(AdventurerS1, target_adv_id)
        if not requester or not target:
            return (None, '冒险者不存在')
        if requester.owner_id != user_id:
            return (None, '只能用自己的冒险者发起互换')
        if requester.dorm_room_id is None or requester.bed_index is None:
            return (None, f'{requester.name} 未分配床位')
        if target.dorm_room_id is None or target.bed_index is None:
            return (None, f'{target.name} 未分配床位')
        if not AdventurerS1Service._bed_swap_respects_room_pools(requester, target):
            return (None, '双方床位所属房间不兼容，无法互换')
        cooldown_hours = AdventurerS1Service._get_bed_swap_cooldown_hours()
        err = AdventurerS1Service._check_bed_swap_cooldown(requester, cooldown_hours)
        if err:
            return (None, err)
        existing = AdventurerS1BedSwapRequest.query.filter(AdventurerS1BedSwapRequest.status == 'pending', AdventurerS1BedSwapRequest.expires_at > _portable_utc_now(), (AdventurerS1BedSwapRequest.requester_adv_id == requester_adv_id) & (AdventurerS1BedSwapRequest.target_adv_id == target_adv_id) | (AdventurerS1BedSwapRequest.requester_adv_id == target_adv_id) & (AdventurerS1BedSwapRequest.target_adv_id == requester_adv_id)).first()
        if existing:
            return (None, '已存在待处理的互换请求，请等待对方回应或稍后再试')
        expiry_hours = AdventurerS1Service._get_bed_swap_request_expiry_hours()
        now = _portable_utc_now()
        try:
            req = AdventurerS1BedSwapRequest(requester_user_id=user_id, requester_adv_id=requester_adv_id, target_adv_id=target_adv_id, status='pending', expires_at=now + timedelta(hours=expiry_hours))
            db_manager.add(req)
            db_manager.commit()
        except Exception as e:
            return (None, player_operation_failure(e, '发送请求失败'))
        return ({'message': f'已向「{target.name}」的主人发送互换请求，等待对方同意', 'request_id': req.id}, None)

    @staticmethod
    def accept_bed_swap(user_id: int, request_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """接受床位互换请求。"""
        req = AdventurerS1BedSwapRequest.query.session.get(AdventurerS1BedSwapRequest, request_id)
        if not req:
            return (None, '请求不存在')
        if req.status != 'pending':
            return (None, '该请求已处理')
        if req.expires_at < _portable_utc_now():
            req.status = 'expired'
            db_manager.commit()
            return (None, '该请求已过期')
        target = AdventurerS1.query.session.get(AdventurerS1, req.target_adv_id)
        if not target or target.owner_id != user_id:
            return (None, '只能接受发给自己冒险者的请求')
        requester = AdventurerS1.query.session.get(AdventurerS1, req.requester_adv_id)
        if not requester:
            req.status = 'rejected'
            db_manager.commit()
            return (None, '请求方冒险者已不存在')
        if requester.dorm_room_id is None or requester.bed_index is None:
            return (None, f'{requester.name} 已没有床位')
        if target.dorm_room_id is None or target.bed_index is None:
            return (None, f'{target.name} 已没有床位')
        if not AdventurerS1Service._bed_swap_respects_room_pools(requester, target):
            return (None, '双方床位所属房间不兼容，无法互换')
        now = _portable_utc_now()
        try:
            with db_manager.transaction():
                ids_sorted = sorted([req.requester_adv_id, req.target_adv_id])
                locked_map = {row.id: row for row in AdventurerS1.query.filter(AdventurerS1.id.in_(ids_sorted)).order_by(AdventurerS1.id).with_for_update().all()}
                requester = locked_map.get(req.requester_adv_id)
                target = locked_map.get(req.target_adv_id)
                if not requester or not target:
                    return (None, '冒险者不存在')
                if requester.dorm_room_id is None or requester.bed_index is None:
                    return (None, f'{requester.name} 已没有床位')
                if target.dorm_room_id is None or target.bed_index is None:
                    return (None, f'{target.name} 已没有床位')
                if not AdventurerS1Service._bed_swap_respects_room_pools(requester, target):
                    return (None, '双方床位所属房间不兼容，无法互换')
                requester.dorm_room_id, target.dorm_room_id = (target.dorm_room_id, requester.dorm_room_id)
                requester.bed_index, target.bed_index = (target.bed_index, requester.bed_index)
                requester.last_bed_swap_at = now
                target.last_bed_swap_at = now
                req.status = 'accepted'
                req.updated_at = now
        except Exception as e:
            return (None, player_operation_failure(e, '互换失败'))
        invalidate_dormitory_cache()
        return ({'message': f'互换成功！「{requester.name}」和「{target.name}」已交换床位'}, None)

    @staticmethod
    def reject_bed_swap(user_id: int, request_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """拒绝床位互换请求。"""
        req = AdventurerS1BedSwapRequest.query.session.get(AdventurerS1BedSwapRequest, request_id)
        if not req:
            return (None, '请求不存在')
        if req.status != 'pending':
            return (None, '该请求已处理')
        target = AdventurerS1.query.session.get(AdventurerS1, req.target_adv_id)
        if not target or target.owner_id != user_id:
            return (None, '只能拒绝发给自己冒险者的请求')
        req.status = 'rejected'
        req.updated_at = _portable_utc_now()
        db_manager.commit()
        return ({'message': '已拒绝互换请求'}, None)

    @staticmethod
    def cancel_bed_swap(user_id: int, request_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """取消自己发起的床位互换请求。"""
        req = AdventurerS1BedSwapRequest.query.session.get(AdventurerS1BedSwapRequest, request_id)
        if not req:
            return (None, '请求不存在')
        if req.requester_user_id != user_id:
            return (None, '只能取消自己发起的请求')
        if req.status != 'pending':
            return (None, '该请求已处理')
        req.status = 'cancelled'
        req.updated_at = _portable_utc_now()
        db_manager.commit()
        return ({'message': '已取消互换请求'}, None)

    @staticmethod
    def get_pending_swap_requests(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """获取与当前用户相关的待处理互换请求。

        返回 incoming（别人发给我的）和 outgoing（我发出的）两组。
        """
        now = _portable_utc_now()
        my_advs = AdventurerS1.query.filter(AdventurerS1.owner_id == user_id, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).all()
        my_adv_ids = [a.id for a in my_advs]
        if not my_adv_ids:
            return ({'incoming': [], 'outgoing': []}, None)
        AdventurerS1BedSwapRequest.query.filter(AdventurerS1BedSwapRequest.status == 'pending', AdventurerS1BedSwapRequest.expires_at <= now).update({'status': 'expired'}, synchronize_session=False)
        db_manager.commit()
        incoming = AdventurerS1BedSwapRequest.query.filter(AdventurerS1BedSwapRequest.target_adv_id.in_(my_adv_ids), AdventurerS1BedSwapRequest.status == 'pending', AdventurerS1BedSwapRequest.expires_at > now).order_by(AdventurerS1BedSwapRequest.created_at.desc()).all()
        outgoing = AdventurerS1BedSwapRequest.query.filter(AdventurerS1BedSwapRequest.requester_user_id == user_id, AdventurerS1BedSwapRequest.status == 'pending', AdventurerS1BedSwapRequest.expires_at > now).order_by(AdventurerS1BedSwapRequest.created_at.desc()).all()
        return ({'incoming': [r.to_dict() for r in incoming], 'outgoing': [r.to_dict() for r in outgoing]}, None)

    @staticmethod
    def _require_building_upgrade_unlocked() -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import AdventurerS1TavernService
        progress, err = AdventurerS1TavernService.get_tavern_upgrade_progress()
        if err:
            return (None, err)
        if not progress.get('building_upgrade_unlocked'):
            req = progress.get('next_level_requirement')
            return (None, f'需管理员解锁酒馆 Lv.2（全服存活冒险者达 {req} 名）')
        return (progress, None)

    @staticmethod
    def _dorm_upgrade_cost_for_instance(inst: 'AdventurerS1BuildingInstance') -> dict:
        upgrade_cfg = inst.template.upgrade_config if inst.template else None
        return dict((upgrade_cfg or {}).get('cost') or {})

    @staticmethod
    def _summarize_donation_totals(building_instance_id: int, purpose: str, user_id: Optional[int]=None) -> dict:
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        from sqlalchemy import func
        query = db.session.query(AdventurerS1BuildingDonation.material_name, func.sum(AdventurerS1BuildingDonation.amount)).filter(AdventurerS1BuildingDonation.building_instance_id == building_instance_id, AdventurerS1BuildingDonation.purpose == purpose)
        if user_id is not None:
            query = query.filter(AdventurerS1BuildingDonation.user_id == user_id)
        return {mat: int(amt or 0) for mat, amt in query.group_by(AdventurerS1BuildingDonation.material_name).all()}

    @staticmethod
    def _donations_meet_required(required: dict, totals: dict) -> bool:
        if not required:
            return False
        return all((int(totals.get(code, 0) or 0) >= int(amount) for code, amount in required.items()))

    @staticmethod
    def _batch_summarize_donation_totals(building_instance_ids: list[int], purpose: str, user_id: Optional[int]=None) -> dict[int, dict[str, int]]:
        """批量汇总多建筑实例的材料捐献（IN + GROUP BY）。"""
        if not building_instance_ids:
            return {}
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        from sqlalchemy import func as sa_func
        query = db.session.query(AdventurerS1BuildingDonation.building_instance_id, AdventurerS1BuildingDonation.material_name, sa_func.sum(AdventurerS1BuildingDonation.amount)).filter(AdventurerS1BuildingDonation.building_instance_id.in_(building_instance_ids), AdventurerS1BuildingDonation.purpose == purpose)
        if user_id is not None:
            query = query.filter(AdventurerS1BuildingDonation.user_id == user_id)
        result: dict[int, dict[str, int]] = {}
        for iid, mat, amt in query.group_by(AdventurerS1BuildingDonation.building_instance_id, AdventurerS1BuildingDonation.material_name).all():
            result.setdefault(iid, {})[mat] = int(amt or 0)
        return result

    @staticmethod
    def _batch_building_helpers_by_inst(building_instance_ids: list[int], *, for_list: bool=False) -> dict[int, list]:
        """批量预取在位帮工；列表只读身份列，详情保留精力等完整读取。"""
        if not building_instance_ids:
            return {}
        query = AdventurerS1.query.filter(AdventurerS1.building_instance_id.in_(building_instance_ids), AdventurerS1.status == 'building')
        if for_list:
            query = query.with_entities(AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id, AdventurerS1.building_instance_id)
        grouped: dict[int, list] = {}
        for helper in query.all():
            grouped.setdefault(helper.building_instance_id, []).append(helper)
        return grouped

    @staticmethod
    def _batch_ensure_dorm_room_building_instances(rooms: list) -> Tuple[dict[int, 'AdventurerS1BuildingInstance'], dict[int, str]]:
        """为缺关联实例的宿舍房间批量懒创建 built 占位实例（单次事务）。

        返回 (room_id -> instance, room_id -> error_message)。
        """
        by_room: dict[int, AdventurerS1BuildingInstance] = {}
        errors: dict[int, str] = {}
        need_create: list = []
        for room in rooms:
            if room.building_instance_id:
                inst = room.building_instance
                if inst is None:
                    inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, room.building_instance_id)
                if inst:
                    if AdventurerS1Service._dorm_room_instance_owned_by_other(room, inst.id):
                        room.building_instance_id = None
                        need_create.append(room)
                    else:
                        by_room[room.id] = inst
                    continue
                room.building_instance_id = None
            need_create.append(room)
        if not need_create:
            return (by_room, errors)
        codes = {_DORM_CAPACITY_TEMPLATE_CODE.get(int(room.capacity)) for room in need_create}
        codes.discard(None)
        templates_by_code: dict[str, AdventurerS1BuildingTemplate] = {}
        if codes:
            for tpl in AdventurerS1BuildingTemplate.query.filter(AdventurerS1BuildingTemplate.code.in_(list(codes))).all():
                templates_by_code[tpl.code] = tpl
        if codes and len(templates_by_code) < len(codes):
            from plugins.adventurer_s1.seed import seed_building_templates
            seed_building_templates()
            for tpl in AdventurerS1BuildingTemplate.query.filter(AdventurerS1BuildingTemplate.code.in_(list(codes))).all():
                templates_by_code[tpl.code] = tpl
        pending: list[tuple] = []
        for room in need_create:
            code = _DORM_CAPACITY_TEMPLATE_CODE.get(int(room.capacity))
            if not code:
                errors[room.id] = f'不支持的宿舍容量（{room.capacity}）'
                continue
            tpl = templates_by_code.get(code)
            if not tpl or not tpl.upgrade_config:
                errors[room.id] = '宿舍升级模板未配置'
                continue
            inst = AdventurerS1BuildingInstance(template_id=tpl.id, custom_name=room.name, status='built', upgrade_level=1)
            pending.append((room, inst))
        if not pending:
            return (by_room, errors)
        try:
            with db_manager.transaction():
                for room, inst in pending:
                    ok, err = db_manager.add(inst)
                    if not ok:
                        raise RuntimeError(err or '创建宿舍升级实例失败')
                    db.session.flush()
                    room.building_instance_id = inst.id
                    by_room[room.id] = inst
        except Exception as e:
            logger.warning('批量创建宿舍升级占位实例失败: %s', e)
            for room, _ in pending:
                if room.id not in by_room:
                    errors[room.id] = str(e) or '创建宿舍升级实例失败'
        return (by_room, errors)

    @staticmethod
    def _ensure_dorm_room_building_instance(room: 'AdventurerS1DormRoom') -> Tuple[Optional['AdventurerS1BuildingInstance'], Optional[str]]:
        """确保宿舍房间有关联的建筑实例（种子房间懒创建 built 占位实例）。"""
        if room.building_instance_id:
            inst = room.building_instance
            if inst is None:
                inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, room.building_instance_id)
            if inst:
                if AdventurerS1Service._dorm_room_instance_owned_by_other(room, inst.id):
                    room.building_instance_id = None
                else:
                    return (inst, None)
            else:
                room.building_instance_id = None
        by_room, errors = AdventurerS1Service._batch_ensure_dorm_room_building_instances([room])
        if room.id in by_room:
            return (by_room[room.id], None)
        return (None, errors.get(room.id, '创建宿舍升级实例失败'))

    @staticmethod
    def donate_to_dorm_room_upgrade(user_id: int, room_id: int, adventurer_id: int, material_name: str, amount: int) -> Tuple[Optional[dict], Optional[str]]:
        """向宿舍 Lv2 升级募集材料（捐满后需手动点「开始升级」）。"""
        from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService, MATERIAL_META
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
        _, err = AdventurerS1Service._require_building_upgrade_unlocked()
        if err:
            return (None, err)
        room = AdventurerS1DormRoom.query.session.get(AdventurerS1DormRoom, room_id)
        if not room:
            return (None, '房间不存在')
        inst, ensure_err = AdventurerS1Service._ensure_dorm_room_building_instance(room)
        if ensure_err:
            return (None, ensure_err)
        if not inst or inst.status != 'built':
            return (None, '宿舍升级进行中或已完成，暂不接受捐献')
        if (inst.upgrade_level or 1) >= 2:
            return (None, '宿舍已达 Lv.2')
        required = AdventurerS1Service._dorm_upgrade_cost_for_instance(inst)
        if not required:
            return (None, '该宿舍不支持升级')
        if material_name not in required:
            return (None, f'升级不需要 {material_name}')
        try:
            amount_int = int(amount)
        except (TypeError, ValueError):
            return (None, '捐献数量必须是正整数')
        if amount_int <= 0:
            return (None, '捐献数量必须 >0')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not adv.is_player_created or adv.owner_id != user_id:
            return (None, '只能用自己的冒险者捐献')
        if adv.status == 'exploring':
            return (None, '探险中无法进行此操作')
        if adv.status == 'dead':
            return (None, '已死亡的冒险者无法捐献')
        can, reason = adv.can_dispatch()
        if not can:
            return (None, reason or '当前状态无法捐献')
        totals = AdventurerS1Service._summarize_donation_totals(inst.id, _DONATION_PURPOSE_UPGRADE)
        need = int(required[material_name])
        remaining_need = need - int(totals.get(material_name, 0) or 0)
        if remaining_need <= 0:
            display = MATERIAL_META.get(material_name, {}).get('name', material_name)
            return (None, f'{display} 已经捐满')
        have, bal_err = S1MaterialService.get_balance(adv.id, material_name)
        if bal_err:
            return (None, bal_err)
        have = int(have)
        display_name = MATERIAL_META.get(material_name, {}).get('name', material_name)
        if have <= 0:
            return (None, f'「{adv.name}」没有 {display_name}')
        actual = min(amount_int, remaining_need, have)
        if actual <= 0:
            return (None, '可捐数量为 0')
        try:
            with db_manager.transaction():
                _, deduct_err = S1MaterialService.deduct(adv.id, material_name, actual, source_type='dormitory_upgrade_donation', related_id=inst.id, related_type='building_instance', description=f'捐献升级{room.name}', auto_commit=False)
                if deduct_err:
                    raise S1PlayerVisibleError(deduct_err)
                donation = AdventurerS1BuildingDonation(building_instance_id=inst.id, adventurer_id=adv.id, user_id=user_id, material_name=material_name, amount=actual, purpose=_DONATION_PURPOSE_UPGRADE)
                ok, add_err = db_manager.add(donation)
                if not ok:
                    raise RuntimeError(add_err)
                db_manager.flush()
                S1ContributionService.accrue(adv.id, S1ContributionService.donation_contribution(material_name, actual), source_type=AdventurerS1Service._contribution_source_type(_CONTRIBUTION_SRC_DONATION, _DONATION_PURPOSE_UPGRADE), related_id=inst.id, related_type='building_instance', description=f'捐献升级{display_name}×{actual}', auto_commit=False)
        except Exception as e:
            return (None, player_operation_failure(e, '捐献失败'))
        totals = AdventurerS1Service._summarize_donation_totals(inst.id, _DONATION_PURPOSE_UPGRADE)
        donations_complete = AdventurerS1Service._donations_meet_required(required, totals)
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'dorm_room_upgrade_donated', 'room_id': room.id, 'building_instance_id': inst.id, 'material_name': material_name, 'amount': actual}, namespace='/')
        except Exception:
            pass
        invalidate_dormitory_cache()
        return ({'message': '捐献成功', 'room_id': room.id, 'donated_amount': actual, 'upgrade_donations_summary': totals, 'upgrade_donations_complete': donations_complete, 'can_start_upgrade': donations_complete and inst.status == 'built'}, None)

    @staticmethod
    def _is_dorm_room_occupant(adv: AdventurerS1, room: 'AdventurerS1DormRoom') -> bool:
        """冒险者是否占用该宿舍房间的有效床位。"""
        if adv.dorm_room_id != room.id:
            return False
        bed_idx = adv.bed_index
        if bed_idx is None:
            return False
        return 0 <= int(bed_idx) < int(room.capacity or 0)

    @staticmethod
    def _pause_dorm_room_recovery_for_upgrade(room: 'AdventurerS1DormRoom', now: Optional[datetime]=None) -> None:
        """宿舍升级开工：在住 resting 冒险者落账后暂停床位恢复（完工时再 reset 计时）。"""
        now = now or _portable_utc_now()
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import flush_recovery_to_db
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import flush_dorm_rest_session
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        occupant_ids = [adv.id for adv in room.occupants or []]
        tavern_seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(occupant_ids)
        for adv in list(room.occupants or []):
            if adv.status != 'resting' or adv.bed_index is None:
                continue
            if adv.id in tavern_seated_ids:
                continue
            flush_recovery_to_db(adv, now=now, tavern_seated=False)
            flush_dorm_rest_session(adv, now=now)

    @staticmethod
    def _resume_dorm_room_recovery_after_upgrade(room: 'AdventurerS1DormRoom', now: Optional[datetime]=None) -> None:
        """宿舍升级完工：从完工时刻重新计床位恢复。"""
        now = now or _portable_utc_now()
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        occupant_ids = [adv.id for adv in room.occupants or []]
        tavern_seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(occupant_ids)
        for adv in list(room.occupants or []):
            if adv.status != 'resting' or adv.bed_index is None:
                continue
            if adv.id in tavern_seated_ids:
                continue
            adv.stamina_updated_at = now
            adv.hp_updated_at = now
            adv.rest_start_time = now

    @staticmethod
    def upgrade_room(room_id: int, user_id: int, adventurer_id: Optional[int]=None, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """材料捐满后开始宿舍 Lv2 升级（进入 upgrading + 首名帮工）。"""
        _, err = AdventurerS1Service._require_building_upgrade_unlocked()
        if err:
            return (None, err)
        room = AdventurerS1DormRoom.query.session.get(AdventurerS1DormRoom, room_id)
        if not room:
            return (None, '房间不存在')
        inst, ensure_err = AdventurerS1Service._ensure_dorm_room_building_instance(room)
        if ensure_err:
            return (None, ensure_err)
        if not inst:
            return (None, '宿舍升级实例不存在')
        if inst.status != 'built':
            return (None, '宿舍正在升级中或状态异常')
        if (inst.upgrade_level or 1) >= 2:
            return (None, '宿舍已达 Lv.2')
        required = AdventurerS1Service._dorm_upgrade_cost_for_instance(inst)
        if not required:
            return (None, '该宿舍不支持升级')
        totals = AdventurerS1Service._summarize_donation_totals(inst.id, _DONATION_PURPOSE_UPGRADE)
        if not AdventurerS1Service._donations_meet_required(required, totals):
            return (None, '升级材料尚未捐满')
        if adventurer_id is None:
            return (None, '请指定执行升级的冒险者')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '只能派遣自己拥有的冒险者')
        if not is_admin and (not AdventurerS1Service._is_dorm_room_occupant(adv, room)):
            return (None, '只有住在该宿舍的冒险者才能申请升级')
        can, reason = adv.can_dispatch()
        if not can:
            return (None, reason)
        upgrade_cfg = inst.template.upgrade_config if inst.template else {}
        now = _portable_utc_now()
        upgrade_hours = int((upgrade_cfg or {}).get('upgrade_hours', 24))
        AdventurerS1Service._pause_dorm_room_recovery_for_upgrade(room, now)
        inst.status = 'upgrading'
        inst.upgrade_started_at = now
        inst.upgrade_complete_at = now + timedelta(hours=upgrade_hours)
        inst.accumulated_progress_seconds = 0
        AdventurerS1StateTransitionCoordinator.leave_rest(adv, 'building')
        adv.status = 'building'
        adv.building_instance_id = inst.id
        adv.building_assigned_at = now
        adv.stamina_updated_at = now
        from plugins.adventurer_s1.models import AdventurerS1BuildingHelper
        helper = AdventurerS1BuildingHelper(building_instance_id=inst.id, adventurer_id=adv.id, helped_at=now, contributed_seconds=0, purpose=_DONATION_PURPOSE_UPGRADE)
        db_manager.add(helper, flush=False)
        db_manager.commit()
        invalidate_dormitory_cache()
        return ({'room_id': room_id, 'building_instance_id': inst.id, 'upgrade_level': inst.upgrade_level, 'target_level': (inst.upgrade_level or 1) + 1, 'upgrade_hours': upgrade_hours, 'adventurer_id': adv.id, 'adventurer_name': adv.name}, None)

    @staticmethod
    def help_dorm_room_upgrade(room_id: int, user_id: int, adventurer_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """派遣冒险者帮工宿舍升级（复用建筑帮工）。"""
        room = AdventurerS1DormRoom.query.session.get(AdventurerS1DormRoom, room_id)
        if not room:
            return (None, '房间不存在')
        inst, ensure_err = AdventurerS1Service._ensure_dorm_room_building_instance(room)
        if ensure_err:
            return (None, ensure_err)
        if not inst:
            return (None, '宿舍升级实例不存在')
        return AdventurerS1Service.help_building(building_instance_id=inst.id, adventurer_id=adventurer_id, user_id=user_id, is_admin=is_admin)

    @staticmethod
    def _building_upgrade_cost_for_instance(inst: 'AdventurerS1BuildingInstance') -> dict:
        return dict(AdventurerS1Service._building_upgrade_stage_config(inst).get('cost') or {})

    @staticmethod
    def _building_upgrade_stage_config(inst: 'AdventurerS1BuildingInstance') -> dict:
        upgrade_cfg = inst.template.upgrade_config if inst.template else None
        return building_upgrade_stage_config(upgrade_cfg, int(inst.upgrade_level or 1))

    @staticmethod
    def _building_max_level(inst: 'AdventurerS1BuildingInstance') -> int:
        upgrade_cfg = inst.template.upgrade_config if inst.template else None
        return building_max_level(upgrade_cfg)

    @staticmethod
    def _building_upgrade_donation_purpose(inst: 'AdventurerS1BuildingInstance') -> str:
        target_level = int(inst.upgrade_level or 1) + 1
        return _DONATION_PURPOSE_UPGRADE if target_level <= 2 else f'upgrade_lv{target_level}'

    @staticmethod
    def _attach_building_upgrade_fields(result: dict, inst: 'AdventurerS1BuildingInstance', user_id: Optional[int]) -> None:
        """详情接口：下一等级升级募集进度（非宿舍；宿舍走房间面板）。"""
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import AdventurerS1TavernService
        tpl = inst.template
        if not tpl or tpl.category == 'dormitory' or (not tpl.upgrade_config):
            return
        max_level = AdventurerS1Service._building_max_level(inst)
        if (inst.upgrade_level or 1) >= max_level:
            return
        progress, err = AdventurerS1TavernService.get_tavern_upgrade_progress()
        if err or not progress.get('building_upgrade_unlocked'):
            return
        required = AdventurerS1Service._building_upgrade_cost_for_instance(inst)
        if not required:
            return
        purpose = AdventurerS1Service._building_upgrade_donation_purpose(inst)
        donation_maps = AdventurerS1Service._batch_summarize_donation_totals([inst.id], purpose)
        totals = donation_maps.get(inst.id, {})
        donations_complete = AdventurerS1Service._donations_meet_required(required, totals)
        result['upgrade_required_materials'] = required
        result['upgrade_donations_summary'] = totals
        result['upgrade_donations_complete'] = donations_complete
        result['can_start_upgrade'] = donations_complete and inst.status == 'built'
        stage_cfg = AdventurerS1Service._building_upgrade_stage_config(inst)
        result['upgrade_description'] = stage_cfg.get('description')
        result['upgrade_target_level'] = int(inst.upgrade_level or 1) + 1
        result['upgrade_max_level'] = max_level
        if user_id:
            my_maps = AdventurerS1Service._batch_summarize_donation_totals([inst.id], purpose, user_id=user_id)
            result['my_upgrade_donations'] = my_maps.get(inst.id, {})

    @staticmethod
    def _evict_all_users_before_building_upgrade(inst: 'AdventurerS1BuildingInstance', now: Optional[datetime]=None) -> list[int]:
        """升级开工前清空设施占用，送回宿舍休息态（不结算打工/使用奖励）。"""
        now = now or _portable_utc_now()
        evicted_ids: list[int] = []
        handler_key = inst.template.handler_key if inst.template else None
        occupant_ids = [row.id for row in AdventurerS1.query.with_entities(AdventurerS1.id).filter(AdventurerS1.building_instance_id == inst.id, AdventurerS1.status == 'building').order_by(AdventurerS1.id.asc()).all()]
        occupants = AdventurerS1.query.filter(AdventurerS1.id.in_(occupant_ids)).order_by(AdventurerS1.id.asc()).populate_existing().with_for_update(nowait=True).all() if occupant_ids else []
        for adv in occupants:
            if adv.building_instance_id != inst.id or adv.status != 'building':
                continue
            AdventurerS1Service._flush_building_rest_like_recovery(adv)
            AdventurerS1Service.enter_rest(adv, now)
            adv.building_instance_id = None
            adv.building_assigned_at = None
            adv.building_usage_extra = None
            evicted_ids.append(adv.id)
        if handler_key == 'tea_house':
            from plugins.adventurer_s1.models import AdventurerS1TeaHouseSeat
            from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import AdventurerS1TeaHouseSeatService
            seated_ids = sorted({s.adventurer_id for s in AdventurerS1TeaHouseSeat.query.with_entities(AdventurerS1TeaHouseSeat.adventurer_id).filter(AdventurerS1TeaHouseSeat.adventurer_id.isnot(None)).all()})
            seated_adventurers = AdventurerS1.query.filter(AdventurerS1.id.in_(seated_ids)).order_by(AdventurerS1.id.asc()).populate_existing().with_for_update(nowait=True).all() if seated_ids else []
            locked_seats = AdventurerS1TeaHouseSeat.query.filter(AdventurerS1TeaHouseSeat.adventurer_id.in_(seated_ids)).order_by(AdventurerS1TeaHouseSeat.id.asc()).populate_existing().with_for_update(nowait=True).all() if seated_ids else []
            tea_seats_by_adv: dict[int, list] = {}
            for seat in locked_seats:
                tea_seats_by_adv.setdefault(int(seat.adventurer_id), []).append(seat)
            for adv in seated_adventurers:
                adv_seats = tea_seats_by_adv.get(adv.id, [])
                if not adv_seats:
                    continue
                AdventurerS1TeaHouseSeatService.settle_and_clear_seats_for_adventurer(adv, now, locked_seats=adv_seats)
                if adv.id not in evicted_ids:
                    evicted_ids.append(adv.id)
        elif handler_key == 'hot_spring':
            from plugins.adventurer_s1.models import AdventurerS1HotSpringSeat
            from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import AdventurerS1HotSpringSeatService
            seated_ids = sorted({s.adventurer_id for s in AdventurerS1HotSpringSeat.query.with_entities(AdventurerS1HotSpringSeat.adventurer_id).filter(AdventurerS1HotSpringSeat.adventurer_id.isnot(None)).all()})
            seated_adventurers = AdventurerS1.query.filter(AdventurerS1.id.in_(seated_ids)).order_by(AdventurerS1.id.asc()).populate_existing().with_for_update(nowait=True).all() if seated_ids else []
            locked_seats = AdventurerS1HotSpringSeat.query.filter(AdventurerS1HotSpringSeat.adventurer_id.in_(seated_ids)).order_by(AdventurerS1HotSpringSeat.id.asc()).populate_existing().with_for_update(nowait=True).all() if seated_ids else []
            hot_spring_seats_by_adv: dict[int, list] = {}
            for seat in locked_seats:
                hot_spring_seats_by_adv.setdefault(int(seat.adventurer_id), []).append(seat)
            for adv in seated_adventurers:
                adv_seats = hot_spring_seats_by_adv.get(adv.id, [])
                if not adv_seats:
                    continue
                AdventurerS1HotSpringSeatService.settle_and_clear_seats_for_adventurer(adv, now, locked_seats=adv_seats)
                if adv.id not in evicted_ids:
                    evicted_ids.append(adv.id)
        return evicted_ids

    @staticmethod
    def donate_to_building_upgrade(user_id: int, building_instance_id: int, adventurer_id: int, material_name: str, amount: int) -> Tuple[Optional[dict], Optional[str]]:
        """向已建成建筑的下一等级升级募集材料（捐满后需手动点「开始升级」）。"""
        from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService, MATERIAL_META
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
        _, err = AdventurerS1Service._require_building_upgrade_unlocked()
        if err:
            return (None, err)
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if not inst.template or inst.template.category == 'dormitory':
            return (None, '宿舍请从宿舍房间面板升级')
        if inst.status != 'built':
            return (None, '建筑升级进行中或已完成，暂不接受捐献')
        max_level = AdventurerS1Service._building_max_level(inst)
        if (inst.upgrade_level or 1) >= max_level:
            return (None, f'建筑已达最高等级（Lv.{max_level}）')
        required = AdventurerS1Service._building_upgrade_cost_for_instance(inst)
        if not required:
            return (None, '该建筑不支持升级')
        if material_name not in required:
            return (None, f'升级不需要 {material_name}')
        try:
            amount_int = int(amount)
        except (TypeError, ValueError):
            return (None, '捐献数量必须是正整数')
        if amount_int <= 0:
            return (None, '捐献数量必须 >0')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not adv.is_player_created or adv.owner_id != user_id:
            return (None, '只能用自己的冒险者捐献')
        if adv.status == 'exploring':
            return (None, '探险中无法进行此操作')
        if adv.status == 'dead':
            return (None, '已死亡的冒险者无法捐献')
        can, reason = adv.can_dispatch()
        if not can:
            return (None, reason or '当前状态无法捐献')
        purpose = AdventurerS1Service._building_upgrade_donation_purpose(inst)
        totals = AdventurerS1Service._summarize_donation_totals(inst.id, purpose)
        need = int(required[material_name])
        remaining_need = need - int(totals.get(material_name, 0) or 0)
        if remaining_need <= 0:
            display = MATERIAL_META.get(material_name, {}).get('name', material_name)
            return (None, f'{display} 已经捐满')
        have, bal_err = S1MaterialService.get_balance(adv.id, material_name)
        if bal_err:
            return (None, bal_err)
        have = int(have)
        display_name = MATERIAL_META.get(material_name, {}).get('name', material_name)
        if have <= 0:
            return (None, f'「{adv.name}」没有 {display_name}')
        actual = min(amount_int, remaining_need, have)
        if actual <= 0:
            return (None, '可捐数量为 0')
        building_name = inst.custom_name or inst.template.name
        try:
            with db_manager.transaction():
                _, deduct_err = S1MaterialService.deduct(adv.id, material_name, actual, source_type='building_upgrade_donation', related_id=inst.id, related_type='building_instance', description=f'捐献升级{building_name}', auto_commit=False)
                if deduct_err:
                    raise S1PlayerVisibleError(deduct_err)
                donation = AdventurerS1BuildingDonation(building_instance_id=inst.id, adventurer_id=adv.id, user_id=user_id, material_name=material_name, amount=actual, purpose=purpose)
                ok, add_err = db_manager.add(donation)
                if not ok:
                    raise RuntimeError(add_err)
                db_manager.flush()
                S1ContributionService.accrue(adv.id, S1ContributionService.donation_contribution(material_name, actual), source_type=AdventurerS1Service._contribution_source_type(_CONTRIBUTION_SRC_DONATION, purpose), related_id=inst.id, related_type='building_instance', description=f'捐献升级{display_name}×{actual}', auto_commit=False)
        except Exception as e:
            return (None, player_operation_failure(e, '捐献失败'))
        totals = AdventurerS1Service._summarize_donation_totals(inst.id, purpose)
        donations_complete = AdventurerS1Service._donations_meet_required(required, totals)
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'building_upgrade_donated', 'building_instance_id': inst.id, 'material_name': material_name, 'amount': actual}, namespace='/')
        except Exception:
            pass
        return ({'message': '捐献成功', 'building_instance_id': inst.id, 'donated_amount': actual, 'upgrade_donations_summary': totals, 'upgrade_donations_complete': donations_complete, 'can_start_upgrade': donations_complete and inst.status == 'built'}, None)

    @staticmethod
    def upgrade_building(building_instance_id: int, adventurer_id: Optional[int]=None, user_id: Optional[int]=None, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """材料捐满后开始建筑下一等级升级：清场 → upgrading → 首名帮工。

        升级进度由 _tick_building_progress 推进（复用建造帮工机制）。
        """
        _, err = AdventurerS1Service._require_building_upgrade_unlocked()
        if err:
            return (None, err)
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if not inst.template or inst.template.category == 'dormitory':
            return (None, '宿舍请从宿舍房间面板升级')
        if inst.status != 'built':
            return (None, '仅已建成建筑可升级')
        if inst.disabled:
            return (None, '该建筑已禁用')
        if not inst.template.upgrade_config:
            return (None, '该建筑不支持升级')
        max_level = AdventurerS1Service._building_max_level(inst)
        if (inst.upgrade_level or 1) >= max_level:
            return (None, f'建筑已达最高等级（Lv.{max_level}）')
        required = AdventurerS1Service._building_upgrade_cost_for_instance(inst)
        purpose = AdventurerS1Service._building_upgrade_donation_purpose(inst)
        totals = AdventurerS1Service._summarize_donation_totals(inst.id, purpose)
        if not AdventurerS1Service._donations_meet_required(required, totals):
            return (None, '升级材料尚未捐满')
        if adventurer_id is None:
            return (None, '请指定执行升级的冒险者')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and user_id is not None and (adv.owner_id != user_id):
            return (None, '只能派遣自己拥有的冒险者')
        can, reason = adv.can_dispatch()
        if not can:
            return (None, reason)
        now = _portable_utc_now()
        upgrade_cfg = AdventurerS1Service._building_upgrade_stage_config(inst)
        upgrade_hours = int(upgrade_cfg.get('upgrade_hours', 4))
        evicted_ids: list[int] = []
        try:
            with db_manager.transaction():
                locked_adv = AdventurerS1.query.filter_by(id=adventurer_id).populate_existing().with_for_update().first()
                if not locked_adv:
                    raise S1PlayerVisibleError('冒险者不存在')
                if not is_admin and user_id is not None and (locked_adv.owner_id != user_id):
                    raise S1PlayerVisibleError('只能派遣自己拥有的冒险者')
                can, reason = locked_adv.can_dispatch()
                if not can:
                    raise S1PlayerVisibleError(reason)
                adv = locked_adv
                locked_inst = AdventurerS1BuildingInstance.query.filter_by(id=building_instance_id).populate_existing().with_for_update().first()
                if not locked_inst or locked_inst.status != 'built':
                    raise S1PlayerVisibleError('建筑状态已变化，请刷新后重试')
                if (locked_inst.upgrade_level or 1) >= max_level:
                    raise S1PlayerVisibleError(f'建筑已达最高等级（Lv.{max_level}）')
                if AdventurerS1Service._building_usage_queue_waiting_count(locked_inst.id) > 0:
                    raise S1PlayerVisibleError('该建筑仍有冒险者候场，请等待递补完成或由玩家取消候场后再升级')
                if AdventurerS1Service._queueable_building_active_usage_count(locked_inst) > 0:
                    raise S1PlayerVisibleError('该建筑仍有冒险者正在使用，请等待本次使用结束后再升级')
                locked_purpose = AdventurerS1Service._building_upgrade_donation_purpose(locked_inst)
                totals_locked = AdventurerS1Service._summarize_donation_totals(locked_inst.id, locked_purpose)
                if not AdventurerS1Service._donations_meet_required(required, totals_locked):
                    raise S1PlayerVisibleError('升级材料尚未捐满')
                evicted_ids = AdventurerS1Service._evict_all_users_before_building_upgrade(locked_inst, now)
                locked_inst.status = 'upgrading'
                locked_inst.upgrade_started_at = now
                locked_inst.upgrade_complete_at = now + timedelta(hours=upgrade_hours)
                locked_inst.accumulated_progress_seconds = 0
                AdventurerS1StateTransitionCoordinator.leave_rest(adv, 'building', now)
                adv.status = 'building'
                adv.building_instance_id = locked_inst.id
                adv.building_assigned_at = now
                adv.stamina_updated_at = now
                from plugins.adventurer_s1.models import AdventurerS1BuildingHelper
                helper = AdventurerS1BuildingHelper(building_instance_id=locked_inst.id, adventurer_id=adv.id, helped_at=now, contributed_seconds=0, purpose=_DONATION_PURPOSE_UPGRADE)
                ok, add_err = db_manager.add(helper, flush=False)
                if not ok:
                    raise RuntimeError(add_err or '记录帮工失败')
                inst = locked_inst
        except RuntimeError as e:
            return (None, player_operation_failure(e, '操作失败'))
        except Exception as e:
            if AdventurerS1Service._is_lock_timeout_error(e):
                return (None, '建筑或设施占用正在变化，请稍后重试')
            return (None, player_operation_failure(e, '升级失败'))
        invalidate_buildings_list_cache()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            payload = {'action': 'building_upgrade_started', 'building_instance_id': inst.id, 'evicted_adventurer_ids': evicted_ids}
            socketio.emit('adventurer_changed', payload, namespace='/')
            if evicted_ids:
                socketio.emit('adventurer_changed', {'action': 'building_usage_evicted', 'reason': 'building_upgrade', 'ids': evicted_ids, 'building_instance_id': inst.id}, namespace='/')
        except Exception:
            pass
        return ({'building_instance_id': inst.id, 'upgrade_level': inst.upgrade_level, 'target_level': (inst.upgrade_level or 1) + 1, 'upgrade_hours': upgrade_hours, 'adventurer_id': adv.id, 'adventurer_name': adv.name, 'evicted_count': len(evicted_ids), 'evicted_adventurer_ids': evicted_ids}, None)

    @staticmethod
    def _dorm_name_prefix(beds: int) -> str:
        return '单人间' if beds == 1 else f'{beds}人间'

    @staticmethod
    def _collect_taken_dorm_display_names(prefix: str, *, exclude_instance_id: Optional[int]=None) -> set:
        """已占用的「{prefix} X」名称：含种子/完工房间 + 在建/待建实例 custom_name。"""
        taken = {r.name for r in AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.name.like(f'{prefix}%')).all()}
        dorm_tpl_ids = [t.id for t in AdventurerS1BuildingTemplate.query.filter_by(category='dormitory').all()]
        if dorm_tpl_ids:
            q = AdventurerS1BuildingInstance.query.filter(AdventurerS1BuildingInstance.template_id.in_(dorm_tpl_ids), AdventurerS1BuildingInstance.custom_name.isnot(None), AdventurerS1BuildingInstance.custom_name.like(f'{prefix}%'))
            if exclude_instance_id is not None:
                q = q.filter(AdventurerS1BuildingInstance.id != exclude_instance_id)
            for name, in q.with_entities(AdventurerS1BuildingInstance.custom_name).all():
                if name:
                    taken.add(name)
        return taken

    @staticmethod
    def _allocate_dorm_display_name(beds: int, *, exclude_instance_id: Optional[int]=None) -> str:
        """分配未占用的宿舍显示名，与 seed DormRoom 命名风格一致。"""
        prefix = AdventurerS1Service._dorm_name_prefix(beds)
        taken = AdventurerS1Service._collect_taken_dorm_display_names(prefix, exclude_instance_id=exclude_instance_id)
        suffix = 'A'
        while f'{prefix} {suffix}' in taken:
            suffix = chr(ord(suffix) + 1)
        return f'{prefix} {suffix}'

    @staticmethod
    def _is_valid_dorm_display_name(name: str, beds: int) -> bool:
        """是否为「单人间/2人间/4人间 + 单字母后缀」的标准宿舍名。"""
        import re
        prefix = AdventurerS1Service._dorm_name_prefix(beds)
        return bool(re.fullmatch(f'{re.escape(prefix)} [A-Z]', name or ''))

    @staticmethod
    def _count_dormitory_rooms() -> int:
        """全服普通宿舍房间总数；不含系统专属房间。"""
        return AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.room_key.is_(None)).count()

    @staticmethod
    def _count_dormitory_total() -> int:
        """玩家共建宿舍楼数量（用于上限展示/校验）。

        只计 applicant_user_id 非空的 dormitory 实例（玩家申请/共建流程产出），
        不含种子房间为 Lv2 升级懒创建的占位实例（applicant 为空）。
        按实例计数，避免同一实例重复建房间导致虚高。
        """
        return AdventurerS1BuildingInstance.query.join(AdventurerS1BuildingTemplate).filter(AdventurerS1BuildingTemplate.category == 'dormitory', AdventurerS1BuildingInstance.applicant_user_id.isnot(None), AdventurerS1BuildingInstance.status.in_(['locked', 'building', 'built', 'upgrading'])).count()

    @staticmethod
    def request_new_dormitory(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """玩家申请新建宿舍楼：等概率抽 1/2/4 床房型，创建 locked 实例。

        流程：
        1. 玩家必须有至少 1 名非死亡的在场冒险者
        2. 全服 status IN ('locked','building') 的宿舍楼必须 < ADVENTURER_S1_DORMITORY_MAX_CONCURRENT_BUILDING
           （locked 也算占用并发名额，避免玩家无限点申请堆积一堆待建宿舍 + 绕过「抽到即定」）
        3. 从 category='dormitory' 模板中等概率抽 1 个（1/2/4 床各 1/3）
        4. 创建 status='locked' 实例并记录 applicant_user_id

        实例创建后需玩家凑齐 required_materials 调 start_building 开工（阶段 C 实现）。
        """
        has_alive_adv = AdventurerS1.query.filter(AdventurerS1.owner_id == user_id, AdventurerS1.is_player_created == True, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).first() is not None
        if not has_alive_adv:
            return (None, '需要至少 1 名在场冒险者才能申请宿舍')
        new_id = None
        try:
            with db_manager.transaction():
                dormitory_templates = AdventurerS1BuildingTemplate.query.filter_by(category='dormitory', is_active=True).order_by(AdventurerS1BuildingTemplate.id.asc()).with_for_update().all()
                if not dormitory_templates:
                    return (None, '宿舍楼模板未初始化，请联系管理员')
                from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
                max_concurrent = int(S1SettingsService.get('dormitory_max_concurrent') or 2)
                current_in_progress = AdventurerS1BuildingInstance.query.join(AdventurerS1BuildingTemplate).filter(AdventurerS1BuildingTemplate.category == 'dormitory', AdventurerS1BuildingInstance.status.in_(['locked', 'building'])).count()
                if current_in_progress >= max_concurrent:
                    return (None, f'全服已有 {current_in_progress} 栋宿舍楼在建或待建（上限 {max_concurrent}），请等其中一栋完工')
                max_total = int(S1SettingsService.get('dormitory_max_total') or 10)
                total_dormitory = AdventurerS1Service._count_dormitory_total()
                if total_dormitory >= max_total:
                    return (None, f'玩家共建宿舍已达上限（{max_total}），需管理员扩容后才能新建')
                drawn = random.choice(dormitory_templates)
                beds = int(drawn.produced_beds or 0)
                custom_name = AdventurerS1Service._allocate_dorm_display_name(beds)
                inst = AdventurerS1BuildingInstance(template_id=drawn.id, custom_name=custom_name, status='locked', applicant_user_id=user_id)
                ok, err = db_manager.add(inst)
                if not ok:
                    raise RuntimeError(err)
                new_id = inst.id
        except Exception as e:
            return (None, player_operation_failure(e, '申请失败'))
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, new_id)
        return (inst.to_dict(), None)

    @staticmethod
    def get_dormitory_my_status(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """玩家宿舍楼面板的汇总状态：材料库存 + 我申请中的实例 + 全服并发情况。

        一个接口拿齐前端宿舍区面板渲染所需的全部动态数据，避免多次往返。
        - my_materials: owner 名下所有冒险者的材料库存汇总（{name: total_quantity}）
        - my_instances: 申请人是当前用户、状态为 locked 或 building 的实例（含模板）
        - concurrent: 全服 status IN ('locked','building') 的宿舍楼数 + 配置上限
        - in_progress_instances: 全服所有 locked + building 实例（含申请人昵称、
          捐献进度、我的捐献），所有玩家共建模式下大家都能看到全部
        """
        from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        from sqlalchemy import func
        from sqlalchemy.orm import joinedload
        from extensions import db
        from models.user import User
        my_materials, err = S1MaterialService.get_owner_totals(user_id)
        if err:
            return (None, err)
        instances = AdventurerS1BuildingInstance.query.options(joinedload(AdventurerS1BuildingInstance.template)).join(AdventurerS1BuildingTemplate).filter(AdventurerS1BuildingTemplate.category == 'dormitory', AdventurerS1BuildingInstance.status.in_(['locked', 'building'])).order_by(AdventurerS1BuildingInstance.id.desc()).all()
        inst_ids = [i.id for i in instances]
        helpers_by_inst = AdventurerS1Service._batch_building_helpers_by_inst(inst_ids)
        donation_totals: dict = {}
        my_donations: dict = {}
        if inst_ids:
            for iid, mat, amt in db.session.query(AdventurerS1BuildingDonation.building_instance_id, AdventurerS1BuildingDonation.material_name, func.sum(AdventurerS1BuildingDonation.amount)).filter(AdventurerS1BuildingDonation.building_instance_id.in_(inst_ids), AdventurerS1BuildingDonation.purpose == _DONATION_PURPOSE_BUILD).group_by(AdventurerS1BuildingDonation.building_instance_id, AdventurerS1BuildingDonation.material_name).all():
                donation_totals.setdefault(iid, {})[mat] = int(amt or 0)
            for iid, mat, amt in db.session.query(AdventurerS1BuildingDonation.building_instance_id, AdventurerS1BuildingDonation.material_name, func.sum(AdventurerS1BuildingDonation.amount)).filter(AdventurerS1BuildingDonation.building_instance_id.in_(inst_ids), AdventurerS1BuildingDonation.purpose == _DONATION_PURPOSE_BUILD, AdventurerS1BuildingDonation.user_id == user_id).group_by(AdventurerS1BuildingDonation.building_instance_id, AdventurerS1BuildingDonation.material_name).all():
                my_donations.setdefault(iid, {})[mat] = int(amt or 0)
        applicant_user_ids = {i.applicant_user_id for i in instances if i.applicant_user_id}
        applicant_nicknames: dict = {}
        applicant_adv_names: dict = {}
        if applicant_user_ids:
            for u in User.query.filter(User.id.in_(applicant_user_ids)).all():
                applicant_nicknames[u.id] = u.nickname or u.username
            for adv in AdventurerS1.query.filter(AdventurerS1.owner_id.in_(applicant_user_ids), AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).all():
                if adv.owner_id not in applicant_adv_names:
                    applicant_adv_names[adv.owner_id] = adv.name
        inst_dicts = []
        for inst in instances:
            d = inst.to_dict(include_helpers=True, helpers=helpers_by_inst.get(inst.id, []))
            d['applicant_nickname'] = applicant_nicknames.get(inst.applicant_user_id)
            d['applicant_adv_name'] = applicant_adv_names.get(inst.applicant_user_id)
            d['is_mine'] = inst.applicant_user_id == user_id
            d['donations_summary'] = donation_totals.get(inst.id, {})
            d['my_donations'] = my_donations.get(inst.id, {})
            inst_dicts.append(d)
        max_concurrent = int(S1SettingsService.get('dormitory_max_concurrent') or 2)
        current_in_progress = len(instances)
        max_total = int(S1SettingsService.get('dormitory_max_total') or 10)
        total_dormitory = AdventurerS1Service._count_dormitory_total()
        room_count = AdventurerS1Service._count_dormitory_rooms()
        built_instances = AdventurerS1BuildingInstance.query.options(joinedload(AdventurerS1BuildingInstance.template)).join(AdventurerS1BuildingTemplate).filter(AdventurerS1BuildingTemplate.category == 'dormitory', AdventurerS1BuildingInstance.status == 'built').order_by(AdventurerS1BuildingInstance.id.desc()).all()
        built_active_counts = {}
        built_ids = [inst.id for inst in built_instances]
        if built_ids:
            built_active_counts = dict(db.session.query(AdventurerS1.building_instance_id, func.count(AdventurerS1.id)).filter(AdventurerS1.building_instance_id.in_(built_ids), AdventurerS1.status == 'building').group_by(AdventurerS1.building_instance_id).all())
        built_dicts = [inst.to_dict(active_helpers_count=int(built_active_counts.get(inst.id, 0))) for inst in built_instances]
        return ({'my_materials': my_materials, 'in_progress_instances': inst_dicts, 'built_instances': built_dicts, 'concurrent': {'current_building': current_in_progress, 'max_concurrent': max_concurrent}, 'total': {'current': total_dormitory, 'max': max_total, 'room_count': room_count}}, None)

    @staticmethod
    def _transition_dormitory_to_building(inst: AdventurerS1BuildingInstance) -> None:
        """把 locked 实例转为 building 状态（在事务内调用）。

        调用方需保证 inst.template 是 dormitory、status=='locked'，且材料已全部到位。
        本函数不扣材料（捐献阶段已扣），只更新状态字段。WebSocket 推送由调用方负责。
        """
        now = _portable_utc_now()
        inst.status = 'building'
        inst.build_started_at = now
        inst.build_complete_at = now + timedelta(hours=inst.template.build_hours)
        inst.accumulated_progress_seconds = 0

    @staticmethod
    def donate_to_building(user_id: int, adventurer_id: int, building_instance_id: int, material_name: str, amount: int) -> Tuple[Optional[dict], Optional[str]]:
        """玩家用自己的某个冒险者向 locked 宿舍楼捐献材料。

        流程：
        1. 校验实例存在 + 是 dormitory + status=='locked'
        2. 校验冒险者归属当前玩家、未死亡
        3. 校验材料类型是该模板的 required_materials 之一
        4. 实际捐献量 = min(玩家想捐, 该材料缺口, 冒险者余额)；0 时报错
        5. 事务内：扣冒险者 MaterialItem → 写 BuildingDonation 明细 →
           聚合所有材料是否捐满 → 是则同事务转 building
        6. 事务外发 Socket 通知所有玩家
        """
        from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService, MATERIAL_META
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        from sqlalchemy import func
        from extensions import db
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if not inst.template or not inst.template.required_materials:
            return (None, '该建筑不需要捐献材料')
        if inst.status != 'locked':
            return (None, f'宿舍楼当前状态为「{inst.status}」，不再接受捐献')
        if AdventurerS1Service._is_tavern_lv2_building_locked(inst):
            return (None, '该建筑需酒馆升至 Lv2 后才能建造')
        required = inst.template.required_materials or {}
        if not required:
            return (None, '该宿舍楼模板未配置材料需求')
        if material_name not in required:
            return (None, f'该宿舍楼不需要 {material_name}')
        try:
            amount_int = int(amount)
        except (ValueError, TypeError):
            return (None, '捐献数量必须是正整数')
        if amount_int <= 0:
            return (None, '捐献数量必须 >0')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not adv.is_player_created or adv.owner_id != user_id:
            return (None, '只能用自己的冒险者捐献')
        if adv.status == 'exploring':
            return (None, '探险中无法进行此操作')
        if adv.status == 'dead':
            return (None, '已死亡的冒险者无法捐献')
        can, reason = adv.can_dispatch()
        if not can:
            return (None, reason or '当前状态无法捐献')
        donated_so_far = db.session.query(func.coalesce(func.sum(AdventurerS1BuildingDonation.amount), 0)).filter(AdventurerS1BuildingDonation.building_instance_id == inst.id, AdventurerS1BuildingDonation.material_name == material_name, AdventurerS1BuildingDonation.purpose == _DONATION_PURPOSE_BUILD).scalar()
        need = int(required[material_name])
        remaining_need = need - int(donated_so_far or 0)
        if remaining_need <= 0:
            return (None, f"{MATERIAL_META.get(material_name, {}).get('name', material_name)} 已经捐满")
        have, err = S1MaterialService.get_balance(adv.id, material_name)
        if err:
            return (None, err)
        have = int(have)
        display_name = MATERIAL_META.get(material_name, {}).get('name', material_name)
        if have <= 0:
            return (None, f'「{adv.name}」没有 {display_name}')
        actual = min(amount_int, remaining_need, have)
        if actual <= 0:
            return (None, '可捐数量为 0')
        triggered_build_start = False
        try:
            with db_manager.transaction():
                _, err = S1MaterialService.deduct(adv.id, material_name, actual, source_type='dormitory_donation', related_id=inst.id, related_type='building_instance', description=f"捐献给{inst.custom_name or '宿舍楼'}", auto_commit=False)
                if err:
                    raise RuntimeError(err)
                donation = AdventurerS1BuildingDonation(building_instance_id=inst.id, adventurer_id=adv.id, user_id=user_id, material_name=material_name, amount=actual, purpose=_DONATION_PURPOSE_BUILD)
                ok, err = player_database_result(db_manager.add(donation))
                if not ok:
                    raise RuntimeError(err)
                db_manager.flush()
                from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
                S1ContributionService.accrue(adv.id, S1ContributionService.donation_contribution(material_name, actual), source_type=AdventurerS1Service._contribution_source_type(_CONTRIBUTION_SRC_DONATION, _DONATION_PURPOSE_BUILD), related_id=inst.id, related_type='building_instance', description=f'捐献{display_name}×{actual}', auto_commit=False)
                totals = dict(db.session.query(AdventurerS1BuildingDonation.material_name, func.sum(AdventurerS1BuildingDonation.amount)).filter(AdventurerS1BuildingDonation.building_instance_id == inst.id, AdventurerS1BuildingDonation.purpose == _DONATION_PURPOSE_BUILD).group_by(AdventurerS1BuildingDonation.material_name).all())
                all_met = all((int(totals.get(code, 0) or 0) >= int(req) for code, req in required.items()))
                if all_met:
                    AdventurerS1Service._transition_dormitory_to_building(inst)
                    triggered_build_start = True
        except Exception as e:
            return (None, player_operation_failure(e, '捐献失败'))
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'dormitory_donated', 'building_instance_id': inst.id, 'material_name': material_name, 'amount': actual, 'donor_user_id': user_id, 'donor_adventurer_id': adv.id, 'triggered_build_start': triggered_build_start}, namespace='/')
            if triggered_build_start:
                socketio.emit('adventurer_changed', {'action': 'dormitory_started', 'reason': 'donation_satisfied', 'building_instance_id': inst.id}, namespace='/')
        except Exception:
            pass
        invalidate_buildings_list_cache()
        return ({'message': '捐献成功', 'donated_amount': actual, 'building_instance_id': inst.id, 'building_status': inst.status, 'triggered_build_start': triggered_build_start}, None)

    @staticmethod
    def _resolve_donation_refund_target(d: 'AdventurerS1BuildingDonation') -> Optional[int]:
        """捐献退还目标冒险者：原 adventurer_id → 原 user 首个冒险者 → None。"""
        target_adv_id = d.adventurer_id
        if target_adv_id is not None and AdventurerS1.query.session.get(AdventurerS1, target_adv_id) is None:
            target_adv_id = None
        if target_adv_id is None and d.user_id is not None:
            adv = AdventurerS1.query.filter(AdventurerS1.owner_id == d.user_id, AdventurerS1.is_player_created == True).order_by(AdventurerS1.id).first()
            if adv is not None:
                target_adv_id = adv.id
        return target_adv_id

    @staticmethod
    def _clear_building_construction_before_refund(inst: 'AdventurerS1BuildingInstance') -> int:
        """误捐退款前释放建造帮工并删除帮工记录（不保留建造进度时长）。

        Returns:
            释放的 building 态冒险者数量。
        """
        from plugins.adventurer_s1.models import AdventurerS1BuildingHelper
        released = AdventurerS1Service._release_building_occupants(inst, close_open_helper_logs=False)
        AdventurerS1BuildingHelper.query.filter_by(building_instance_id=inst.id).delete(synchronize_session=False)
        inst.accumulated_progress_seconds = 0
        inst.build_started_at = None
        inst.build_complete_at = None
        return released

    @staticmethod
    def _refund_locked_building_donations_and_delete(inst: 'AdventurerS1BuildingInstance', *, refund_description: str, material_source_type: str='dormitory_cancel_refund', reverse_contribution: bool=False) -> Tuple[list, list, list]:
        """locked / building 阶段按捐献明细退材料并删除实例（须在 db_manager.transaction 内调用）。

        building 阶段会先释放帮工冒险者、删除帮工记录并清零建造进度，再退材料删实例。

        Returns:
            (refund_summary, dropped, contribution_reversed)
        """
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation, AdventurerS1BuildingHelper
        from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
        from extensions import db
        if inst.status == 'building' or AdventurerS1BuildingHelper.query.filter_by(building_instance_id=inst.id).first() is not None or AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').first() is not None:
            AdventurerS1Service._clear_building_construction_before_refund(inst)
        donations = AdventurerS1BuildingDonation.query.filter_by(building_instance_id=inst.id).all()
        refund_summary = []
        dropped = []
        contribution_reversed = []
        for d in donations:
            target_adv_id = AdventurerS1Service._resolve_donation_refund_target(d)
            if target_adv_id is None:
                dropped.append({'donation_id': d.id, 'material_name': d.material_name, 'amount': d.amount})
                continue
            _, err = S1MaterialService.add(target_adv_id, d.material_name, int(d.amount), source_type=material_source_type, related_id=inst.id, related_type='building_instance', description=refund_description, auto_commit=False)
            if err:
                raise RuntimeError(err)
            refund_summary.append({'adventurer_id': target_adv_id, 'material_name': d.material_name, 'amount': int(d.amount)})
            if reverse_contribution:
                from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
                contrib = S1ContributionService.donation_contribution(d.material_name, d.amount)
                if contrib > 0:
                    _, err = S1ContributionService.spend(target_adv_id, contrib, source_type='lv2_build_cancel_contrib', related_id=inst.id, related_type='building_instance', description=refund_description, auto_commit=False)
                    if err:
                        raise RuntimeError(err)
                    contribution_reversed.append({'adventurer_id': target_adv_id, 'amount': contrib})
        AdventurerS1BuildingDonation.query.filter_by(building_instance_id=inst.id).delete(synchronize_session=False)
        db.session.delete(inst)
        return (refund_summary, dropped, contribution_reversed)

    @staticmethod
    def cancel_dormitory_construction(admin_user_id: int, building_instance_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员停止仍处于 locked 阶段的宿舍楼，按 Donation 明细原路退还材料。

        退还策略：
        - 优先退到原 adventurer_id 名下（即使该冒险者已 dead）。
        - 原冒险者已被删除（adventurer_id is NULL）时退到原 user_id 名下首个冒险者（任何状态）。
        - 原 user 也没了 → 这笔丢弃，操作日志记录。

        building 阶段不允许停（已开工，进度可能已被帮工推进，回滚成本太高）。
        """
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if not inst.template or inst.template.category != 'dormitory':
            return (None, '只能停止宿舍楼，请用建筑管理处理普通建筑')
        if inst.status != 'locked':
            return (None, f'宿舍楼当前状态为「{inst.status}」，无法停止（仅允许停 locked 阶段）')
        try:
            with db_manager.transaction():
                refund_summary, dropped, _ = AdventurerS1Service._refund_locked_building_donations_and_delete(inst, refund_description=f"管理员停止「{inst.custom_name or '宿舍楼'}」退还")
        except Exception as e:
            return (None, player_operation_failure(e, '停止失败'))
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'dormitory_cancelled', 'building_instance_id': building_instance_id, 'admin_user_id': admin_user_id}, namespace='/')
        except Exception:
            pass
        return ({'message': '已停止建造，材料已按捐献记录退还', 'building_instance_id': building_instance_id, 'refund_count': len(refund_summary), 'dropped_count': len(dropped)}, None)

    @staticmethod
    def admin_preview_lv2_locked_donation_instances() -> Tuple[dict, Optional[str]]:
        """预览酒馆未升 Lv2 时误开放捐献的 Lv2 锁定建筑实例（只读）。"""
        from plugins.adventurer_s1.seed import _TAVERN_LV2_LOCKED_CODES
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        from sqlalchemy import func
        tavern_level = int(S1SettingsService.get('tavern_level') or 1)
        if tavern_level >= 2:
            return ({'tavern_level': tavern_level, 'instances': [], 'count': 0}, None)
        locked_tpl_ids = [t.id for t in AdventurerS1BuildingTemplate.query.filter(AdventurerS1BuildingTemplate.code.in_(_TAVERN_LV2_LOCKED_CODES)).all()]
        if not locked_tpl_ids:
            return ({'tavern_level': tavern_level, 'instances': [], 'count': 0}, None)
        insts = AdventurerS1BuildingInstance.query.filter(AdventurerS1BuildingInstance.template_id.in_(locked_tpl_ids)).all()
        if not insts:
            return ({'tavern_level': tavern_level, 'instances': [], 'count': 0}, None)
        inst_ids = [i.id for i in insts]
        donation_counts = dict(db.session.query(AdventurerS1BuildingDonation.building_instance_id, func.count(AdventurerS1BuildingDonation.id)).filter(AdventurerS1BuildingDonation.building_instance_id.in_(inst_ids)).group_by(AdventurerS1BuildingDonation.building_instance_id).all())
        items = []
        for inst in insts:
            if not donation_counts.get(inst.id):
                continue
            tpl = inst.template
            items.append({'id': inst.id, 'code': tpl.code if tpl else None, 'name': tpl.name if tpl else None, 'status': inst.status, 'donation_count': int(donation_counts.get(inst.id, 0))})
        return ({'tavern_level': tavern_level, 'instances': items, 'count': len(items)}, None)

    @staticmethod
    def admin_refund_lv2_locked_building_donations(admin_user_id: int, building_instance_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """退还酒馆未升 Lv2 时误捐献的 Lv2 锁定建筑材料，删除实例恢复未解锁态。"""
        from plugins.adventurer_s1.seed import _TAVERN_LV2_LOCKED_CODES
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        if int(S1SettingsService.get('tavern_level') or 1) >= 2:
            return (None, '酒馆已升 Lv2，无需退还 Lv2 锁定建筑误捐')
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if not inst.template or inst.template.code not in _TAVERN_LV2_LOCKED_CODES:
            return (None, '仅支持 Lv2 锁定建筑（铁矿山/线索追查处等）')
        if inst.status not in ('locked', 'building'):
            return (None, f'建筑当前状态为「{inst.status}」，仅允许退还 locked/building 阶段误捐')
        building_name = inst.template.name if inst.template else '建筑'
        try:
            with db_manager.transaction():
                refund_summary, dropped, contribution_reversed = AdventurerS1Service._refund_locked_building_donations_and_delete(inst, refund_description=f'退还「{building_name}」误捐材料', material_source_type='lv2_build_cancel_refund', reverse_contribution=True)
        except Exception as e:
            return (None, f'退还失败：{e}')
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'lv2_building_donations_refunded', 'building_instance_id': building_instance_id, 'admin_user_id': admin_user_id}, namespace='/')
        except Exception:
            pass
        return ({'message': f'已退还「{building_name}」误捐材料并移除实例', 'building_instance_id': building_instance_id, 'refund_count': len(refund_summary), 'dropped_count': len(dropped), 'contribution_reversed_count': len(contribution_reversed)}, None)

    @staticmethod
    @monitor_performance('s1.adventurer.list')
    def get_adventurer_list(user_id: int) -> Tuple[Optional[list], Optional[str]]:
        """获取当前玩家拥有的冒险者列表。"""
        from db_repository.s1_tavern_read_repository import tavern_card_read_session
        from plugins.adventurer_s1.services.s1_squad_service import S1SquadService
        with tavern_card_read_session() as session:
            with track_db_phase('page_fetch'):
                adventurers = session.query(AdventurerS1).filter_by(owner_id=user_id, is_player_created=True).options(*AdventurerS1Service._adventurer_card_query_options()).order_by(AdventurerS1.created_at.desc()).all()
            with track_db_phase('card_metadata'):
                from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
                from plugins.adventurer_s1.models import AdventurerS1Follow
                adv_ids = [adv.id for adv in adventurers]
                seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(adv_ids, session=session)
                followed_ids = set()
                if adv_ids:
                    followed_ids = {row[0] for row in session.query(AdventurerS1Follow.adventurer_id).filter(AdventurerS1Follow.user_id == user_id, AdventurerS1Follow.adventurer_id.in_(adv_ids)).all()}
                building_info_map = AdventurerS1Service.build_adventurer_building_info_map(adventurers, session=session)
                dorm_recovery_paused_ids = AdventurerS1Service._dorm_recovery_paused_adventurer_ids(adventurers, session=session)
                all_eq_names = {eq.name for adv in adventurers for eq in adv.equipment_items if eq.name}
                clue_meta_all = AdventurerS1Service._batch_clue_meta_by_item_names(all_eq_names, session=session)
                from plugins.adventurer_s1.services.s1_item_use_condition_service import sanitize_adventurer_payload_items
                from plugins.adventurer_s1.services.s1_trait_loadout_service import build_card_trait_loadout_states
                trait_loadout_states = build_card_trait_loadout_states((trait for adv in adventurers for trait in adv.traits), session=session)
                badge_items = [{'id': adv.id} for adv in adventurers]
                S1SquadService.attach_badges(badge_items, session=session)
                badges_by_id = {item['id']: item for item in badge_items}
        with track_db_phase('serialize'):
            result = []
            for adv in adventurers:
                row = adv.to_dict(current_user_id=user_id, mode='list', trait_loadout_states=trait_loadout_states, tavern_seated=adv.id in seated_ids, building_info=building_info_map.get(adv.id), is_followed=adv.id in followed_ids, item_clue_meta={eq.name: clue_meta_all[eq.name] for eq in adv.equipment_items if eq.name in clue_meta_all})
                row['dorm_recovery_paused'] = adv.id in dorm_recovery_paused_ids
                sanitize_adventurer_payload_items(row)
                if 'squad_badge' in badges_by_id[adv.id]:
                    row['squad_badge'] = badges_by_id[adv.id]['squad_badge']
                result.append(row)
        return (result, None)

    @staticmethod
    def _build_adventurer_detail_dict(adv: 'AdventurerS1', user_id: int, is_admin: bool=False, is_followed: Optional[bool]=None) -> dict:
        """详情 payload：to_dict(detail) + 后端计算的特质恢复倒计时。"""
        item_dict, clue_meta = AdventurerS1Service._build_item_catalog_context(adv, include_warehouse=is_admin or (user_id is not None and adv.owner_id == user_id))
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import enrich_detail_trait_recovery
        tavern_seated = adv.id in AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id])
        data = adv.to_dict(item_dict=item_dict, item_clue_meta=clue_meta, current_user_id=user_id, is_admin=is_admin, tavern_seated=tavern_seated, is_followed=is_followed, include_history_story=is_admin)
        data['dorm_recovery_paused'] = bool(AdventurerS1Service._dorm_recovery_paused_adventurer_ids([adv]))
        enrich_detail_trait_recovery(data, adv, tavern_seated=tavern_seated)
        from plugins.adventurer_s1.services.s1_trait_loadout_service import build_trait_loadout_states
        try:
            loadout_states = build_trait_loadout_states(adv.traits)
        except Exception as exc:
            db_manager.rollback()
            logger.warning('S1 特质装卸投影失败，按强制携带回退 adv=%s: %s', adv.id, exc)
            loadout_states = {}
        active_traits = [trait for trait in adv.traits if not trait.is_archived]
        is_owner = adv.owner_id == user_id
        from utils.time_utils import to_utc_iso
        for entry, trait in zip(data.get('traits') or [], active_traits):
            entry['created_at'] = to_utc_iso(trait.created_at) if trait.created_at else None
            state = loadout_states.get(int(trait.id))
            if state is None:
                continue
            entry['is_pure_positive'] = bool(state['is_pure_positive'])
            entry['equipped'] = bool(state['equipped'])
            if is_owner:
                entry['id'] = int(trait.id)
        from plugins.adventurer_s1.services.s1_item_use_condition_service import sanitize_adventurer_payload_items
        sanitize_adventurer_payload_items(data)
        if not is_admin:
            story_text = db.session.query(AdventurerS1.history_story).filter(AdventurerS1.id == adv.id).scalar() or ''
            data['history_entry_count'] = len(split_history_story_entries(story_text))
        return data

    @staticmethod
    def _refresh_cached_detail_trait_recovery(cached: dict, adv: 'AdventurerS1') -> None:
        """Recovery countdown is time-sensitive; never serve stale snapshots from detail cache."""
        from utils.time_utils import to_utc_iso
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import enrich_detail_trait_recovery
        active_traits_by_name = {t.name: t for t in getattr(adv, 'traits', None) or [] if not getattr(t, 'is_archived', False) and getattr(t, 'name', None)}
        active_traits_by_id = {int(t.id): t for t in getattr(adv, 'traits', None) or [] if not getattr(t, 'is_archived', False) and getattr(t, 'id', None)}
        refreshed_traits = []
        for entry in cached.get('traits') or []:
            name = entry.get('name')
            trait_id = entry.get('id')
            trait_obj = active_traits_by_id.get(int(trait_id)) if isinstance(trait_id, int) else active_traits_by_name.get(name)
            if trait_obj is None:
                continue
            row = dict(entry)
            if row.get('is_pure_positive') is True:
                row['equipped'] = getattr(trait_obj, 'equipped', True) is not False
            elif 'equipped' in row:
                row['equipped'] = True
            row['recovery_progress'] = int(getattr(trait_obj, 'recovery_progress', 0) or 0)
            row['recovery_required'] = getattr(trait_obj, 'recovery_required', None)
            row['recovery_policy'] = getattr(trait_obj, 'recovery_policy', None)
            row['recovery_stage'] = getattr(trait_obj, 'recovery_stage', None)
            if (getattr(trait_obj, 'recovery_stage', None) or '').strip().lower() == 'trace':
                row['recovery_hours'] = getattr(trait_obj, 'recovery_hours', None)
                expires_at = getattr(trait_obj, 'recovery_expires_at', None)
                if expires_at is not None:
                    row['recovery_expires_at'] = to_utc_iso(expires_at)
            row.pop('recovery', None)
            refreshed_traits.append(row)
        cached['traits'] = refreshed_traits
        tavern_seated = adv.id in AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id])
        enrich_detail_trait_recovery(cached, adv, tavern_seated=tavern_seated)

    @staticmethod
    def get_adventurer_history_story(adventurer_id: int, page: int, per_page: int) -> Tuple[Optional[dict], Optional[str]]:
        """分页返回冒险史段落（最新在前）。详情接口不下发全文，改走此接口。"""
        row = db_manager.query(AdventurerS1.history_story).filter(AdventurerS1.id == adventurer_id).first()
        if row is None:
            return (None, '冒险者不存在')
        entries_newest_first = list(reversed(split_history_story_entries(row[0])))
        total = len(entries_newest_first)
        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 5), 20))
        start = (page - 1) * per_page
        page_entries = entries_newest_first[start:start + per_page]
        return (build_paginated_response(page_entries, total, page, per_page), None)

    @staticmethod
    def _publish_trait_recovery_notifications(receipt: Optional[dict]) -> None:
        """Publish a scalar cache/socket receipt without touching ORM state."""
        payload = receipt if isinstance(receipt, dict) else {}
        try:
            notify_ids = sorted({int(value) for value in payload.get('notify_ids') or [] if value is not None})
            activity_owner_ids = sorted({int(value) for value in payload.get('activity_owner_ids') or [] if value is not None})
        except Exception:
            current_app.logger.exception('S1 特质恢复提交后通知回执无效')
            return
        try:
            for adventurer_id in notify_ids:
                invalidate_adventurer_detail_cache(adventurer_id)
            for owner_id in activity_owner_ids:
                invalidate_my_activity_cache(owner_id)
        except Exception:
            current_app.logger.exception('S1 特质恢复提交后缓存失效失败')
        if not notify_ids:
            return
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'trait_recovery_settled', 'ids': notify_ids}, namespace='/')
        except Exception:
            current_app.logger.exception('S1 特质恢复提交后 Socket 通知失败')

    @staticmethod
    def _settle_due_trait_recovery_for_adventurers(adventurers: list, *, auto_commit: bool=True, notify: bool=True) -> dict:
        """结算到期特质恢复；嵌入状态转换时由外层事务统一提交。

        ``auto_commit=False`` 只供已经拥有事务边界的写路径使用。这样状态、座位清理、
        恢复结算和容器成员关系会原子提交，并保持外层行锁直到最终提交。
        """
        if not adventurers:
            return {'notify_ids': [], 'activity_owner_ids': []}
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import settle_due_natural_trait_recovery, settle_due_trace_trait_recovery

        def _state_signature(adv) -> tuple:
            trait_rows = []
            for trait in getattr(adv, 'traits', None) or []:
                trait_rows.append((getattr(trait, 'id', None), bool(getattr(trait, 'is_archived', False)), getattr(trait, 'recovery_policy', None), getattr(trait, 'recovery_stage', None), getattr(trait, 'recovery_progress', None), getattr(trait, 'recovery_required', None), getattr(trait, 'recovery_hours', None), getattr(trait, 'recovery_expires_at', None), getattr(trait, 'recovery_rest_baseline_seconds', None), getattr(trait, 'archived_reason', None), getattr(trait, 'archived_at', None)))
            trait_rows.sort(key=lambda row: (row[0] is None, row[0] or 0))
            return (tuple(trait_rows), getattr(adv, 'trait_recovery_rest_seconds', None), getattr(adv, 'rest_start_time', None), getattr(adv, 'building_assigned_at', None))

        def _settle_and_build_receipt() -> dict:
            before_by_id = {int(adv.id): _state_signature(adv) for adv in adventurers if getattr(adv, 'id', None) is not None}
            owner_by_id = {int(adv.id): int(adv.owner_id) for adv in adventurers if getattr(adv, 'id', None) is not None and getattr(adv, 'owner_id', None) is not None}
            settle_due_trace_trait_recovery(adventurers)
            settle_due_natural_trait_recovery(adventurers)
            changed_ids = [int(adv.id) for adv in adventurers if getattr(adv, 'id', None) is not None and before_by_id.get(int(adv.id)) != _state_signature(adv)]
            return {'notify_ids': sorted(set(changed_ids)), 'activity_owner_ids': sorted({owner_by_id[adventurer_id] for adventurer_id in changed_ids if adventurer_id in owner_by_id})}
        if auto_commit:
            with db_manager.transaction():
                receipt = _settle_and_build_receipt()
        else:
            receipt = _settle_and_build_receipt()
        if notify:
            AdventurerS1Service._publish_trait_recovery_notifications(receipt)
        return receipt

    @staticmethod
    def get_adventurer_detail(adventurer_id: int, user_id: int, is_admin: bool=False, force_refresh: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """获取冒险者详情（含仓库物品 + 人际关系）。

        人际关系只暴露「最亲密 / 最不和」各 1 人的身份（id + 姓名），
        具体好感度数值（score / interaction_count）不出接口；score=0（默认陌生人）不计入任何一侧。
        """
        from sqlalchemy.orm import joinedload, selectinload, noload, defer, lazyload
        from plugins.adventurer_s1.models import AdventurerS1BuildingInstance, AdventurerS1DormRoom, AdventurerS1Follow
        from plugins.adventurer_s1.services.s1_squad_service import S1SquadService
        cache_key = f'detail:{adventurer_id}:{user_id}:{int(is_admin)}:{int(S1SquadService.enabled())}'
        cached = _adventurer_detail_cache.get(cache_key)
        if cached is not None and force_refresh:
            cached_owner_id = int(cached.get('owner_id') or 0)
            if is_admin or cached_owner_id == int(user_id or 0):
                cached = None
        if cached is not None:
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, populate_existing=True, options=(selectinload(AdventurerS1.traits), joinedload(AdventurerS1.building_instance).options(joinedload(AdventurerS1BuildingInstance.template), noload(AdventurerS1BuildingInstance.helpers)), defer(AdventurerS1.history_story), noload(AdventurerS1.equipment_items), noload(AdventurerS1.skill_items), noload(AdventurerS1.investor_items), noload(AdventurerS1.warehouse_items), noload(AdventurerS1.materials), noload(AdventurerS1.dorm_room), noload(AdventurerS1.story_threads),))
            if not adv:
                return (None, '冒险者不存在')
            if cached.get('owner_id') == adv.owner_id:
                if 'party_preference' in cached:
                    cached['party_preference'] = adv.party_preference
                AdventurerS1Service._refresh_cached_detail_trait_recovery(cached, adv)
                S1SquadService.attach_badges([cached])
                return (cached, None)
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id, populate_existing=True, options=(selectinload(AdventurerS1.traits), selectinload(AdventurerS1.equipment_items), selectinload(AdventurerS1.skill_items), selectinload(AdventurerS1.investor_items) if is_admin else noload(AdventurerS1.investor_items), lazyload(AdventurerS1.warehouse_items), lazyload(AdventurerS1.materials), selectinload(AdventurerS1.dorm_room).options(noload(AdventurerS1DormRoom.occupants), joinedload(AdventurerS1DormRoom.building_instance).options(joinedload(AdventurerS1BuildingInstance.template), noload(AdventurerS1BuildingInstance.helpers))), joinedload(AdventurerS1.building_instance).options(joinedload(AdventurerS1BuildingInstance.template), noload(AdventurerS1BuildingInstance.helpers)), defer(AdventurerS1.history_story), noload(AdventurerS1.story_threads),))
        if not adv:
            return (None, '冒险者不存在')
        is_followed = None
        if user_id:
            is_followed = AdventurerS1Follow.query.filter_by(user_id=user_id, adventurer_id=adventurer_id).first() is not None
        data = AdventurerS1Service._build_adventurer_detail_dict(adv, user_id, is_admin=is_admin, is_followed=is_followed)
        if int(adv.owner_id or 0) == int(user_id or 0):
            from plugins.adventurer_s1.services.s1_title_service import S1TitleService
            data.update(S1TitleService.player_state(adv))
        from plugins.adventurer_s1.models import AdventurerS1Relationship
        rel_involves_me = (AdventurerS1Relationship.adventurer_a_id == adventurer_id) | (AdventurerS1Relationship.adventurer_b_id == adventurer_id)
        rels = AdventurerS1Relationship.query.filter(rel_involves_me, AdventurerS1Relationship.score != 0).all()
        best_rel = max((r for r in rels if r.score > 0), key=lambda r: (r.score, r.updated_at or datetime.min), default=None)
        worst_rel = min((r for r in rels if r.score < 0), key=lambda r: (r.score, -(r.updated_at.timestamp() if r.updated_at else 0)), default=None)

        def _other_id(rel) -> Optional[int]:
            if rel is None:
                return None
            return rel.adventurer_b_id if rel.adventurer_a_id == adventurer_id else rel.adventurer_a_id
        best_id, worst_id = (_other_id(best_rel), _other_id(worst_rel))
        other_ids = {i for i in (best_id, worst_id) if i is not None}
        name_map = dict(AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name).filter(AdventurerS1.id.in_(other_ids)).all()) if other_ids else {}
        data['relationships'] = {'best': {'adventurer_id': best_id, 'adventurer_name': name_map.get(best_id, '?')} if best_id else None, 'worst': {'adventurer_id': worst_id, 'adventurer_name': name_map.get(worst_id, '?')} if worst_id else None}
        S1SquadService.attach_badges([data])
        _adventurer_detail_cache.set(cache_key, data)
        return (data, None)
    VALID_PARTY_PREFERENCES = ('auto', 'manual', 'idle', 'rest')

    @staticmethod
    def set_party_preference(adventurer_id: int, user_id: int, preference: str, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """设置组队偏好：auto 自动进入随机地图单人匹配，其他值不自动报名。"""
        if preference not in AdventurerS1Service.VALID_PARTY_PREFERENCES:
            return (None, '无效的组队偏好')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        err = AdventurerS1Service.check_owner_playable(adv, user_id, is_admin=is_admin)
        if err:
            return (None, err)
        if not is_admin:
            exploring_err = AdventurerS1Service.exploring_action_error(adv.status)
            if exploring_err:
                return (None, exploring_err)
        if adv.party_preference != preference:
            with db_manager.transaction():
                adv.party_preference = preference
        invalidate_adventurer_detail_cache(adventurer_id)
        return (AdventurerS1Service._build_adventurer_detail_dict(adv, user_id, is_admin=is_admin), None)

    @staticmethod
    def join_party(adventurer_id: int, party_id: int, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """玩家手动让自己的冒险者加入指定 forming 小队。

        权限：只能操作自己名下的冒险者（admin 例外）。
        业务校验（跨度 / 状态 / 精力 / 满员）下沉到 party_service.join_party_manual。
        返回值仅含前端刷新页面所需的最小字段集。
        """
        from plugins.adventurer_s1.models import AdventurerS1Party
        from plugins.adventurer_s1.services.adventurer_s1_party_service import join_party_manual
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        err = AdventurerS1Service.check_owner_playable(adv, user_id, is_admin=is_admin)
        if err:
            return (None, err)
        party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
        if not party:
            return (None, '小队不存在')
        ok, err = join_party_manual(adv, party)
        if not ok:
            return (None, err)
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
        invalidate_parties_board_cache()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'joined_party', 'reason': 'manual_join', 'ids': [adventurer_id]}, namespace='/')
            socketio.emit('party_changed', {'action': 'member_joined', 'party_id': party.id, 'member_count': len(party.members), 'departure_time': party.departure_time.isoformat() if party.departure_time else None}, namespace='/')
        except Exception:
            pass
        return ({'adventurer_id': adv.id, 'party_id': party.id, 'member_count': len(party.members), 'target_size': party.target_size, 'departure_time': party.departure_time.isoformat() if party.departure_time else None}, None)

    @staticmethod
    def list_special_dungeons(adventurer_id: Optional[int]=None, user_id: Optional[int]=None, is_admin: bool=False) -> Tuple[Optional[list], Optional[str]]:
        """列出门票制特殊副本（供前端组队页展示）。

        只返回名称、所需门票、不限等级文案与世界观简介；后端掷骰概率属于隐藏机制，
        一律不下发前端。        若带上 adventurer_id，则附带该冒险者是否随身携带对应门票（用于
        前端置灰「组建」按钮），仅校验归属后查询，不暴露概率。仓库中的门票不计入。
        """
        from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_SPECIAL_DUNGEONS, get_s1_special_party_target_size
        base = _special_dungeons_list_cache.get('base')
        if base is None:
            fixed_size = get_s1_special_party_target_size()
            base = []
            for dungeon in S1_SPECIAL_DUNGEONS:
                base.append({'name': dungeon['name'], 'ticket_item': dungeon['ticket_item'], 'level_label': dungeon.get('level_label') or '特殊副本', 'flavor': dungeon.get('flavor', ''), 'target_size': fixed_size, 'entry_enabled': dungeon.get('entry_enabled', True)})
            _special_dungeons_list_cache.set('base', base)
        if adventurer_id is None:
            return ([dict(entry) for entry in base], None)
        owner_row = db_manager.query(AdventurerS1.owner_id).filter(AdventurerS1.id == adventurer_id).first()
        if not owner_row:
            return (None, '冒险者不存在')
        if not is_admin and owner_row[0] != user_id:
            return (None, '无权查看此冒险者')
        from plugins.adventurer_s1.services.s1_item_grant_service import _special_dungeons_ticket_cache_manager, adventurer_carried_item_names
        ticket_cache = _special_dungeons_ticket_cache_manager()
        held = ticket_cache.get(adventurer_id)
        if held is None:
            ticket_names = [d['ticket_item'] for d in S1_SPECIAL_DUNGEONS]
            held = adventurer_carried_item_names(adventurer_id, ticket_names)
            ticket_cache.set(adventurer_id, held)
        payload = []
        for entry in base:
            row = dict(entry)
            row['holds_ticket'] = row['ticket_item'] in held
            payload.append(row)
        return (payload, None)

    @staticmethod
    def create_special_party(adventurer_id: int, dungeon_name: str, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """玩家用持票冒险者手动组建一支前往门票制特殊副本的小队（该冒险者为队长）。

        特殊副本固定 4 人；创建后为 forming（集结中），满员后启动出发倒计时。
        权限：只能操作自己名下冒险者（admin 例外）。门票在发车时才扣除。
        """
        from plugins.adventurer_s1.services.adventurer_s1_party_service import create_special_party as _create_special_party
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        err = AdventurerS1Service.check_owner_playable(adv, user_id, is_admin=is_admin)
        if err:
            return (None, err)
        party, err = _create_special_party(adv, dungeon_name)
        if err:
            return (None, err)
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
        invalidate_parties_board_cache()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'joined_party', 'reason': 'create_special_party', 'ids': [adventurer_id]}, namespace='/')
            socketio.emit('party_changed', {'action': 'party_created', 'party_id': party.id, 'member_count': len(party.members)}, namespace='/')
        except Exception:
            pass
        return ({'adventurer_id': adv.id, 'party_id': party.id, 'target_dungeon': party.target_dungeon, 'status': party.status, 'member_count': len(party.members), 'target_size': party.target_size, 'departure_time': party.departure_time.isoformat() if party.departure_time else None}, None)

    @staticmethod
    def _publish_party_leave(adventurer_id: int, info: dict, *, reason: str, emit_adventurer: bool=True) -> None:
        """提交后统一失效组队视图并广播离队结果。"""
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
        invalidate_parties_board_cache()
        matchmaking_changed = bool(info.get('matchmaking_changed'))
        if matchmaking_changed:
            from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import _emit_matchmaking_changed
            _emit_matchmaking_changed('match_ticket_unmatched', ticket_user_ids=info.get('ticket_user_ids') or None, party_id=info.get('party_id'), adventurer_id=adventurer_id, cancelled_ticket_count=len(info.get('cancelled_ticket_ids') or ()), forming_ticket_count=len(info.get('forming_ticket_ids') or ()), requeued_ticket_count=len(info.get('requeued_ticket_ids') or ()))
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            if emit_adventurer:
                socketio.emit('adventurer_changed', {'action': 'left_party', 'reason': reason, 'ids': [adventurer_id]}, namespace='/')
            if not matchmaking_changed:
                socketio.emit('party_changed', {'action': 'member_left', 'party_id': info['party_id'], 'member_count': info['member_count']}, namespace='/')
        except Exception:
            pass

    @staticmethod
    def leave_party(adventurer_id: int, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """玩家手动让自己的冒险者离开当前所在 forming 小队（仅限 forming 阶段）。

        权限同 join_party。离队后人数 < target_size 时暂停 departure_time 并冻结剩余秒数。
        """
        from plugins.adventurer_s1.services.adventurer_s1_party_service import leave_party as _leave_party_core
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '无权操作此冒险者')
        ok, err, info = _leave_party_core(adv)
        if not ok:
            return (None, err)
        AdventurerS1Service._publish_party_leave(adventurer_id, info, reason='manual_leave')
        return ({'adventurer_id': adv.id, 'party_id': info['party_id'], 'member_count': info['member_count']}, None)

    @staticmethod
    def vote_party_departure(adventurer_id: int, party_id: int, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """成员投票提前出发；全体成员投赞成票后下一调度 tick 立即发车。"""
        from plugins.adventurer_s1.models import AdventurerS1Party
        from plugins.adventurer_s1.services.adventurer_s1_party_service import vote_party_departure as _vote_core
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        err = AdventurerS1Service.check_owner_playable(adv, user_id, is_admin=is_admin)
        if err:
            return (None, err)
        party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
        if not party:
            return (None, '小队不存在')
        ok, err, info = _vote_core(adv, party)
        if not ok:
            return (None, err)
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
        invalidate_parties_board_cache()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('party_changed', {'action': 'departure_vote', 'party_id': party.id, 'vote_count': info['vote_count'], 'member_count': info['member_count'], 'all_voted': info['all_voted'], 'departure_time': info['departure_time']}, namespace='/')
        except Exception:
            pass
        return (info, None)

    @staticmethod
    def equip_from_warehouse(adventurer_id: int, user_id: int, is_admin: bool=False, item_id: Optional[int]=None, item_name: Optional[str]=None, quantity: Optional[int]=None) -> Tuple[Optional[dict], Optional[str]]:
        """从仓库取出物品装备到随身。优先 item_id；仅一件同名时可用 item_name。

        quantity 为 None → 整堆取出（向后兼容）；否则按数量拆堆，剩余留在仓库。
        随身已有同属性堆则合并，不新占格子。
        """
        from plugins.adventurer_s1.seed import is_herbalist_item
        from plugins.adventurer_s1.services.s1_item_instance_service import add_or_merge_stack, find_mergeable_stack, resolve_move_quantity, resolve_warehouse_item, take_from_stack, lock_warehouse_aggregate
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '无权操作此冒险者')
        inv_err = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id, is_admin)
        if inv_err:
            return (None, inv_err)
        wh_item, err = resolve_warehouse_item(adventurer_id, item_id=item_id, item_name=item_name)
        if err:
            return (None, err)
        _, qty_err = resolve_move_quantity(wh_item, quantity)
        if qty_err:
            return (None, qty_err)
        try:
            with db_manager.transaction():
                lock_warehouse_aggregate(adventurer_id)
                wh_row = AdventurerS1WarehouseItem.query.filter_by(id=wh_item.id, adventurer_id=adventurer_id).populate_existing().with_for_update().first()
                if not wh_row:
                    raise S1PlayerVisibleError(f'「{wh_item.name}」已不在仓库')
                move_qty, qty_err = resolve_move_quantity(wh_row, quantity)
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)
                merge_target = find_mergeable_stack(AdventurerS1Equipment, adventurer_id, name=wh_row.name, item_type=wh_row.type, rarity=wh_row.rarity, custom_desc=wh_row.custom_desc, uses_left=wh_row.uses_left, evolution_stage=wh_row.evolution_stage, recovery_minutes=wh_row.recovery_minutes, recovery_started_at=wh_row.recovery_started_at, recovery_ends_at=wh_row.recovery_ends_at, is_player_locked=wh_row.is_player_locked, for_update=True)
                if merge_target is None:
                    equip_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
                    current_count = AdventurerS1Equipment.query.filter_by(adventurer_id=adventurer_id).count()
                    if current_count >= equip_limit:
                        raise S1PlayerVisibleError(f'随身装备已达上限 {equip_limit} 件，请先卸下其他装备')
                    if is_herbalist_item(wh_row.name):
                        herb_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
                        equipment_names = AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.name).filter_by(adventurer_id=adventurer_id).all()
                        herb_count = sum((1 for name, in equipment_names if is_herbalist_item(name)))
                        if herb_count >= herb_limit:
                            raise S1PlayerVisibleError(f'草药店类物品随身已达上限 {herb_limit} 件')
                snapshot = dict(name=wh_row.name, item_type=wh_row.type, rarity=wh_row.rarity, custom_desc=wh_row.custom_desc, uses_left=wh_row.uses_left, evolution_stage=wh_row.evolution_stage, recovery_minutes=wh_row.recovery_minutes, recovery_started_at=wh_row.recovery_started_at, recovery_ends_at=wh_row.recovery_ends_at, is_player_locked=wh_row.is_player_locked)
                take_from_stack(wh_row, move_qty)
                add_or_merge_stack(AdventurerS1Equipment, adventurer_id, quantity=move_qty, **snapshot)
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        invalidate_adventurer_detail_cache(adventurer_id)
        return (AdventurerS1Service._build_adventurer_detail_dict(adv, user_id, is_admin=is_admin), None)

    @staticmethod
    def unequip_to_warehouse(adventurer_id: int, user_id: int, is_admin: bool=False, item_id: Optional[int]=None, item_name: Optional[str]=None, quantity: Optional[int]=None) -> Tuple[Optional[dict], Optional[str]]:
        """把随身装备卸下存入仓库。优先 item_id；仅一件同名时可用 item_name。

        quantity 为 None → 整堆卸下（向后兼容）；否则按数量拆堆，剩余留在随身。
        仓库已有同属性堆则合并，不新占格子。
        """
        from plugins.adventurer_s1.services.s1_item_instance_service import add_or_merge_stack, find_mergeable_stack, resolve_equipment_item, resolve_move_quantity, take_from_stack, lock_warehouse_aggregate
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '无权操作此冒险者')
        inv_err = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id, is_admin)
        if inv_err:
            return (None, inv_err)
        eq, err = resolve_equipment_item(adventurer_id, item_id=item_id, item_name=item_name)
        if err:
            return (None, err)
        _, qty_err = resolve_move_quantity(eq, quantity)
        if qty_err:
            return (None, qty_err)
        try:
            with db_manager.transaction():
                lock_warehouse_aggregate(adventurer_id)
                eq_row = AdventurerS1Equipment.query.filter_by(id=eq.id, adventurer_id=adventurer_id).populate_existing().with_for_update().first()
                if not eq_row:
                    raise S1PlayerVisibleError(f'「{eq.name}」已不在随身')
                move_qty, qty_err = resolve_move_quantity(eq_row, quantity)
                if qty_err:
                    raise S1PlayerVisibleError(qty_err)
                merge_target = find_mergeable_stack(AdventurerS1WarehouseItem, adventurer_id, name=eq_row.name, item_type=eq_row.type, rarity=eq_row.rarity, custom_desc=eq_row.custom_desc, uses_left=eq_row.uses_left, evolution_stage=eq_row.evolution_stage, recovery_minutes=eq_row.recovery_minutes, recovery_started_at=eq_row.recovery_started_at, recovery_ends_at=eq_row.recovery_ends_at, is_player_locked=eq_row.is_player_locked, for_update=True)
                if merge_target is None:
                    wh_cap = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
                    wh_count = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adventurer_id).count()
                    if wh_count >= wh_cap:
                        raise S1PlayerVisibleError(f'仓库已满（{wh_cap} 件），请先清理仓库')
                snapshot = dict(name=eq_row.name, item_type=eq_row.type, rarity=eq_row.rarity, custom_desc=eq_row.custom_desc, uses_left=eq_row.uses_left, evolution_stage=eq_row.evolution_stage, recovery_minutes=eq_row.recovery_minutes, recovery_started_at=eq_row.recovery_started_at, recovery_ends_at=eq_row.recovery_ends_at, is_player_locked=eq_row.is_player_locked)
                take_from_stack(eq_row, move_qty)
                add_or_merge_stack(AdventurerS1WarehouseItem, adventurer_id, quantity=move_qty, **snapshot)
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        invalidate_adventurer_detail_cache(adventurer_id)
        return (AdventurerS1Service._build_adventurer_detail_dict(adv, user_id, is_admin=is_admin), None)

    @staticmethod
    def batch_equip_from_warehouse(adventurer_id: int, user_id: int, is_admin: bool=False, item_ids: Optional[List[int]]=None, item_names: Optional[List[str]]=None) -> Tuple[Optional[dict], Optional[str]]:
        """批量从仓库取出装备到随身（每件整堆移动）。须传 item_ids；item_names 仅兼容旧客户端。

        随身已有同属性堆则合并、不占新格；仅新建堆才计入上限。
        """
        from plugins.adventurer_s1.seed import is_herbalist_item
        from plugins.adventurer_s1.services.s1_item_instance_service import add_or_merge_stack, find_mergeable_stack, lock_warehouse_aggregate, resolve_warehouse_item, take_from_stack
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '无权操作此冒险者')
        inv_err = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id, is_admin)
        if inv_err:
            return (None, inv_err)
        ids = list(item_ids or [])
        if not ids and item_names:
            for name in item_names:
                wh_item, err = resolve_warehouse_item(adventurer_id, item_name=name)
                if err:
                    return (None, err)
                ids.append(wh_item.id)
        if not ids:
            return (None, '缺少 item_ids')
        equip_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
        herb_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
        equipped = []
        errors = []
        try:
            with db_manager.transaction():
                lock_warehouse_aggregate(adventurer_id)
                eq_count = AdventurerS1Equipment.query.filter_by(adventurer_id=adventurer_id).count()
                equipment_names = AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.name).filter_by(adventurer_id=adventurer_id).all()
                herb_count = sum((1 for name, in equipment_names if is_herbalist_item(name)))
                for wh_id in ids:
                    wh_row = AdventurerS1WarehouseItem.query.filter_by(id=wh_id, adventurer_id=adventurer_id).populate_existing().with_for_update().first()
                    if not wh_row:
                        errors.append('未找到指定仓库物品')
                        continue
                    is_herb = is_herbalist_item(wh_row.name)
                    merge_target = find_mergeable_stack(AdventurerS1Equipment, adventurer_id, name=wh_row.name, item_type=wh_row.type, rarity=wh_row.rarity, custom_desc=wh_row.custom_desc, uses_left=wh_row.uses_left, evolution_stage=wh_row.evolution_stage, recovery_minutes=wh_row.recovery_minutes, recovery_started_at=wh_row.recovery_started_at, recovery_ends_at=wh_row.recovery_ends_at, is_player_locked=wh_row.is_player_locked, for_update=True)
                    if merge_target is None:
                        if eq_count >= equip_limit:
                            errors.append(f'随身已达上限 {equip_limit} 件')
                            continue
                        if is_herb and herb_count >= herb_limit:
                            errors.append(f'草药店类物品将超过上限 {herb_limit}')
                            continue
                    move_qty = item_quantity_of(wh_row)
                    snapshot = dict(name=wh_row.name, item_type=wh_row.type, rarity=wh_row.rarity, custom_desc=wh_row.custom_desc, uses_left=wh_row.uses_left, evolution_stage=wh_row.evolution_stage, recovery_minutes=wh_row.recovery_minutes, recovery_started_at=wh_row.recovery_started_at, recovery_ends_at=wh_row.recovery_ends_at, is_player_locked=wh_row.is_player_locked)
                    take_from_stack(wh_row, move_qty)
                    _, is_new = add_or_merge_stack(AdventurerS1Equipment, adventurer_id, quantity=move_qty, **snapshot)
                    if is_new:
                        eq_count += 1
                        if is_herb:
                            herb_count += 1
                    equipped.append(snapshot['name'])
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        if not equipped and errors:
            return (None, '；'.join(errors))
        if not is_admin and equipped:
            from plugins.adventurer_s1.services.s1_item_use_condition_service import player_clue_item_flavor_name
            clue_meta = AdventurerS1Service._batch_clue_meta_by_item_names(set(equipped))
            equipped = [player_clue_item_flavor_name(name) if name in clue_meta else name for name in equipped]
        invalidate_adventurer_detail_cache(adventurer_id)
        result = AdventurerS1Service._build_adventurer_detail_dict(adv, user_id, is_admin=is_admin)
        result['_equip_summary'] = {'equipped': equipped, 'errors': errors}
        return (result, None)

    @staticmethod
    def batch_unequip_to_warehouse(adventurer_id: int, user_id: int, is_admin: bool=False, item_ids: Optional[List[int]]=None, item_names: Optional[List[str]]=None) -> Tuple[Optional[dict], Optional[str]]:
        """批量把随身装备卸下存入仓库（每件整堆移动）。须传 item_ids；item_names 仅兼容旧客户端。

        仓库已有同属性堆则合并、不占新格；仅新建堆才计入容量。
        """
        from plugins.adventurer_s1.services.s1_item_instance_service import add_or_merge_stack, find_mergeable_stack, lock_warehouse_aggregate, resolve_equipment_item, take_from_stack
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '无权操作此冒险者')
        inv_err = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id, is_admin)
        if inv_err:
            return (None, inv_err)
        ids = list(item_ids or [])
        if not ids and item_names:
            for name in item_names:
                eq, err = resolve_equipment_item(adventurer_id, item_name=name)
                if err:
                    return (None, err)
                ids.append(eq.id)
        if not ids:
            return (None, '缺少 item_ids')
        wh_cap = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
        deposited = []
        errors = []
        try:
            with db_manager.transaction():
                lock_warehouse_aggregate(adventurer_id)
                wh_count = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adventurer_id).count()
                for eq_id in ids:
                    eq_row = AdventurerS1Equipment.query.filter_by(id=eq_id, adventurer_id=adventurer_id).populate_existing().with_for_update().first()
                    if not eq_row:
                        errors.append('未找到指定随身装备')
                        continue
                    merge_target = find_mergeable_stack(AdventurerS1WarehouseItem, adventurer_id, name=eq_row.name, item_type=eq_row.type, rarity=eq_row.rarity, custom_desc=eq_row.custom_desc, uses_left=eq_row.uses_left, evolution_stage=eq_row.evolution_stage, recovery_minutes=eq_row.recovery_minutes, recovery_started_at=eq_row.recovery_started_at, recovery_ends_at=eq_row.recovery_ends_at, is_player_locked=eq_row.is_player_locked, for_update=True)
                    if merge_target is None and wh_count >= wh_cap:
                        errors.append(f'仓库已满（{wh_cap}），无法继续存入')
                        break
                    move_qty = item_quantity_of(eq_row)
                    snapshot = dict(name=eq_row.name, item_type=eq_row.type, rarity=eq_row.rarity, custom_desc=eq_row.custom_desc, uses_left=eq_row.uses_left, evolution_stage=eq_row.evolution_stage, recovery_minutes=eq_row.recovery_minutes, recovery_started_at=eq_row.recovery_started_at, recovery_ends_at=eq_row.recovery_ends_at, is_player_locked=eq_row.is_player_locked)
                    take_from_stack(eq_row, move_qty)
                    _, is_new = add_or_merge_stack(AdventurerS1WarehouseItem, adventurer_id, quantity=move_qty, **snapshot)
                    if is_new:
                        wh_count += 1
                    deposited.append(snapshot['name'])
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        if not deposited and errors:
            return (None, '；'.join(errors))
        if not is_admin and deposited:
            from plugins.adventurer_s1.services.s1_item_use_condition_service import player_clue_item_flavor_name
            clue_meta = AdventurerS1Service._batch_clue_meta_by_item_names(set(deposited))
            deposited = [player_clue_item_flavor_name(name) if name in clue_meta else name for name in deposited]
        invalidate_adventurer_detail_cache(adventurer_id)
        result = AdventurerS1Service._build_adventurer_detail_dict(adv, user_id, is_admin=is_admin)
        result['_unequip_summary'] = {'deposited': deposited, 'errors': errors}
        return (result, None)

    @staticmethod
    def set_trait_equipped(adventurer_id: int, user_id: int, trait_id: int, *, equipped: bool) -> Tuple[Optional[dict], Optional[str]]:
        """仅允许玩家切换经独立审核确认的纯正面特质。"""
        try:
            result: dict | None = None
            domain_error: str | None = None
            with db_manager.transaction():
                adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
                domain_error = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id)
                if domain_error is None:
                    trait = AdventurerS1Trait.query.filter_by(id=trait_id, adventurer_id=adventurer_id, is_archived=False).with_for_update().first()
                    if trait is None:
                        domain_error = '该特质不存在或不属于此冒险者'
                    else:
                        from plugins.adventurer_s1.services.s1_trait_loadout_service import reviewed_trait_effect_polarity
                        from plugins.adventurer_s1.services.s1_trait_context_selection_service import TRAIT_EFFECT_POLARITY_PURE_POSITIVE
                        polarity = reviewed_trait_effect_polarity(trait)
                        if polarity != TRAIT_EFFECT_POLARITY_PURE_POSITIVE:
                            domain_error = '该特质不是经确认的纯正面特质，不能调整携带状态'
                        else:
                            trait.equipped = bool(equipped)
                            result = {'id': int(trait.id), 'name': trait.name, 'is_pure_positive': True, 'equipped': bool(equipped)}
        except Exception as exc:
            return (None, player_operation_failure(exc, '操作失败'))
        if domain_error is not None:
            return (None, domain_error)
        invalidate_adventurer_detail_cache(adventurer_id)
        return (result, None)

    @staticmethod
    def unequip_all_traits(adventurer_id: int, user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """一次卸下本人当前可卸下的特质，沿用单项操作的状态与分类边界。"""
        from plugins.adventurer_s1.services.s1_trait_loadout_service import build_trait_loadout_states
        try:
            domain_error = None
            trait_ids = []
            with db_manager.transaction():
                adv = AdventurerS1.query.filter_by(id=adventurer_id).populate_existing().with_for_update().first()
                domain_error = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id)
                if domain_error is None:
                    traits = AdventurerS1Trait.query.filter_by(adventurer_id=adventurer_id, is_archived=False).order_by(AdventurerS1Trait.id).populate_existing().with_for_update().all()
                    states = build_trait_loadout_states(traits)
                    trait_ids = [trait_id for trait_id, state in states.items() if state['is_pure_positive'] and state['equipped']]
                    if trait_ids:
                        AdventurerS1Trait.query.filter(AdventurerS1Trait.adventurer_id == adventurer_id, AdventurerS1Trait.id.in_(trait_ids), AdventurerS1Trait.is_archived.is_(False)).update({'equipped': False}, synchronize_session='fetch')
        except Exception:
            logger.exception('S1 批量卸下特质失败 adv=%s', adventurer_id)
            return (None, '特质配置失败，请稍后重试')
        if domain_error is not None:
            return (None, domain_error)
        invalidate_adventurer_detail_cache(adventurer_id)
        return ({'unequipped_count': len(trait_ids)}, None)

    @staticmethod
    def get_adventurer_skills(adventurer_id: int, user_id: int) -> Tuple[Optional[list], Optional[str]]:
        """获取冒险者全部技能（含 equipped 标记）。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if adv.owner_id != user_id:
            return (None, '无权查看此冒险者')
        from utils.time_utils import to_utc_iso
        recovery_now = _portable_utc_now()
        return ([{'id': s.id, 'name': s.name, 'description': s.description, 'equipped': bool(s.equipped), 'recovery_minutes': max(0, int(s.recovery_minutes or 0)), 'recovering': bool(s.recovery_ends_at and s.recovery_ends_at > recovery_now), **({'recovery_ends_at': to_utc_iso(s.recovery_ends_at)} if s.recovery_ends_at and s.recovery_ends_at > recovery_now else {})} for s in adv.skill_items], None)

    @staticmethod
    def equip_skill(adventurer_id: int, user_id: int, skill_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """将冒险者的某个已有技能标记为携带；若已达上限则拒绝。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if adv.owner_id != user_id:
            return (None, '无权操作此冒险者')
        inv_err = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id)
        if inv_err:
            return (None, inv_err)
        skill = AdventurerS1Skill.query.filter_by(id=skill_id, adventurer_id=adventurer_id).first()
        if not skill:
            return (None, '该技能不存在或不属于此冒险者')
        if skill.equipped:
            return ({'id': skill.id, 'name': skill.name, 'equipped': True}, None)
        max_equipped = int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5))
        equipped_count = AdventurerS1Skill.query.filter_by(adventurer_id=adventurer_id, equipped=True).count()
        if equipped_count >= max_equipped:
            return (None, f'已携带 {equipped_count} 个技能，达到上限 {max_equipped}，请先卸下其它技能')
        try:
            with db_manager.transaction():
                skill.equipped = True
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        return ({'id': skill.id, 'name': skill.name, 'equipped': True}, None)

    @staticmethod
    def unequip_skill(adventurer_id: int, user_id: int, skill_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """将冒险者的某个已携带技能放回未携带池。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if adv.owner_id != user_id:
            return (None, '无权操作此冒险者')
        inv_err = AdventurerS1Service._check_inventory_operation_allowed(adv, user_id)
        if inv_err:
            return (None, inv_err)
        skill = AdventurerS1Skill.query.filter_by(id=skill_id, adventurer_id=adventurer_id).first()
        if not skill:
            return (None, '该技能不存在或不属于此冒险者')
        if not skill.equipped:
            return ({'id': skill.id, 'name': skill.name, 'equipped': False}, None)
        try:
            with db_manager.transaction():
                skill.equipped = False
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        return ({'id': skill.id, 'name': skill.name, 'equipped': False}, None)

    @staticmethod
    def enforce_departure_carry_limits(adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """进副本前系统强制整理随身携带：超出上限的装备入仓库、技能放回未携带池。

        保留最早获得的 limit 件（按 id 升序），卸下其余。返回
        ({'equipment_moved': [...], 'skills_unequipped': [...]}, warn_or_none)。
        """
        from plugins.adventurer_s1.services.s1_item_instance_service import add_or_merge_stack, find_mergeable_stack, lock_warehouse_aggregate, take_from_stack
        equip_limit = int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5))
        max_equipped = int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5))
        wh_cap = int(current_app.config.get('ADVENTURER_S1_WAREHOUSE_CAPACITY', 100))
        equipment_moved: list[str] = []
        skills_unequipped: list[str] = []
        warnings: list[str] = []
        try:
            with db_manager.transaction():
                lock_warehouse_aggregate(adventurer_id)
                eq_rows = AdventurerS1Equipment.query.filter_by(adventurer_id=adventurer_id).order_by(AdventurerS1Equipment.id.asc()).with_for_update().all()
                excess_eq = eq_rows[equip_limit:]
                wh_count = AdventurerS1WarehouseItem.query.filter_by(adventurer_id=adventurer_id).count()
                for eq_row in excess_eq:
                    merge_target = find_mergeable_stack(AdventurerS1WarehouseItem, adventurer_id, name=eq_row.name, item_type=eq_row.type, rarity=eq_row.rarity, custom_desc=eq_row.custom_desc, uses_left=eq_row.uses_left, evolution_stage=eq_row.evolution_stage, recovery_minutes=eq_row.recovery_minutes, recovery_started_at=eq_row.recovery_started_at, recovery_ends_at=eq_row.recovery_ends_at, is_player_locked=eq_row.is_player_locked, for_update=True)
                    if merge_target is None and wh_count >= wh_cap:
                        warnings.append(f'仓库已满，无法卸下「{eq_row.name}」')
                        continue
                    move_qty = item_quantity_of(eq_row)
                    snapshot = dict(name=eq_row.name, item_type=eq_row.type, rarity=eq_row.rarity, custom_desc=eq_row.custom_desc, uses_left=eq_row.uses_left, evolution_stage=eq_row.evolution_stage, recovery_minutes=eq_row.recovery_minutes, recovery_started_at=eq_row.recovery_started_at, recovery_ends_at=eq_row.recovery_ends_at, is_player_locked=eq_row.is_player_locked)
                    take_from_stack(eq_row, move_qty)
                    _, is_new = add_or_merge_stack(AdventurerS1WarehouseItem, adventurer_id, quantity=move_qty, **snapshot)
                    if is_new:
                        wh_count += 1
                    equipment_moved.append(snapshot['name'])
                skill_rows = AdventurerS1Skill.query.filter_by(adventurer_id=adventurer_id, equipped=True).order_by(AdventurerS1Skill.id.asc()).with_for_update().all()
                for skill in skill_rows[max_equipped:]:
                    skill.equipped = False
                    skills_unequipped.append(skill.name)
        except Exception as e:
            return (None, f'整理随身携带失败：{e}')
        if not equipment_moved and (not skills_unequipped):
            return (None, None)
        invalidate_adventurer_detail_cache(adventurer_id)
        warn = '；'.join(warnings) if warnings else None
        return ({'equipment_moved': equipment_moved, 'skills_unequipped': skills_unequipped}, warn)

    @staticmethod
    def _batch_clue_meta_by_item_names(names: set[str], *, session=None) -> dict[str, dict]:
        from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_clue_meta_by_item_names
        return catalog_clue_meta_by_item_names(names, session=session)

    @staticmethod
    def _serialize_adventurer_for_client(adv: 'AdventurerS1', user_id: int | None=None, *, is_admin: bool=False) -> dict:
        item_dict, clue_meta = AdventurerS1Service._build_item_catalog_context(adv, include_warehouse=is_admin or (user_id is not None and adv.owner_id == user_id))
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
        tavern_seated = adv.id in AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id])
        from plugins.adventurer_s1.services.s1_item_use_condition_service import sanitize_adventurer_payload_items
        data = adv.to_dict(item_dict=item_dict, item_clue_meta=clue_meta, current_user_id=user_id, is_admin=is_admin, tavern_seated=tavern_seated)
        data['dorm_recovery_paused'] = bool(AdventurerS1Service._dorm_recovery_paused_adventurer_ids([adv]))
        sanitize_adventurer_payload_items(data)
        return data

    @staticmethod
    def _build_item_catalog_context(adv: 'AdventurerS1', *, include_warehouse: bool=True) -> tuple[dict, dict]:
        """从图鉴批量取描述 + 隐藏 Boss 线索公开字段（避免 N+1）。"""
        from plugins.adventurer_s1.models import AdventurerS1Item
        from plugins.adventurer_s1.services.s1_item_use_condition_service import build_clue_attach_meta
        names = [e.name for e in adv.equipment_items]
        try:
            if include_warehouse:
                names += [w.name for w in adv.warehouse_items]
        except Exception:
            pass
        if not names:
            return ({}, {})
        rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
        desc: dict[str, str] = {}
        clue_meta: dict[str, dict] = {}
        for row in rows:
            desc[row.name] = row.description or ''
            fields = build_clue_attach_meta(row)
            if fields:
                clue_meta[row.name] = fields
        return (desc, clue_meta)

    @staticmethod
    def _build_item_dict(adv: 'AdventurerS1') -> dict:
        """兼容旧调用：仅返回描述映射。"""
        desc, _ = AdventurerS1Service._build_item_catalog_context(adv)
        return desc

    @staticmethod
    def _buildings_lv2_preview_flag() -> bool:
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        return bool(S1SettingsService.get('tavern_upgrade_ui_enabled')) and int(S1SettingsService.get('tavern_level') or 1) < 2

    @staticmethod
    def _build_buildings_shared_payload(is_admin: bool) -> dict:
        """全服共享的建筑列表载荷（不含 per-user my_donations）。"""
        from plugins.adventurer_s1.seed import _TAVERN_LV2_LOCKED_CODES
        from plugins.adventurer_s1.services.s1_building_client_sanitize import sanitize_settlement_config_for_client
        from sqlalchemy.orm import joinedload, raiseload
        show_lv2_preview = AdventurerS1Service._buildings_lv2_preview_flag()
        templates = AdventurerS1BuildingTemplate.query.options(raiseload('*')).filter_by(is_active=True).order_by(AdventurerS1BuildingTemplate.sort_order).all()
        template_ids = [t.id for t in templates]
        all_instances = AdventurerS1BuildingInstance.query.options(joinedload(AdventurerS1BuildingInstance.template), raiseload('*')).filter(AdventurerS1BuildingInstance.template_id.in_(template_ids)).all() if template_ids else []
        disabled_template_ids = {i.template_id for i in all_instances if i.disabled}
        instances = [i for i in all_instances if not i.disabled and (is_admin or i.status != 'locked' or i.is_donation_open())]
        instance_map = {i.template_id: i for i in instances}
        all_instance_ids = [i.id for i in instances]
        helpers_by_inst = AdventurerS1Service._batch_building_helpers_by_inst(all_instance_ids, for_list=True)
        inst_ids_for_donation = [i.id for i in instances if i.status == 'locked' and i.template and i.template.required_materials]
        donation_totals = AdventurerS1Service._batch_summarize_donation_totals(inst_ids_for_donation, _DONATION_PURPOSE_BUILD)
        buildings = []
        for t in templates:
            if t.code in _BUILDING_CODES_HIDDEN_FROM_PLAYER_LIST:
                continue
            inst = instance_map.get(t.id)
            force_lv2_preview = show_lv2_preview and (not is_admin) and (t.code in _TAVERN_LV2_LOCKED_CODES)
            if inst and (not force_lv2_preview):
                reveal_usage = is_admin or inst.status == 'built'
                d = inst.to_dict(include_helpers=True, helpers=helpers_by_inst.get(inst.id, []), reveal_usage=reveal_usage, for_list=True, for_client=not is_admin)
                d['donations_summary'] = donation_totals.get(inst.id, {})
                d['my_donations'] = {}
                buildings.append(d)
            elif t.id in disabled_template_ids and (not force_lv2_preview):
                continue
            elif is_admin or (show_lv2_preview and t.code in _TAVERN_LV2_LOCKED_CODES):
                buildings.append({'id': None, 'template_id': t.id, 'custom_name': None, 'status': 'locked', 'disabled': False, 'remaining_seconds': 0, 'progress_percent': 0, 'total_build_seconds': t.build_hours * 3600, 'helpers': [], 'active_helpers_count': 0, 'lv2_locked_preview': bool(show_lv2_preview and t.code in _TAVERN_LV2_LOCKED_CODES), 'template': {'id': t.id, 'name': t.name, 'icon': t.icon, 'description': t.description, 'building_type': t.building_type, 'category': t.category, 'build_hours': t.build_hours, 'helper_slots': t.helper_slots, 'stamina_cost_per_help': t.stamina_cost_per_help, 'speedup_minutes': t.speedup_minutes, 'settlement_config': sanitize_settlement_config_for_client(t.settlement_config, handler_key=t.handler_key) if not is_admin else t.settlement_config, 'produced_beds': t.produced_beds, 'required_materials': t.required_materials}})
        return {'buildings': buildings, '_donation_inst_ids': inst_ids_for_donation}

    @staticmethod
    def get_buildings(user_id: Optional[int]=None, is_admin: bool=False) -> Tuple[dict, Optional[str]]:
        """获取活跃建筑模板及其实例状态。

        普通玩家只看已建成、建造中或已开放材料捐献的建筑；
        未开放的 locked 与模板占位只给管理员视图。

        全服列表走 shared 缓存（90s + 写操作/scheduler tick 失效），
        仅 per-user my_donations 在缓存命中后补 1 条聚合查询。
        """
        cache_key = f'buildings:{user_id or 0}:{int(is_admin)}'
        cached = _buildings_response_cache.get(cache_key)
        if cached is not None:
            return (cached, None)
        try:
            preview_flag = int(AdventurerS1Service._buildings_lv2_preview_flag())
        finally:
            db_manager.remove()
        shared_key = f'shared:{int(is_admin)}:{preview_flag}'
        shared = _buildings_shared_cache.get(shared_key)
        if shared is None:
            with _buildings_shared_build_lock:
                shared = _buildings_shared_cache.get(shared_key)
                if shared is None:
                    try:
                        shared = AdventurerS1Service._build_buildings_shared_payload(is_admin)
                    finally:
                        db_manager.remove()
                    _buildings_shared_cache.set(shared_key, shared)
        donation_inst_ids = shared.get('_donation_inst_ids') or []
        my_map: dict[int, dict[str, int]] = {}
        if user_id and donation_inst_ids:
            try:
                my_map = AdventurerS1Service._batch_summarize_donation_totals(donation_inst_ids, _DONATION_PURPOSE_BUILD, user_id)
            finally:
                db_manager.remove()
        if my_map:
            buildings = []
            for row in shared['buildings']:
                inst_id = row.get('id')
                if inst_id and inst_id in my_map:
                    patched = dict(row)
                    patched['my_donations'] = my_map[inst_id]
                    buildings.append(patched)
                else:
                    buildings.append(row)
            payload = {'buildings': buildings}
        else:
            payload = {'buildings': shared['buildings']}
        _buildings_response_cache.set(cache_key, payload)
        return (payload, None)

    @staticmethod
    def get_all_building_instances_for_admin() -> Tuple[Optional[list], Optional[str]]:
        """管理员查看所有建筑实例（含已禁用），按模板分组返回。"""
        instances = AdventurerS1BuildingInstance.query.order_by(AdventurerS1BuildingInstance.template_id, AdventurerS1BuildingInstance.id).all()
        result = []
        for inst in instances:
            result.append(inst.to_dict())
        return (result, None)

    @staticmethod
    def toggle_building_disabled(instance_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """GM 切换建筑实例的禁用状态。"""
        try:
            with db_manager.transaction():
                inst = AdventurerS1BuildingInstance.query.filter_by(id=instance_id).populate_existing().with_for_update().first()
                if not inst:
                    return (None, '建筑不存在')
                next_disabled = not bool(inst.disabled)
                if next_disabled and AdventurerS1Service._building_usage_queue_waiting_count(inst.id) > 0:
                    return (None, '该建筑仍有冒险者候场，请等待递补完成或由玩家取消候场后再禁用')
                if next_disabled and AdventurerS1Service._queueable_building_active_usage_count(inst) > 0:
                    return (None, '该建筑仍有冒险者正在使用，请等待本次使用结束后再禁用')
                inst.disabled = next_disabled
        except Exception as exc:
            return (None, f'操作失败: {exc}')
        invalidate_buildings_list_cache()
        if not inst.disabled:
            from plugins.adventurer_s1.services.s1_building_usage_queue_service import S1BuildingUsageQueueService
            S1BuildingUsageQueueService.promote_waiting_for_building(inst.id)
        return ({'id': inst.id, 'disabled': inst.disabled}, None)

    @staticmethod
    def _building_usage_queue_waiting_count(building_instance_id: int) -> int:
        """读取通用设施候场数；状态变更入口需在建筑行锁内调用。"""
        from plugins.adventurer_s1.services.s1_building_usage_queue_service import S1BuildingUsageQueueService
        return int(S1BuildingUsageQueueService.get_queue_state(building_instance_id)['waiting_count'])

    @staticmethod
    def _queueable_building_active_usage_count(inst: AdventurerS1BuildingInstance) -> int:
        """只统计通用候场设施的真实在用者，不把候场记录算作占位。"""
        from plugins.adventurer_s1.services.s1_building_usage_queue_service import S1BuildingUsageQueueService
        handler_key = inst.template.handler_key if inst.template else None
        if inst.status != 'built' or not S1BuildingUsageQueueService.is_queue_handler(handler_key):
            return 0
        return int(AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').count())

    @staticmethod
    def _queueable_building_usage_counts_for_instances(instances: list[AdventurerS1BuildingInstance], handler_key: Optional[str]) -> tuple[int, int]:
        """批量返回模板下 (候场人数, 在用人数)，避免逐实例 COUNT。"""
        from plugins.adventurer_s1.services.s1_building_usage_queue_service import S1BuildingUsageQueueService
        instance_ids = [int(inst.id) for inst in instances]
        waiting_count = S1BuildingUsageQueueService.get_waiting_count_for_buildings(instance_ids)
        if not S1BuildingUsageQueueService.is_queue_handler(handler_key):
            return (waiting_count, 0)
        built_ids = [int(inst.id) for inst in instances if inst.status == 'built']
        if not built_ids:
            return (waiting_count, 0)
        active_count = AdventurerS1.query.filter(AdventurerS1.building_instance_id.in_(built_ids), AdventurerS1.status == 'building').count()
        return (waiting_count, int(active_count))

    @staticmethod
    def get_building_detail(building_instance_id: int, user_id: Optional[int]=None, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """获取单个建筑详情（含帮忙者列表 + 今日使用次数）。"""
        from sqlalchemy.orm import joinedload
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id, options=(joinedload(AdventurerS1BuildingInstance.template),))
        donation_open = inst is not None and inst.is_donation_open()
        if not inst or inst.disabled or (inst.status == 'locked' and (not is_admin) and (not donation_open)):
            return (None, '建筑不存在')
        helpers = None
        if inst.status in ('building', 'upgrading'):
            helpers = AdventurerS1.query.filter(AdventurerS1.building_instance_id == inst.id, AdventurerS1.status == 'building').all()
        my_adv = None
        low_consumption_balances = None
        today_usage_count = None
        cooldown_available_at = None
        if user_id:
            my_adv = db_manager.query(AdventurerS1.id, AdventurerS1.name, AdventurerS1.status, AdventurerS1.gold, AdventurerS1.stamina, AdventurerS1.hp, AdventurerS1.max_hp, AdventurerS1.stamina_updated_at, AdventurerS1.hp_updated_at, AdventurerS1.level, AdventurerS1.training_count, AdventurerS1.building_instance_id, AdventurerS1.building_usage_extra).filter_by(owner_id=user_id, is_player_created=True).first()
            if my_adv and inst.status == 'built':
                from plugins.adventurer_s1.models import AdventurerS1BuildingUsageLog
                from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService
                now = _portable_utc_now()
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                usage_config = BuildingSettlementService.get_reward_config(inst)
                cooldown_hours = AdventurerS1Service._effective_building_cooldown_hours(inst, usage_config)
                lookback_start = min(today_start, now - timedelta(hours=cooldown_hours)) if cooldown_hours > 0 else today_start
                usage_summary = db_manager.query(func.sum(case((AdventurerS1BuildingUsageLog.used_at >= today_start, 1), else_=0)), func.max(AdventurerS1BuildingUsageLog.used_at)).filter(AdventurerS1BuildingUsageLog.adventurer_id == my_adv[0], AdventurerS1BuildingUsageLog.building_instance_id == building_instance_id, AdventurerS1BuildingUsageLog.used_at >= lookback_start).one()
                today_usage_count = int(usage_summary[0] or 0)
                latest_used_at = usage_summary[1]
                if cooldown_hours > 0 and latest_used_at:
                    next_available_at = latest_used_at + timedelta(hours=cooldown_hours)
                    if next_available_at > now:
                        cooldown_available_at = next_available_at
            if my_adv and inst.status == 'built' and inst.template:
                from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
                if S1CurrencyService.is_low_consumption_facility(inst.template.handler_key):
                    low_consumption_balances, balance_err = S1CurrencyService.get_low_consumption_payment_balances(my_adv[0])
                    if balance_err:
                        return (None, balance_err)
        result = inst.to_dict(include_helpers=True, helpers=helpers, reveal_usage=is_admin or inst.status == 'built', for_client=not is_admin)
        if today_usage_count is not None:
            result['today_usage_count'] = today_usage_count
        if my_adv and inst.status == 'built':
            from utils.time_utils import to_utc_iso
            result['cooldown_available_at'] = to_utc_iso(cooldown_available_at) if cooldown_available_at else None
        tpl = inst.template
        result['my_craft_inputs'] = []
        if my_adv and tpl and (tpl.handler_key == 'blacksmith') and (inst.status == 'built') and (my_adv.building_instance_id == inst.id) and (my_adv.status == 'building'):
            usage_extra = my_adv.building_usage_extra
            refs = usage_extra.get('item_refs') if isinstance(usage_extra, dict) else None
            if isinstance(refs, list):
                result['my_craft_inputs'] = [{'name': ref['name'], 'quantity': ref.get('quantity', 1)} for ref in refs if isinstance(ref, dict) and isinstance(ref.get('name'), str) and isinstance(ref.get('quantity', 1), int) and (not isinstance(ref.get('quantity', 1), bool)) and (ref.get('quantity', 1) > 0)]
        if tpl and inst.status == 'built':
            from plugins.adventurer_s1.services.s1_building_usage_queue_service import S1BuildingUsageQueueService
            if S1BuildingUsageQueueService.is_queue_handler(tpl.handler_key):
                result['usage_queue'] = S1BuildingUsageQueueService.get_queue_state(inst.id, int(my_adv[0]) if my_adv else None)
        if tpl and tpl.handler_key == 'clue_bureau' and (inst.status == 'built'):
            from plugins.adventurer_s1.services.s1_clue_bureau_service import S1ClueBureauService
            result['clue_bureau'] = S1ClueBureauService.summarize_pool(for_client=not is_admin)
        if my_adv and inst.status == 'built':
            from flask import current_app
            from plugins.adventurer_s1.services.adventurer_s1_building_handler import get_training_cap
            max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
            result['my_adventurer'] = {'id': my_adv[0], 'name': my_adv[1], 'status': my_adv[2], 'gold': float(my_adv[3] or 0), 'stamina': int(my_adv[4] or 0), 'max_stamina': max_stamina, 'hp': int(my_adv[5] or 0), 'max_hp': int(my_adv[6] or 100), 'stamina_updated_at': to_utc_iso(my_adv[7]) if my_adv[7] else None, 'hp_updated_at': to_utc_iso(my_adv[8]) if my_adv[8] else None, 'training_count': int(my_adv[10] or 0), 'training_cap': get_training_cap(my_adv[9])}
            if low_consumption_balances is not None:
                result['my_adventurer'].update({'low_consumption_gold': low_consumption_balances['low_consumption_balance'], 'low_consumption_gold_cap': low_consumption_balances['cap']})
        if inst.status == 'built':
            AdventurerS1Service._attach_building_upgrade_fields(result, inst, user_id)
        return (result, None)

    @staticmethod
    def _fetch_building_contribution_data(inst: AdventurerS1BuildingInstance, purpose: Optional[str]=None) -> Tuple[list, list, dict, dict]:
        """一次取齐贡献排行所需数据：(helpers, donations, adv_info, effective_by_helper)。

        purpose: None=全部阶段合计；'build'/'upgrade'=仅该阶段（便于分阶段统计）。

        adv_info 是 {adventurer_id: Row(id, name, owner_id)} 批量映射，
        供各排行共用，避免逐行触发 relationship 的 N+1。

        有效贡献秒数：contributed_seconds 精确记账优先（同阶段帮手之和 == 该阶段总工期）；
        NULL 的历史记录按「在场时长 × speedup」估算，截断到建筑完工时刻后，
        再按 purpose 分组等比压缩进「该阶段总工期 - 精确记账之和」。
        """
        from collections import defaultdict
        from extensions import db
        from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
        helpers_q = AdventurerS1BuildingHelper.query.filter_by(building_instance_id=inst.id)
        donations_q = AdventurerS1BuildingDonation.query.filter_by(building_instance_id=inst.id)
        if purpose is not None:
            helpers_q = helpers_q.filter_by(purpose=purpose)
            donations_q = donations_q.filter_by(purpose=purpose)
        helpers = helpers_q.all()
        donations = donations_q.all()
        adv_ids = {h.adventurer_id for h in helpers} | {d.adventurer_id for d in donations if d.adventurer_id}
        adv_info = {}
        if adv_ids:
            rows = db.session.query(AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id).filter(AdventurerS1.id.in_(adv_ids)).all()
            adv_info = {r.id: r for r in rows}
        now = _portable_utc_now()
        speedup_minutes = inst.template.speedup_minutes if inst.template and inst.template.speedup_minutes else 60
        cap_time = inst.build_complete_at if inst.status == 'built' and inst.build_complete_at else None
        effective_by_helper: dict[int, float] = {}
        for h in helpers:
            if h.contributed_seconds is not None:
                effective_by_helper[h.id] = float(h.contributed_seconds)
                continue
            end_time = h.left_at or now
            if cap_time and end_time > cap_time:
                end_time = cap_time
            duration_seconds = max(0.0, (end_time - h.helped_at).total_seconds())
            effective_by_helper[h.id] = duration_seconds * speedup_minutes / 60.0
        purpose_groups: dict[str, list] = defaultdict(list)
        for h in helpers:
            purpose_groups[AdventurerS1Service._helper_record_purpose(h)].append(h)
        for ph, ph_helpers in purpose_groups.items():
            precise_total = sum((effective_by_helper[h.id] for h in ph_helpers if h.contributed_seconds is not None))
            estimated_ids = [h.id for h in ph_helpers if h.contributed_seconds is None]
            if not estimated_ids:
                continue
            total_build_seconds = AdventurerS1Service._phase_total_seconds(inst, ph)
            estimate_budget = max(0.0, total_build_seconds - precise_total)
            raw_sum = sum((effective_by_helper[hid] for hid in estimated_ids))
            if raw_sum > estimate_budget and raw_sum > 0:
                scale = estimate_budget / raw_sum
                for hid in estimated_ids:
                    effective_by_helper[hid] *= scale
        return (helpers, donations, adv_info, effective_by_helper)

    @staticmethod
    def _build_combined_ranking(helpers: list, donations: list, adv_info: dict, effective_by_helper: dict) -> List[dict]:
        """按玩家归并的综合贡献排行（2026-06-12 拍板，2026-07-03 材料按等效体力加权）。

        综合分 = 工时占比 × 50% + 材料占比 × 50%：
        - 工时占比 = 玩家名下帮手的有效贡献秒数 / 全体有效贡献秒数之和
        - 材料占比 = 玩家加权材料贡献 / 全部加权材料贡献之和
          （与 S1ContributionService.donation_contribution 同源：木/石 3/个，铁锭 67/个等）
        分母取实际发生量而非模板理论值，GM 强制完工（工时未满额）时占比依然成立。
        并列时最早开始贡献（首笔帮工进场或捐献）者在前。
        无主贡献（owner 为空）计入分母但 user_id 为 None，不参与命名权。
        """
        from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
        buckets: dict = {}

        def _bucket(uid):
            if uid not in buckets:
                buckets[uid] = {'user_id': uid, 'display_name': None, 'seconds': 0.0, 'material_amount': 0, 'first_at': None}
            return buckets[uid]

        def _touch_first(bucket, at):
            if at and (bucket['first_at'] is None or at < bucket['first_at']):
                bucket['first_at'] = at
        total_seconds = 0.0
        for h in helpers:
            secs = effective_by_helper.get(h.id, 0.0)
            info = adv_info.get(h.adventurer_id)
            bucket = _bucket(info.owner_id if info else None)
            bucket['seconds'] += secs
            total_seconds += secs
            if info and (not bucket['display_name']):
                bucket['display_name'] = info.name
            _touch_first(bucket, h.helped_at)
        total_material = 0
        for d in donations:
            info = adv_info.get(d.adventurer_id) if d.adventurer_id else None
            uid = d.user_id if d.user_id is not None else info.owner_id if info else None
            bucket = _bucket(uid)
            weighted = S1ContributionService.donation_contribution(d.material_name, int(d.amount or 0))
            bucket['material_amount'] += weighted
            total_material += weighted
            if info and (not bucket['display_name']):
                bucket['display_name'] = info.name
            _touch_first(bucket, d.created_at)
        ranked = []
        for bucket in buckets.values():
            time_share = bucket['seconds'] / total_seconds if total_seconds > 0 else 0.0
            material_share = bucket['material_amount'] / total_material if total_material > 0 else 0.0
            score = time_share * 0.5 + material_share * 0.5
            ranked.append({'user_id': bucket['user_id'], 'display_name': bucket['display_name'] or '未知', 'effective_hours': round(bucket['seconds'] / 3600, 2), 'material_amount': bucket['material_amount'], 'time_share': round(time_share, 4), 'material_share': round(material_share, 4), 'score': round(score, 4), '_sort_score': score, '_first_at': bucket['first_at'] or datetime.max})
        ranked.sort(key=lambda x: (-x['_sort_score'], x['_first_at']))
        for row in ranked:
            row.pop('_sort_score', None)
            row.pop('_first_at', None)
        return ranked

    @staticmethod
    def _pick_dorm_upgrade_naming_winner(inst: AdventurerS1BuildingInstance, room: 'AdventurerS1DormRoom') -> Optional[int]:
        """升级完工时：在住客中判定本次升级（purpose=upgrade）综合贡献最大的玩家。

        仅统计当前占用床位的玩家冒险者所属 user_id；非住客即使升级贡献更高也不参与。
        """
        resident_user_ids = {adv.owner_id for adv in AdventurerS1.query.filter(AdventurerS1.dorm_room_id == room.id, AdventurerS1.bed_index.isnot(None), AdventurerS1.owner_id.isnot(None), AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).all()}
        if not resident_user_ids:
            return None
        helpers, donations, adv_info, effective_by_helper = AdventurerS1Service._fetch_building_contribution_data(inst, purpose=_DONATION_PURPOSE_UPGRADE)
        ranked = AdventurerS1Service._build_combined_ranking(helpers, donations, adv_info, effective_by_helper)
        for row in ranked:
            uid = row['user_id']
            if uid is not None and uid in resident_user_ids and (row['score'] > 0):
                return uid
        return None

    @staticmethod
    def _dorm_room_instance_owned_by_other(room: 'AdventurerS1DormRoom', instance_id: int) -> bool:
        """同一建筑实例只能绑定一间宿舍（历史脏数据会误绑到别家实例）。"""
        return AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.building_instance_id == instance_id, AdventurerS1DormRoom.id != room.id).first() is not None

    @staticmethod
    def _finalize_dormitory_upgrade_rooms(inst: 'AdventurerS1BuildingInstance', now: Optional[datetime]=None) -> Tuple[Optional[int], Optional[int]]:
        """宿舍升级完工：同步所有关联房间的 level，恢复床位计时，授予命名权。"""
        dorm_rooms = AdventurerS1DormRoom.query.filter_by(building_instance_id=inst.id).order_by(AdventurerS1DormRoom.id).all()
        if not dorm_rooms:
            return (None, None)
        primary_room_id = dorm_rooms[0].id
        upgrade_naming_user_id = None
        for dorm_room in dorm_rooms:
            if int(dorm_room.level or 1) < 2:
                dorm_room.level = 2
                AdventurerS1Service._resume_dorm_room_recovery_after_upgrade(dorm_room, now)
            winner = AdventurerS1Service._apply_dorm_upgrade_naming(dorm_room, inst)
            if upgrade_naming_user_id is None and winner is not None:
                upgrade_naming_user_id = winner
        return (primary_room_id, upgrade_naming_user_id)

    @staticmethod
    def _apply_dorm_upgrade_naming(room: 'AdventurerS1DormRoom', inst: AdventurerS1BuildingInstance) -> Optional[int]:
        """升级完工后授予住客中的升级贡献冠军一次性命名权（幂等）。"""
        if room.upgrade_naming_user_id is not None:
            return room.upgrade_naming_user_id
        winner = None
        try:
            winner = AdventurerS1Service._pick_dorm_upgrade_naming_winner(inst, room)
        except Exception:
            logger.exception('宿舍升级命名权判定失败: room_id=%s instance_id=%s', room.id, inst.id)
        room.upgrade_naming_user_id = winner
        return winner

    @staticmethod
    def _pick_dorm_naming_winner(inst: AdventurerS1BuildingInstance) -> Optional[int]:
        """完工时判定综合贡献最大的玩家（宿舍命名权获得者）。

        无任何有效玩家贡献（如 GM 零帮工零捐献强制完工）返回 None。
        """
        helpers, donations, adv_info, effective_by_helper = AdventurerS1Service._fetch_building_contribution_data(inst, purpose=_DONATION_PURPOSE_BUILD)
        ranked = AdventurerS1Service._build_combined_ranking(helpers, donations, adv_info, effective_by_helper)
        for row in ranked:
            if row['user_id'] is not None and row['score'] > 0:
                return row['user_id']
        return None

    @staticmethod
    def _format_contributors_response(helpers: list, donations: list, adv_info: dict, effective_by_helper: dict) -> dict:
        """将贡献明细格式化为排行 API 结构。"""
        from collections import defaultdict
        from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
        time_map: dict[int, dict] = {}
        for h in helpers:
            if h.adventurer_id not in time_map:
                info = adv_info.get(h.adventurer_id)
                time_map[h.adventurer_id] = {'adventurer_id': h.adventurer_id, 'adventurer_name': info.name if info else '未知', 'effective_hours': 0.0}
            time_map[h.adventurer_id]['effective_hours'] += effective_by_helper[h.id] / 3600
        for v in time_map.values():
            v['effective_hours'] = round(v['effective_hours'], 2)
        time_contributors = sorted(time_map.values(), key=lambda x: x['effective_hours'], reverse=True)
        mat_map: dict[int, dict] = {}
        for d in donations:
            key = d.adventurer_id or 0
            if key not in mat_map:
                info = adv_info.get(d.adventurer_id) if d.adventurer_id else None
                mat_map[key] = {'adventurer_id': d.adventurer_id, 'adventurer_name': info.name if info else '未知', 'user_id': d.user_id, 'materials': defaultdict(int), 'total_amount': 0}
            mat_map[key]['materials'][d.material_name] += d.amount
            mat_map[key]['total_amount'] += S1ContributionService.donation_contribution(d.material_name, int(d.amount or 0))
        material_contributors = sorted([{'adventurer_id': v['adventurer_id'], 'adventurer_name': v['adventurer_name'], 'user_id': v['user_id'], 'materials': dict(v['materials']), 'total_amount': v['total_amount']} for v in mat_map.values()], key=lambda x: x['total_amount'], reverse=True)
        combined_contributors = AdventurerS1Service._build_combined_ranking(helpers, donations, adv_info, effective_by_helper)
        return {'time_contributors': time_contributors, 'material_contributors': material_contributors, 'combined_contributors': combined_contributors}

    @staticmethod
    def get_building_contributors(building_instance_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """获取建筑贡献者排行：有效帮忙时长、材料捐献、按玩家归并的综合占比。

        顶层字段为 build+upgrade 合计；by_purpose 按阶段分开，便于后续统计。
        """
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        helpers, donations, adv_info, effective_by_helper = AdventurerS1Service._fetch_building_contribution_data(inst)
        total = AdventurerS1Service._format_contributors_response(helpers, donations, adv_info, effective_by_helper)
        by_purpose = {}
        for purpose in (_DONATION_PURPOSE_BUILD, _DONATION_PURPOSE_UPGRADE):
            ph_helpers, ph_donations, ph_adv_info, ph_effective = AdventurerS1Service._fetch_building_contribution_data(inst, purpose=purpose)
            by_purpose[purpose] = AdventurerS1Service._format_contributors_response(ph_helpers, ph_donations, ph_adv_info, ph_effective)
        return ({**total, 'by_purpose': by_purpose}, None)

    @staticmethod
    def start_building(building_instance_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """管理员手动开始建造。"""
        if not is_admin:
            return (None, '仅管理员可操作')
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.status != 'locked':
            return (None, f'建筑当前状态为「{inst.status}」，无法开始建造')
        required = inst.template.required_materials if inst.template else None
        if required:
            from extensions import db
            from sqlalchemy import func as _func
            from plugins.adventurer_s1.models import AdventurerS1BuildingDonation
            totals = dict(db.session.query(AdventurerS1BuildingDonation.material_name, _func.sum(AdventurerS1BuildingDonation.amount)).filter(AdventurerS1BuildingDonation.building_instance_id == inst.id, AdventurerS1BuildingDonation.purpose == _DONATION_PURPOSE_BUILD).group_by(AdventurerS1BuildingDonation.material_name).all())
            from plugins.adventurer_s1.services.adventurer_s1_material_service import MATERIAL_META
            missing = []
            for mat_code, need in required.items():
                have = int(totals.get(mat_code, 0) or 0)
                if have < int(need):
                    display = MATERIAL_META.get(mat_code, {}).get('name', mat_code)
                    missing.append(f'{display}（{have}/{int(need)}）')
            if missing:
                inst.disabled = False
                if inst.build_started_at is None:
                    inst.build_started_at = _portable_utc_now()
                ok, err = db_manager.commit()
                if not ok:
                    return (None, f'操作失败：{err}')
                data = inst.to_dict()
                data['waiting_for_donations'] = True
                data['missing_materials'] = missing
                invalidate_buildings_list_cache()
                return (data, None)
        now = _portable_utc_now()
        build_hours = inst.template.build_hours if inst.template else 24
        inst.status = 'building'
        inst.disabled = False
        inst.build_started_at = now
        inst.build_complete_at = now + timedelta(hours=build_hours)
        inst.accumulated_progress_seconds = 0
        ok, err = db_manager.commit()
        if not ok:
            return (None, f'操作失败：{err}')
        invalidate_buildings_list_cache()
        return (inst.to_dict(), None)

    @staticmethod
    def help_building(building_instance_id: int, adventurer_id: int, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """冒险者帮忙建造/升级：派遣冒险者到建筑，持续消耗精力、增加进度。"""
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.status not in ('building', 'upgrading'):
            return (None, '建筑不在建造/升级中')
        if inst.template and (not inst.template.is_active):
            return (None, '该建筑已下架')
        if not is_admin and AdventurerS1Service._is_tavern_lv2_building_locked(inst):
            return (None, '该建筑需酒馆升至 Lv2 后才能建造')
        now = _portable_utc_now()
        try:
            AdventurerS1Service._tick_building_progress(inst)
        except (OperationalError, PendingRollbackError):
            db_manager.rollback()
            logger.info('派遣帮工 tick 建筑进度遇到死锁，已回滚')
        if inst.status == 'built':
            return (None, '建筑已建造/升级完成')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '只能派遣自己拥有的冒险者')
        can, reason = adv.can_dispatch()
        if not can:
            return (None, reason)
        max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
        recovery_per_minute = int(current_app.config.get('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1))
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
        current_stamina = get_live_stamina(adv, max_stamina=max_stamina, recovery_per_minute=recovery_per_minute)
        if current_stamina < 1:
            return (None, f'精力不足（当前 {current_stamina}）')
        try:
            with db_manager.transaction():
                locked_adv = AdventurerS1.query.filter_by(id=adventurer_id).populate_existing().with_for_update().first()
                if not locked_adv:
                    return (None, '冒险者不存在')
                can, reason = locked_adv.can_dispatch()
                if not can:
                    return (None, reason)
                adv = locked_adv
                locked_inst = AdventurerS1BuildingInstance.query.filter_by(id=inst.id).populate_existing().with_for_update().first()
                if not locked_inst or locked_inst.status not in ('building', 'upgrading'):
                    return (None, '建筑不在建造/升级中')
                inst = locked_inst
                if inst.template and inst.template.helper_slots > 0:
                    helper_cnt = AdventurerS1.query.filter_by(building_instance_id=inst.id).count()
                    if helper_cnt >= inst.template.helper_slots:
                        return (None, f'帮忙位已满（{helper_cnt}/{inst.template.helper_slots}）')
                AdventurerS1StateTransitionCoordinator.leave_rest(adv, 'building', now)
                adv.building_instance_id = inst.id
                adv.building_assigned_at = now
                helper_purpose = AdventurerS1Service._purpose_from_building_status(inst.status)
                helper = AdventurerS1BuildingHelper(building_instance_id=inst.id, adventurer_id=adventurer_id, helped_at=now, contributed_seconds=0, purpose=helper_purpose)
                db_manager.add(helper)
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        helpers = AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').all()
        AdventurerS1Service._update_build_eta(inst, helpers=helpers, now=now)
        ok, err = db_manager.commit()
        if not ok:
            return (None, f'操作失败：{err}')
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'building_assigned', 'reason': 'help_building', 'ids': [adventurer_id]}, namespace='/')
        except Exception:
            pass
        invalidate_buildings_list_cache()
        if inst.template and inst.template.category == 'dormitory':
            invalidate_dormitory_cache()
        return ({'building': inst.to_dict(include_helpers=True), 'adventurer_stamina': adv.stamina, 'message': f'{adv.name} 已派遣到建筑'}, None)

    @staticmethod
    def _built_usage_recall_block_reason(adv: 'AdventurerS1', inst: Optional['AdventurerS1BuildingInstance']) -> Optional[str]:
        """已建成建筑的功能使用中禁止 recall（医院/疗养类除外）。"""
        if not inst or inst.status != 'built':
            return None
        if adv.status != 'building' or adv.building_instance_id != inst.id:
            return None
        handler_key = inst.template.handler_key if inst.template else None
        if handler_key in AdventurerS1Service.BUILDING_USAGE_EARLY_EXIT_HANDLERS:
            return None
        if handler_key == 'skill_school':
            return f'「{adv.name}」还在百艺阁跟导师学艺，等这堂课结束再来找他吧'
        label = inst.template.name if inst.template and inst.template.name else '建筑'
        _USAGE_LABELS = {'blacksmith': '铁匠铺锻造', 'tailor_workshop': '绮罗裁坊裁制', 'alchemy_workshop': '炼金工房', 'ember_kitchen': '余烬灶坊烹饪', 'healing_house': '治疗所', 'chapel': '小圣堂', 'pet_evolution': '嬗变祭坛', 'training': '修炼', 'dining_hall': '用餐'}
        if handler_key in _USAGE_LABELS:
            label = _USAGE_LABELS[handler_key]
        return f'「{adv.name}」正在{label}中，请等待结束后再操作'

    @staticmethod
    def _try_auto_complete_built_usage_before_recall(adv: 'AdventurerS1', inst: 'AdventurerS1BuildingInstance', user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """若 usage 时长已满则优先走 complete，避免 recall 吞掉已扣费用。"""
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService
        handler_key = inst.template.handler_key if inst.template else None
        config = BuildingSettlementService.get_reward_config(inst)
        duration_minutes = AdventurerS1Service._effective_usage_duration_minutes(adv, config)
        usage_start = AdventurerS1Service._effective_usage_start_at(adv, handler_key)
        if duration_minutes <= 0 or not usage_start:
            return (None, None)
        elapsed_seconds = (_portable_utc_now() - usage_start).total_seconds()
        if elapsed_seconds < duration_minutes * 60:
            return (None, None)
        stored_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else None
        return AdventurerS1Service.complete_building_usage(adventurer_id=adv.id, building_instance_id=inst.id, user_id=user_id, is_admin=is_admin, extra_params=stored_extra)

    @staticmethod
    def _refund_building_usage_input_materials(adventurer_id: int, building_instance_id: int, assigned_at: Optional[datetime], config: dict, description: str) -> int:
        """退还已建成建筑使用开始时扣除的 input_materials。

        产业链建筑（如炭窑/熔铁坊）在 start 阶段扣输入材料、complete 阶段发产物。
        若会话被异常中断，只退金币/精力会吞掉输入材料；这里按本次 start 后的
        building_usage_cost 材料流水补退，并扣除同窗口内已退过的数量以保证幂等。
        """
        input_materials = {str(k): int(v) for k, v in (config.get('input_materials') or {}).items() if int(v) > 0}
        if not input_materials or not assigned_at:
            return 0
        window_start = assigned_at - timedelta(seconds=5)
        related_id = str(building_instance_id)
        material_names = list(input_materials.keys())
        cost_rows = db.session.query(AdventurerS1MaterialLog.name, db.func.coalesce(db.func.sum(-AdventurerS1MaterialLog.change_amount), 0)).filter(AdventurerS1MaterialLog.adventurer_id == adventurer_id, AdventurerS1MaterialLog.related_id == related_id, AdventurerS1MaterialLog.related_type == 'building_instance', AdventurerS1MaterialLog.source_type == 'building_usage_cost', AdventurerS1MaterialLog.change_amount < 0, AdventurerS1MaterialLog.created_at >= window_start, AdventurerS1MaterialLog.name.in_(material_names)).group_by(AdventurerS1MaterialLog.name).all()
        cost_by_name = {name: int(amount or 0) for name, amount in cost_rows}
        if not cost_by_name:
            return 0
        refunded_rows = db.session.query(AdventurerS1MaterialLog.name, db.func.coalesce(db.func.sum(AdventurerS1MaterialLog.change_amount), 0)).filter(AdventurerS1MaterialLog.adventurer_id == adventurer_id, AdventurerS1MaterialLog.related_id == related_id, AdventurerS1MaterialLog.related_type == 'building_instance', AdventurerS1MaterialLog.source_type == 'building_usage_abort_refund', AdventurerS1MaterialLog.change_amount > 0, AdventurerS1MaterialLog.created_at >= window_start, AdventurerS1MaterialLog.name.in_(material_names)).group_by(AdventurerS1MaterialLog.name).all()
        refunded_by_name = {name: int(amount or 0) for name, amount in refunded_rows}
        from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
        refunded_total = 0
        for name, cost_amount in cost_by_name.items():
            refund_amount = max(0, int(cost_amount) - int(refunded_by_name.get(name, 0) or 0))
            if refund_amount <= 0:
                continue
            _, refund_err = S1MaterialService.add(adventurer_id, name, refund_amount, source_type='building_usage_abort_refund', related_id=building_instance_id, related_type='building_instance', description=description, auto_commit=False)
            if refund_err:
                raise RuntimeError(refund_err)
            refunded_total += refund_amount
        return refunded_total

    @staticmethod
    def _abort_orphaned_built_usage(adventurer_id: int, building_instance_id: int) -> bool:
        """resting 态仍挂 building_instance_id 的已建成 usage 占位：退还 start 费用并清场。"""
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst or inst.status != 'built' or (not inst.template):
            return False
        handler_key = inst.template.handler_key
        if handler_key in AdventurerS1Service.BUILDING_USAGE_EARLY_EXIT_HANDLERS:
            return False
        from plugins.adventurer_s1.services.adventurer_s1_building_ai_recovery_service import AdventurerS1BuildingAiRecoveryService
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv or adv.building_instance_id != building_instance_id:
            return False
        if adv.status == 'building':
            usage_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
            usage_session_id = str(usage_extra.get('usage_session_id') or '') if handler_key in ('skill_school', 'item_assembly_workshop', 'ember_kitchen') else None
            return AdventurerS1BuildingAiRecoveryService.abort_stuck_session(adventurer_id, building_instance_id, handler_key, reason='building_usage_orphan_abort', usage_session_id=usage_session_id)
        if adv.status not in AdventurerS1Service.FREE_STATUSES:
            return False
        assigned_at = adv.building_assigned_at
        if not assigned_at:
            return False
        try:
            with db_manager.transaction():
                locked_adv = AdventurerS1.query.filter_by(id=adventurer_id).populate_existing().with_for_update().first()
                if not locked_adv or locked_adv.building_instance_id != building_instance_id or locked_adv.status not in AdventurerS1Service.FREE_STATUSES:
                    return False
                gold_refund = AdventurerS1BuildingAiRecoveryService._resolve_start_gold_refund(adventurer_id, building_instance_id, assigned_at)
                from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService
                config = BuildingSettlementService.get_reward_config(inst)
                extra = locked_adv.building_usage_extra if isinstance(locked_adv.building_usage_extra, dict) else {}
                if gold_refund <= 0:
                    gold_refund = float(extra.get('gold_cost') or config.get('gold_cost') or 0)
                if gold_refund > 0:
                    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
                    building_name = inst.template.name if inst.template else '建筑'
                    refund_description = '百艺阁授艺未成，退回束脩' if handler_key == 'skill_school' else f'「{building_name}」异常中断，退还使用费用'
                    _, refund_err = S1CurrencyService.add(adventurer_id, 'gold', float(gold_refund), source_type='building_usage_abort_refund', related_id=building_instance_id, related_type='building_instance', description=refund_description, auto_commit=False)
                    if refund_err:
                        raise RuntimeError(refund_err)
                stamina_cost = int(extra.get('stamina_cost') or config.get('usage_stamina_cost') or 0)
                if stamina_cost > 0:
                    max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
                    locked_adv.stamina = min(max_stamina, int(locked_adv.stamina or 0) + stamina_cost)
                AdventurerS1Service._refund_building_usage_input_materials(adventurer_id, building_instance_id, assigned_at, config, description=f"「{(inst.template.name if inst.template else '建筑')}」异常中断，退还投入材料")
                from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
                S1SkillSchoolService.release_usage_slot(locked_adv.id, now=_portable_utc_now())
                locked_adv.building_instance_id = None
                locked_adv.building_assigned_at = None
                locked_adv.building_usage_extra = None
            return True
        except Exception:
            logger.exception('已建成 usage 孤儿占位中止失败 adv=%s inst=%s', adventurer_id, building_instance_id)
            return False

    @staticmethod
    def recall_from_building(adventurer_id: int, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """召回正在建造/升级帮工的冒险者。

        幂等：精力耗尽时 `_tick_building_progress` 会把帮忙冒险者自动设为 resting 并
        清空 building_instance_id；前端可能仍发来 recall 请求。这里识别"已自动释放"
        的状态并友好返回 success，避免给用户看到误导的"不在建造中"错误。
        """
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '只能召回自己拥有的冒险者')
        if adv.status == 'building' and adv.building_instance_id:
            tick_inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, adv.building_instance_id)
            if tick_inst:
                try:
                    AdventurerS1Service._tick_building_progress(tick_inst)
                except (OperationalError, PendingRollbackError):
                    db_manager.rollback()
                    logger.info('召回冒险者 tick 建筑进度遇到死锁，已回滚')
                else:
                    try:
                        db_manager.refresh(adv)
                    except Exception:
                        pass
        if adv.status in AdventurerS1Service.FREE_STATUSES and (not adv.building_instance_id):
            return ({'adventurer': AdventurerS1Service._serialize_adventurer_for_client(adv, user_id, is_admin=is_admin), 'building': None, 'auto_released': True, 'message': '冒险者已不在建造中（可能因精力耗尽自动返回床位）'}, None)
        if adv.status in AdventurerS1Service.FREE_STATUSES and adv.building_instance_id:
            stale_inst_id = adv.building_instance_id
            if AdventurerS1Service._abort_orphaned_built_usage(adventurer_id, stale_inst_id):
                try:
                    db_manager.refresh(adv)
                except Exception:
                    adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id) or adv
                invalidate_buildings_list_cache()
                invalidate_dormitory_cache()
                return ({'adventurer': AdventurerS1Service._serialize_adventurer_for_client(adv, user_id, is_admin=is_admin), 'building': None, 'auto_released': True, 'message': '异常占用已清理，使用费用已退还'}, None)
            try:
                with db_manager.transaction():
                    locked_adv = AdventurerS1.query.filter_by(id=adventurer_id).populate_existing().with_for_update().first()
                    if not locked_adv:
                        return (None, '冒险者不存在')
                    if not is_admin and locked_adv.owner_id != user_id:
                        return (None, '只能召回自己拥有的冒险者')
                    if locked_adv.status not in AdventurerS1Service.FREE_STATUSES or locked_adv.building_instance_id != stale_inst_id:
                        return (None, '冒险者状态已经变化，请刷新后重试')
                    AdventurerS1BuildingInstance.query.filter_by(id=stale_inst_id).populate_existing().with_for_update().first()
                    AdventurerS1Service._close_open_building_helpers(stale_inst_id, adventurer_id)
                    from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
                    S1SkillSchoolService.release_usage_slot(adventurer_id)
                    locked_adv.building_instance_id = None
                    locked_adv.building_assigned_at = None
                    locked_adv.building_usage_extra = None
                    adv = locked_adv
            except Exception as e:
                return (None, player_operation_failure(e, '操作失败'))
            invalidate_buildings_list_cache()
            invalidate_dormitory_cache()
            return ({'adventurer': AdventurerS1Service._serialize_adventurer_for_client(adv, user_id, is_admin=is_admin), 'building': None, 'auto_released': True, 'message': '冒险者已不在建造中（残留占位已清理）'}, None)
        if adv.status != 'building':
            return (None, '该冒险者不在建造中')
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, adv.building_instance_id)
        if inst and inst.status == 'built':
            block_reason = AdventurerS1Service._built_usage_recall_block_reason(adv, inst)
            if block_reason:
                completed, complete_err = AdventurerS1Service._try_auto_complete_built_usage_before_recall(adv, inst, user_id, is_admin=is_admin)
                if completed is not None and complete_err is None:
                    invalidate_buildings_list_cache()
                    return (completed, None)
                return (None, block_reason)
        handler = None
        try:
            with db_manager.transaction():
                locked_adv = AdventurerS1.query.filter_by(id=adventurer_id).populate_existing().with_for_update().first()
                if not locked_adv:
                    return (None, '冒险者不存在')
                if not is_admin and locked_adv.owner_id != user_id:
                    return (None, '只能召回自己拥有的冒险者')
                if locked_adv.status in AdventurerS1Service.FREE_STATUSES and (not locked_adv.building_instance_id):
                    return ({'adventurer': AdventurerS1Service._serialize_adventurer_for_client(locked_adv, user_id, is_admin=is_admin), 'building': None, 'auto_released': True, 'message': '冒险者已不在建造中（可能因精力耗尽自动返回床位）'}, None)
                if locked_adv.status != 'building' or not locked_adv.building_instance_id:
                    return (None, '冒险者状态已经变化，请刷新后重试')
                locked_inst = AdventurerS1BuildingInstance.query.filter_by(id=locked_adv.building_instance_id).populate_existing().with_for_update().first()
                inst = locked_inst
                if inst and inst.status == 'built':
                    block_reason = AdventurerS1Service._built_usage_recall_block_reason(locked_adv, inst)
                    if block_reason:
                        return (None, block_reason)
                helper = AdventurerS1BuildingHelper.query.filter_by(building_instance_id=locked_adv.building_instance_id, adventurer_id=adventurer_id, left_at=None).order_by(AdventurerS1BuildingHelper.id.asc()).populate_existing().with_for_update().first()
                if helper:
                    helper.left_at = _portable_utc_now()
                if inst and inst.template:
                    from plugins.adventurer_s1.services.adventurer_s1_building_handler import get_handler
                    handler = get_handler(inst.template.handler_key)
                    if handler:
                        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService
                        cfg = BuildingSettlementService.get_reward_config(inst)
                        handler.on_recall(locked_adv, inst, cfg)
                if handler:
                    AdventurerS1StateTransitionCoordinator.return_from_building(locked_adv)
                else:
                    AdventurerS1Service._flush_building_rest_like_recovery(locked_adv)
                    AdventurerS1Service.enter_rest(locked_adv)
                    locked_adv.building_instance_id = None
                    locked_adv.building_assigned_at = None
                    locked_adv.building_usage_extra = None
                adv = locked_adv
                if inst and inst.status in ('building', 'upgrading'):
                    helpers = AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').all()
                    AdventurerS1Service._update_build_eta(inst, helpers=helpers)
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'building_recalled', 'reason': 'recall_from_building', 'ids': [adventurer_id]}, namespace='/')
        except Exception:
            pass
        invalidate_buildings_list_cache()
        if inst and inst.template and (inst.template.category == 'dormitory'):
            invalidate_dormitory_cache()
        return ({'adventurer': AdventurerS1Service._serialize_adventurer_for_client(adv, user_id, is_admin=is_admin), 'building': inst.to_dict(include_helpers=True) if inst else None}, None)

    @staticmethod
    def start_building_usage(adventurer_id: int, building_instance_id: int, user_id: Optional[int]=None, is_admin: bool=False, extra_params: Optional[dict]=None) -> Tuple[Optional[dict], Optional[str]]:
        """开始使用建筑：检查属性/人数/次数/CD/金币/精力，进入建筑。

        settlement_config 支持的字段：
          - usage_stamina_cost: 精力消耗（默认 0）
          - daily_limit: 每日次数限制（0 或 null 表示不限）
          - usage_duration_minutes: 使用时长（分钟，信息性，前端计时用）
          - required_stat: 属性需求字段名（strength/agility/intelligence/luck）
          - required_stat_min: 属性最低值
          - gold_cost: 金币消耗（默认 0，休闲建筑用）
          - cooldown_hours: CD 冷却小时数（默认 0，休闲建筑 12 小时）
          - extra_params: 额外参数（如治疗所选择的 target_traits），存入 building_usage_extra 供自动结算使用
        """
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and user_id is not None and (adv.owner_id != user_id):
            return (None, '只能派遣自己拥有的冒险者')
        can, reason = adv.can_dispatch()
        if not can:
            return (None, reason)
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.status == 'upgrading':
            return (None, '建筑正在升级中，暂时无法使用')
        if inst.status != 'built':
            return (None, '建筑尚未建成')
        if inst.disabled:
            return (None, '该建筑已被禁用')
        if not is_admin and AdventurerS1Service._is_tavern_lv2_building_locked(inst):
            return (None, '该建筑需酒馆升至 Lv2 后才能使用')
        AdventurerS1Service._sweep_stale_construction_occupants(inst)
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService
        config = BuildingSettlementService.get_reward_config(inst)
        from plugins.adventurer_s1.services.adventurer_s1_building_handler import get_handler
        handler = get_handler(inst.template.handler_key) if inst.template else None
        if handler:
            handler_err = handler.can_start(adv, inst, config)
            if handler_err:
                return (None, handler_err)
        handler_key = inst.template.handler_key if inst.template else None
        from plugins.adventurer_s1.services.s1_building_usage_queue_service import S1BuildingUsageQueueService
        building_queue_eligible = S1BuildingUsageQueueService.is_queue_handler(handler_key)
        building_queue_join_requested = building_queue_eligible and isinstance(extra_params, dict) and (extra_params.get('join_queue') is True)
        if handler_key in {'item_assembly_workshop', 'ember_kitchen'} and (not current_app.debug) and (not current_app.config.get('TESTING')):
            from services.async_task_service import AsyncTaskService
            if not (AsyncTaskService.has_durable_queue() or AsyncTaskService.can_run_database_startup_recovery()):
                building_label = '构装工坊' if handler_key == 'item_assembly_workshop' else '余烬灶坊'
                return (None, f'{building_label}暂时无法连接处理队列，请稍后再试')
        frozen_request_params = dict(extra_params or {}) if handler_key in {'item_assembly_workshop', 'ember_kitchen'} else None
        if handler and handler.is_async():
            if handler_key in ('healing_house', 'chapel'):
                raw_traits = (extra_params or {}).get('target_traits') or []
                raw_item_ref = (extra_params or {}).get('item_ref')
                target_traits = list(dict.fromkeys((str(t).strip() for t in raw_traits if t)))
                if not target_traits:
                    return (None, '请先选择要净化的特质' if handler_key == 'chapel' else '请先选择要治疗的特质')
                if len(target_traits) > 1:
                    return (None, '单次只能选择 1 个特质')
                existing = {t.name for t in adv.traits if not t.is_archived}
                for tn in target_traits:
                    if tn not in existing:
                        return (None, f'未找到特质「{tn}」')
                extra_params = {'target_traits': target_traits}
                if handler_key == 'healing_house' and raw_item_ref and AdventurerS1Service._building_has_item_treatment(inst):
                    healing_item_ref, item_err = AdventurerS1Service._resolve_healing_item_ref(adv, raw_item_ref)
                    if item_err:
                        return (None, item_err)
                    if healing_item_ref:
                        extra_params['item_ref'] = healing_item_ref
            elif handler_key == 'blacksmith':
                from plugins.adventurer_s1.services.s1_blacksmith_forge_intent import normalize_blacksmith_forge_intent
                forge_intent, forge_intent_err = normalize_blacksmith_forge_intent((extra_params or {}).get('forge_intent'))
                if forge_intent_err:
                    return (None, forge_intent_err)
                raw_item_refs = (extra_params or {}).get('item_refs') or []
                if not isinstance(raw_item_refs, list) or not raw_item_refs:
                    raw_equipment = (extra_params or {}).get('equipment_name')
                    equipment_name = str(raw_equipment).strip() if raw_equipment else ''
                    raw_materials = (extra_params or {}).get('material_names') or []
                    legacy_names = [equipment_name] + [str(m).strip() for m in raw_materials if m]
                    raw_item_refs = [{'source': 'any', 'name': name, 'id': None} for name in legacy_names if name]
                from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_OPERATION_TRANSFORM_INPUT, resolve_item_refs, resolve_item_ref, validate_items_operation
                normalized_refs, item_refs_err = resolve_item_refs(adv, raw_item_refs, min_count=2, max_count=2, selection_error='铁匠铺需要选择 2 件锻造物品')
                if item_refs_err:
                    return (None, item_refs_err)
                resolved_items = []
                for ref in normalized_refs:
                    item, item_err, _ = resolve_item_ref(adv, ref)
                    if item_err:
                        return (None, item_err)
                    resolved_items.append(item)
                operation_err = validate_items_operation(resolved_items, ITEM_OPERATION_TRANSFORM_INPUT, action_label='锻造')
                if operation_err:
                    return (None, operation_err)
                extra_params = {'item_refs': normalized_refs, 'forge_intent': forge_intent}
            elif handler_key == 'tailor_workshop':
                from plugins.adventurer_s1.services.s1_tailor_workshop_contract import normalize_tailor_workshop_operation
                tailor_operation, tailor_operation_err = normalize_tailor_workshop_operation((extra_params or {}).get('tailor_operation'))
                if tailor_operation_err:
                    return (None, tailor_operation_err)
                from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_OPERATION_TRANSFORM_INPUT, resolve_item_refs, resolve_item_ref, validate_items_operation
                normalized_refs, tailor_refs_err = resolve_item_refs(adv, (extra_params or {}).get('item_refs') or [], min_count=2, max_count=2, selection_error='绮罗裁坊需要选择 2 件裁制物品')
                if tailor_refs_err:
                    return (None, tailor_refs_err)
                resolved_items = []
                for ref in normalized_refs:
                    item, item_err, _ = resolve_item_ref(adv, ref)
                    if item_err:
                        return (None, item_err)
                    resolved_items.append(item)
                operation_err = validate_items_operation(resolved_items, ITEM_OPERATION_TRANSFORM_INPUT, action_label='裁制')
                if operation_err:
                    return (None, operation_err)
                extra_params = {'item_refs': normalized_refs, 'tailor_operation': tailor_operation}
            elif handler_key == 'alchemy_workshop':
                from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_OPERATION_TRANSFORM_INPUT, resolve_item_refs, resolve_item_ref, validate_items_operation
                raw_item_refs = (extra_params or {}).get('item_refs') or []
                normalized_refs, alchemy_err = resolve_item_refs(adv, raw_item_refs, min_count=1, max_count=5, selection_error='炼金工房需要选择 1~5 件物品')
                if alchemy_err:
                    return (None, alchemy_err)
                resolved_items = []
                for ref in normalized_refs:
                    item, item_err, _ = resolve_item_ref(adv, ref)
                    if item_err:
                        return (None, item_err)
                    resolved_items.append(item)
                operation_err = validate_items_operation(resolved_items, ITEM_OPERATION_TRANSFORM_INPUT, action_label='炼金')
                if operation_err:
                    return (None, operation_err)
                extra_params = {'item_refs': normalized_refs}
        if handler_key == 'pet_evolution':
            raw_companion_id = (extra_params or {}).get('companion_id')
            if raw_companion_id is None:
                return (None, '请选择要嬗变的伙伴')
            try:
                companion_id = int(raw_companion_id)
            except (TypeError, ValueError):
                return (None, '无效的伙伴 ID')
            companion_found = False
            for eq in adv.equipment_items:
                if eq.id == companion_id:
                    companion_found = True
                    break
            if not companion_found:
                return (None, '未找到指定的伙伴')
            companion = next((eq for eq in adv.equipment_items if eq.id == companion_id))
            from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_OPERATION_TRANSFORM_INPUT, validate_item_operation
            operation_err = validate_item_operation(companion, ITEM_OPERATION_TRANSFORM_INPUT, action_label='嬗变')
            if operation_err:
                return (None, operation_err)
            from plugins.adventurer_s1.services.adventurer_s1_building_handler import calculate_pet_evolution_gold_cost
            stage_at_start = max(0, int(companion.evolution_stage or 0))
            gold_cost = calculate_pet_evolution_gold_cost(config.get('gold_cost') or 0, stage_at_start)
            normalized_extra = {'companion_id': companion_id, 'gold_cost': gold_cost, 'evolution_stage': stage_at_start}
            raw_material_ref = (extra_params or {}).get('auxiliary_material_ref')
            if raw_material_ref is not None:
                if not isinstance(raw_material_ref, dict):
                    return (None, '无效的辅助嬗变素材')
                from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_OPERATION_TRANSFORM_INPUT, resolve_item_ref, validate_item_operation
                material, material_err, material_source = resolve_item_ref(adv, raw_material_ref)
                if material_err:
                    return (None, material_err)
                if material_source == 'carry' and material.id == companion_id:
                    return (None, '嬗变伙伴不能同时作为辅助素材')
                material_operation_err = validate_item_operation(material, ITEM_OPERATION_TRANSFORM_INPUT, action_label='辅助嬗变')
                if material_operation_err:
                    return (None, material_operation_err)
                from plugins.adventurer_s1.models import AdventurerS1Item
                material_catalog = AdventurerS1Item.query.filter_by(name=material.name).first()
                normalized_extra['auxiliary_material_ref'] = {'source': material_source, 'id': material.id, 'name': material.name}
                normalized_extra['auxiliary_material'] = {'name': material.name, 'type': material.type, 'rarity': material.rarity or 'common', 'desc': material.custom_desc or (material_catalog.description if material_catalog else '')}
            extra_params = normalized_extra
        if handler_key == 'dining_hall':
            food_code = str((extra_params or {}).get('food_code') or '').strip()
            from plugins.adventurer_s1.services.s1_dining_service import S1DiningService
            food, food_err = S1DiningService.resolve_food(food_code)
            if food_err:
                return (None, food_err)
            extra_params = {'food_code': food['code'], 'food_name': food['name'], 'gold_cost': food['gold_cost']}
        if handler_key == 'skill_school':
            raw_skill_params = extra_params or {}
            skill_code = str(raw_skill_params.get('skill_code') or '').strip()
            skill_school_join_queue_requested = raw_skill_params.get('join_queue') is True
            from plugins.adventurer_s1.services.s1_skill_school_service import S1SkillSchoolService
            if 'operation' in raw_skill_params:
                if skill_code:
                    return (None, '旧卷传艺和单独指点只能选一种')
                custom_params, prep_err = S1SkillSchoolService.prepare_custom_start(adv, inst, raw_skill_params.get('operation'), raw_skill_params.get('direction'), raw_skill_params.get('target_skill_id'))
                if prep_err:
                    return (None, prep_err)
                extra_params = custom_params
            else:
                if not skill_code:
                    return (None, '请选择要学习的技能')
                catalog_item, prep_err = S1SkillSchoolService.prepare_start(adv, skill_code)
                if prep_err:
                    return (None, prep_err)
                extra_params = {'skill_code': skill_code, 'skill_name': str(catalog_item.get('name') or '').strip(), 'gold_cost': max(0, int(catalog_item.get('gold_cost') or 0))}
                skill_school_join_queue_requested = False
        else:
            skill_school_join_queue_requested = False
        skill_school_usage_mode = None
        skill_school_queue_position = None
        building_queue_position = None
        if handler_key == 'skill_school':
            skill_school_usage_mode = S1SkillSchoolService.usage_mode_for_extra(extra_params)
        if handler_key == 'gambling_hall':
            from plugins.adventurer_s1.services.s1_gambling_service import S1GamblingService
            seat_index, seat_err = S1GamblingService.validate_seat_for_start(inst.id, extra_params)
            if seat_err:
                return (None, seat_err)
            extra_params = {'seat_index': seat_index}
        required_stat = config.get('required_stat')
        required_min = config.get('required_stat_min') or 0
        if required_stat and required_min > 0:
            stat_value = int(getattr(adv, required_stat, 0) or 0)
            stat_names = {'strength': '力量', 'agility': '敏捷', 'intelligence': '智力', 'luck': '运气'}
            if stat_value < required_min:
                return (None, f'{stat_names.get(required_stat, required_stat)}不足（需要 {required_min}，当前 {stat_value}）')
        usage_slots = S1SkillSchoolService.get_usage_slot_limit() if handler_key == 'skill_school' else AdventurerS1Service._effective_usage_slots(inst, config)
        if usage_slots > 0:
            if handler_key == 'skill_school':
                current_users = S1SkillSchoolService.get_usage_counts(inst.id)[skill_school_usage_mode]
            else:
                current_users = AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').count()
            if current_users >= usage_slots:
                if handler_key == 'skill_school':
                    if skill_school_usage_mode != 'custom' or not skill_school_join_queue_requested:
                        return (None, S1SkillSchoolService.format_usage_full_message(skill_school_usage_mode, current_users, usage_slots))
                from plugins.adventurer_s1.services.adventurer_s1_building_handler import format_usage_slots_full_message
                if handler_key != 'skill_school' and (not (building_queue_eligible and building_queue_join_requested)):
                    tpl = inst.template
                    return (None, format_usage_slots_full_message(current_users, usage_slots, tpl.handler_key if tpl else None, tpl.category if tpl else None))
            if handler_key == 'skill_school' and skill_school_usage_mode == 'custom' and (not skill_school_join_queue_requested):
                queue_state = S1SkillSchoolService.get_custom_queue_state(inst.id)
                if queue_state['waiting_count'] > 0:
                    return (None, S1SkillSchoolService.format_custom_queue_priority_message(queue_state['waiting_count']))
            if building_queue_eligible and (not building_queue_join_requested):
                queue_state = S1BuildingUsageQueueService.get_queue_state(inst.id)
                if queue_state['waiting_count'] > 0:
                    return (None, S1BuildingUsageQueueService.format_queue_priority_message(queue_state['waiting_count']))
        daily_limit = config.get('daily_limit') or 0
        if daily_limit > 0:
            from plugins.adventurer_s1.models import AdventurerS1BuildingUsageLog
            today_start = _portable_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_count = AdventurerS1BuildingUsageLog.query.filter(AdventurerS1BuildingUsageLog.adventurer_id == adventurer_id, AdventurerS1BuildingUsageLog.building_instance_id == building_instance_id, AdventurerS1BuildingUsageLog.used_at >= today_start).count()
            if today_count >= daily_limit:
                return (None, f'今日已使用 {today_count} 次，已达上限')
        cooldown_hours = AdventurerS1Service._effective_building_cooldown_hours(inst, config)
        if cooldown_hours > 0:
            from plugins.adventurer_s1.models import AdventurerS1BuildingUsageLog
            cd_since = _portable_utc_now() - timedelta(hours=cooldown_hours)
            recent_usage = AdventurerS1BuildingUsageLog.query.filter(AdventurerS1BuildingUsageLog.adventurer_id == adventurer_id, AdventurerS1BuildingUsageLog.building_instance_id == building_instance_id, AdventurerS1BuildingUsageLog.used_at >= cd_since).order_by(AdventurerS1BuildingUsageLog.used_at.desc()).first()
            if recent_usage:
                next_available = recent_usage.used_at + timedelta(hours=cooldown_hours)
                remaining = (next_available - _portable_utc_now()).total_seconds()
                remaining_min = max(1, int(remaining / 60))
                return (None, f'冷却中，还需等待 {remaining_min} 分钟')
        gold_cost = config.get('gold_cost') or 0
        gold_per_hp = config.get('gold_cost_per_hp') or 0
        gold_per_hour = config.get('gold_cost_per_hour') or 0
        duration_minutes = int(config.get('usage_duration_minutes') or 0)
        duration_per_hp = int(config.get('usage_duration_minutes_per_hp') or 0)
        missing_hp = 0
        heal_hp = 0
        clinic_hp_per_minute = 0
        search_hours = 0
        clue_stamina_cost = 0
        if handler_key in ('dining_hall', 'skill_school', 'pet_evolution') and isinstance(extra_params, dict):
            dynamic_gold = int(extra_params.get('gold_cost') or 0)
            if dynamic_gold > 0:
                gold_cost = dynamic_gold
        if handler_key == 'skill_school' and isinstance(extra_params, dict) and (extra_params.get('operation') in ('learn', 'advance')):
            duration_minutes = max(1, int(extra_params.get('usage_duration_minutes') or 0))
        if handler_key == 'sanatorium':
            resolved, rent_err = AdventurerS1Service._resolve_sanatorium_rental(config, extra_params)
            if rent_err:
                return (None, rent_err)
            gold_cost, duration_minutes = resolved
        elif handler_key == 'clinic':
            resolved, clinic_err = AdventurerS1Service._resolve_clinic_rental(config, extra_params, adv)
            if clinic_err:
                return (None, clinic_err)
            gold_cost, duration_minutes, heal_hp, clinic_hp_per_minute = resolved
        elif handler_key == 'scavenge':
            from plugins.adventurer_s1.services.s1_scavenge_service import S1ScavengeService
            resolved, scav_err = S1ScavengeService.resolve_dispatch(config, extra_params)
            if scav_err:
                return (None, scav_err)
            gold_cost, duration_minutes, search_hours = resolved
        elif handler_key == 'clue_bureau':
            from plugins.adventurer_s1.services.s1_clue_bureau_service import S1ClueBureauService
            resolved, clue_err = S1ClueBureauService.resolve_dispatch(config, extra_params)
            if clue_err:
                return (None, clue_err)
            duration_minutes, search_hours, clue_stamina_cost = resolved
            gold_cost = 0
        elif gold_per_hp > 0 or duration_per_hp > 0:
            missing_hp = AdventurerS1Service._resolve_per_hp_missing(adv)
            if handler_key == 'hospital':
                resolved, treat_err = AdventurerS1Service._resolve_hospital_treatment(config, extra_params, missing_hp)
                if treat_err:
                    return (None, treat_err)
                gold_cost, duration_minutes, heal_hp = resolved
            else:
                if gold_per_hp > 0:
                    gold_cost = gold_per_hp * missing_hp
                if duration_per_hp > 0:
                    duration_minutes = max(1, missing_hp * duration_per_hp) if missing_hp > 0 else 0
                heal_hp = missing_hp
        low_consumption_balances = None
        if gold_cost > 0:
            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            if S1CurrencyService.is_low_consumption_facility(handler_key):
                low_consumption_balances, bal_err = S1CurrencyService.get_low_consumption_payment_balances(adv.id)
                if bal_err:
                    return (None, bal_err)
                if float((low_consumption_balances or {}).get('available') or 0) < gold_cost:
                    return (None, f"可用金额不足（需要 {gold_cost}，金币与低消费临时金合计 {round(float((low_consumption_balances or {}).get('available') or 0), 2)}）")
            else:
                gold_balance, bal_err = S1CurrencyService.get_balance(adv.id, 'gold')
                if bal_err:
                    return (None, bal_err)
                if (gold_balance or 0) < gold_cost:
                    return (None, f'金币不足（需要 {gold_cost}，当前 {round(float(gold_balance or 0), 2)}）')
        input_materials = {str(k): int(v) for k, v in (config.get('input_materials') or {}).items() if int(v) > 0}
        if input_materials:
            from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
            balances, bal_err = S1MaterialService.get_all_balances(adv.id)
            if bal_err:
                return (None, bal_err)
            material_meta = S1MaterialService.get_material_meta()
            for mat_name, need in input_materials.items():
                have = int((balances or {}).get(mat_name, 0))
                if have < need:
                    mat_label = (material_meta.get(mat_name) or {}).get('name', mat_name)
                    return (None, f'{mat_label}不足（需要 {need}，当前 {have}）')
        stamina_cost = config.get('usage_stamina_cost') or 0
        if handler_key == 'clue_bureau':
            stamina_cost = clue_stamina_cost
        elif handler_key == 'skill_school' and isinstance(extra_params, dict) and (extra_params.get('operation') in ('learn', 'advance')):
            stamina_cost = max(0, int(extra_params.get('stamina_cost') or 0))
        if stamina_cost > 0:
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
            current_stamina = get_live_stamina(adv, max_stamina=int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100)))
            if current_stamina < stamina_cost:
                return (None, f'精力不足（需要 {stamina_cost}，当前 {current_stamina}）')
        low_consumption_payment = None
        try:
            with db_manager.transaction():
                usage_now = _portable_utc_now()
                locked_adv_query = AdventurerS1.query.filter_by(id=adventurer_id)
                if handler_key in {'item_assembly_workshop', 'ember_kitchen'}:
                    from sqlalchemy.orm import selectinload
                    locked_adv_query = locked_adv_query.options(selectinload(AdventurerS1.equipment_items), selectinload(AdventurerS1.warehouse_items))
                locked_adv = locked_adv_query.populate_existing().with_for_update().first()
                if not locked_adv:
                    return (None, '冒险者不存在')
                can, reason = locked_adv.can_dispatch()
                if not can:
                    return (None, reason)
                adv = locked_adv
                if handler_key == 'skill_school' and S1SkillSchoolService.has_retry_opportunity(adv):
                    return (None, S1SkillSchoolService.retry_block_message())
                if handler_key in {'item_assembly_workshop', 'ember_kitchen'}:
                    extra_params, prep_err = handler.prepare_start(adv, frozen_request_params)
                    if prep_err:
                        return (None, prep_err)
                if stamina_cost > 0:
                    from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
                    locked_stamina = get_live_stamina(adv, max_stamina=int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100)))
                    if locked_stamina < stamina_cost:
                        return (None, f'精力不足（需要 {stamina_cost}，当前 {locked_stamina}）')
                if usage_slots > 0:
                    locked_inst = AdventurerS1BuildingInstance.query.filter_by(id=inst.id).populate_existing().with_for_update().first()
                    if not locked_inst:
                        return (None, '建筑不存在')
                    inst = locked_inst
                    if inst.status != 'built':
                        return (None, '建筑状态已变化，请刷新后重试')
                    if inst.disabled:
                        return (None, '该建筑已被禁用')
                    locked_handler_key = AdventurerS1BuildingTemplate.query.with_entities(AdventurerS1BuildingTemplate.handler_key).filter_by(id=inst.template_id).scalar()
                    if locked_handler_key != handler_key:
                        return (None, '建筑配置已变化，请刷新后重试')
                    if handler_key == 'gambling_hall':
                        seat_index, seat_err = S1GamblingService.validate_seat_for_start(inst.id, extra_params)
                        if seat_err:
                            return (None, seat_err)
                        extra_params = {'seat_index': seat_index}
                    if handler_key == 'skill_school':
                        mode_slots = usage_slots
                        if skill_school_usage_mode == 'custom':
                            queue_state = S1SkillSchoolService.get_custom_queue_state(inst.id)
                            if skill_school_join_queue_requested:
                                current_users = S1SkillSchoolService.get_usage_counts(inst.id)[skill_school_usage_mode]
                                if queue_state['waiting_count'] <= 0 and current_users < mode_slots:
                                    claimed, current_users = S1SkillSchoolService.claim_usage_slot(inst.id, adv.id, skill_school_usage_mode, mode_slots, now=usage_now)
                                else:
                                    claimed = False
                                if not claimed:
                                    skill_school_queue_position = S1SkillSchoolService.enqueue_custom_training(inst.id, adv, now=usage_now)
                            elif queue_state['waiting_count'] > 0:
                                return (None, S1SkillSchoolService.format_custom_queue_priority_message(queue_state['waiting_count']))
                            else:
                                claimed, current_users = S1SkillSchoolService.claim_usage_slot(inst.id, adv.id, skill_school_usage_mode, mode_slots, now=usage_now)
                            if not skill_school_join_queue_requested and (not claimed):
                                return (None, S1SkillSchoolService.format_usage_full_message(skill_school_usage_mode, current_users, mode_slots))
                        else:
                            claimed, current_users = S1SkillSchoolService.claim_usage_slot(inst.id, adv.id, skill_school_usage_mode, mode_slots, now=usage_now)
                            if not claimed:
                                return (None, S1SkillSchoolService.format_usage_full_message(skill_school_usage_mode, current_users, mode_slots))
                    else:
                        current_users = AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').count()
                        if building_queue_eligible:
                            queue_state = S1BuildingUsageQueueService.get_queue_state(inst.id)
                            if building_queue_join_requested:
                                if queue_state['waiting_count'] > 0 or current_users >= usage_slots:
                                    building_queue_position = S1BuildingUsageQueueService.enqueue(inst.id, adv, now=usage_now)
                            elif queue_state['waiting_count'] > 0:
                                return (None, S1BuildingUsageQueueService.format_queue_priority_message(queue_state['waiting_count']))
                    if handler_key != 'skill_school' and building_queue_position is None and (current_users >= usage_slots):
                        from plugins.adventurer_s1.services.adventurer_s1_building_handler import format_usage_slots_full_message
                        tpl = inst.template
                        return (None, format_usage_slots_full_message(current_users, usage_slots, tpl.handler_key if tpl else None, tpl.category if tpl else None))
                AdventurerS1Service._close_open_building_helpers(None, adventurer_id, now=usage_now)
                next_status = 'skill_school_queue' if skill_school_queue_position is not None else S1BuildingUsageQueueService.QUEUE_STATUS if building_queue_position is not None else 'building'
                if stamina_cost > 0:
                    AdventurerS1StateTransitionCoordinator.leave_rest(adv, next_status, usage_now)
                    adv.stamina = max(0, adv.stamina - stamina_cost)
                else:
                    AdventurerS1StateTransitionCoordinator.leave_rest(adv, next_status, usage_now)
                if gold_cost > 0:
                    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
                    building_name = inst.template.name if inst.template else '建筑'
                    source_type = 'building_usage_cost'
                    deduct_desc = f'使用「{building_name}」费用'
                    if handler_key == 'skill_school' and isinstance(extra_params, dict) and (extra_params.get('operation') in ('learn', 'advance')):
                        operation = extra_params['operation']
                        source_type = f'skill_school_custom_{operation}'
                        action_label = '另学一门本事' if operation == 'learn' else f"磨炼「{extra_params.get('target_skill_name') or '旧招'}」"
                        deduct_desc = f'{building_name}请教导师：{action_label}'
                    elif handler_key == 'skill_school' and isinstance(extra_params, dict):
                        skill_name = str(extra_params.get('skill_name') or '技能').strip()
                        source_type = 'skill_school_learn'
                        deduct_desc = f'{building_name}旧卷传艺「{skill_name}」'
                    elif handler_key == 'blacksmith':
                        source_type = 'blacksmith_usage_cost'
                        deduct_desc = '铁匠铺锻造费用'
                    elif handler_key == 'tailor_workshop':
                        source_type = 'tailor_workshop_usage_cost'
                        deduct_desc = '绮罗裁坊裁制费用'
                    elif handler_key == 'alchemy_workshop':
                        source_type = 'alchemy_workshop_usage_cost'
                        deduct_desc = '炼金工房炼金费用'
                    elif handler_key == 'item_assembly_workshop':
                        source_type = 'assembly_workshop_usage_cost'
                        deduct_desc = '构装工坊组合费用'
                    elif handler_key == 'ember_kitchen':
                        source_type = 'ember_kitchen_usage_cost'
                        deduct_desc = '余烬灶坊烹饪费用'
                    if S1CurrencyService.is_low_consumption_facility(handler_key):
                        low_consumption_payment, dedu_err = S1CurrencyService.deduct_low_consumption(adv.id, float(gold_cost), facility_key=handler_key, source_type=source_type, related_id=inst.id, related_type='building_instance', description=deduct_desc, auto_commit=False)
                    else:
                        _, dedu_err = S1CurrencyService.deduct(adv.id, 'gold', float(gold_cost), source_type=source_type, related_id=inst.id, related_type='building_instance', description=deduct_desc, auto_commit=False)
                    if dedu_err:
                        raise S1PlayerVisibleError(dedu_err)
                if input_materials:
                    from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
                    building_name = inst.template.name if inst.template else '建筑'
                    for mat_name, need in input_materials.items():
                        _, mat_err = S1MaterialService.deduct(adv.id, mat_name, need, source_type='building_usage_cost', related_id=inst.id, related_type='building_instance', description=f'使用「{building_name}」消耗', auto_commit=False)
                        if mat_err:
                            raise S1PlayerVisibleError(mat_err)
                if handler_key == 'pet_evolution' and isinstance(extra_params, dict):
                    material_ref = extra_params.get('auxiliary_material_ref')
                    if material_ref:
                        from uuid import uuid4
                        from plugins.adventurer_s1.services.s1_item_instance_service import ITEM_OPERATION_TRANSFORM_INPUT, consume_item_ref_with_restore_snapshot
                        usage_session_id = uuid4().hex
                        session_extra = dict(extra_params)
                        session_extra['usage_session_id'] = usage_session_id
                        extra_params = session_extra
                        _, consume_err, restore_snapshot = consume_item_ref_with_restore_snapshot(adv.id, material_ref, amount=1, source_type='pet_evolution', description='嬗变祭坛辅助素材消耗', operation=ITEM_OPERATION_TRANSFORM_INPUT, action_label='辅助嬗变', related_id=usage_session_id, related_type='building_usage_session')
                        if consume_err:
                            raise RuntimeError(consume_err)
                        merged_extra = dict(extra_params)
                        merged_extra['auxiliary_material_refund_snapshot'] = restore_snapshot
                        extra_params = merged_extra
                if low_consumption_payment:
                    merged_extra = dict(extra_params or {})
                    merged_extra['low_consumption_payment'] = {'gold_spent': low_consumption_payment['gold_spent'], 'low_consumption_spent': low_consumption_payment['low_consumption_spent'], 'total_spent': low_consumption_payment['total_spent']}
                    extra_params = merged_extra
                if missing_hp > 0 and (gold_per_hp > 0 or duration_per_hp > 0):
                    merged_extra = dict(extra_params or {})
                    merged_extra['missing_hp'] = missing_hp
                    if heal_hp > 0:
                        merged_extra['heal_hp'] = heal_hp
                    if duration_per_hp > 0:
                        merged_extra['usage_duration_minutes'] = duration_minutes
                    extra_params = merged_extra
                elif handler_key == 'sanatorium':
                    merged_extra = dict(extra_params or {})
                    merged_extra['usage_duration_minutes'] = duration_minutes
                    merged_extra['rental_hours'] = max(1, duration_minutes // 60)
                    merged_extra['rental_start_at'] = usage_now.isoformat()
                    extra_params = merged_extra
                elif handler_key == 'clinic':
                    merged_extra = dict(extra_params or {})
                    merged_extra['heal_hp'] = heal_hp
                    merged_extra['hp_per_minute'] = clinic_hp_per_minute
                    merged_extra['hp_percent_per_minute'] = float(config.get('hp_percent_per_minute') or 1)
                    merged_extra['usage_duration_minutes'] = duration_minutes
                    merged_extra['rental_minutes'] = duration_minutes
                    extra_params = merged_extra
                elif handler_key == 'scavenge':
                    merged_extra = dict(extra_params or {})
                    merged_extra['usage_duration_minutes'] = duration_minutes
                    merged_extra['search_hours'] = search_hours
                    extra_params = merged_extra
                elif handler_key == 'clue_bureau':
                    merged_extra = dict(extra_params or {})
                    merged_extra['usage_duration_minutes'] = duration_minutes
                    merged_extra['search_hours'] = search_hours
                    extra_params = merged_extra
                if building_queue_position is not None:
                    reserved_extra = dict(extra_params or {})
                    reserved_extra.update({'gold_cost': round(float(gold_cost or 0), 2), 'stamina_cost': max(0, int(stamina_cost or 0)), 'usage_duration_minutes': max(0, int(duration_minutes or 0))})
                    extra_params = reserved_extra
                if handler and handler.is_async_for(extra_params):
                    from uuid import uuid4
                    async_extra = dict(extra_params or {})
                    if handler_key in ('skill_school', 'item_assembly_workshop', 'ember_kitchen', 'pet_evolution'):
                        async_extra.setdefault('usage_session_id', uuid4().hex)
                    else:
                        async_extra['usage_session_id'] = uuid4().hex
                    extra_params = async_extra
                adv.building_instance_id = inst.id
                if skill_school_queue_position is not None:
                    queued_extra = dict(extra_params or {})
                    queued_extra.update({'queue_state': 'waiting', 'queued_at': usage_now.isoformat()})
                    adv.building_assigned_at = None
                    adv.building_usage_extra = queued_extra
                elif building_queue_position is not None:
                    queued_extra = dict(extra_params or {})
                    queued_extra.update({'queue_state': 'waiting', 'queued_at': usage_now.isoformat(), 'queue_handler_key': handler_key})
                    adv.building_assigned_at = None
                    adv.building_usage_extra = queued_extra
                else:
                    adv.building_assigned_at = usage_now
                    adv.building_usage_extra = extra_params or None
                    if handler_key == 'skill_school' and skill_school_usage_mode == 'custom':
                        usage_session_id = str((adv.building_usage_extra or {}).get('usage_session_id') or '').strip()
                        _, freeze_err = S1SkillSchoolService.ensure_retry_opportunity_for_active_lesson(adv, inst.id, usage_session_id)
                        if freeze_err:
                            raise RuntimeError(f'冻结导师课堂失败: {freeze_err}')
        except Exception as e:
            return (None, player_operation_failure(e, '操作失败'))
        invalidate_adventurer_detail_cache(adventurer_id)
        invalidate_buildings_list_cache()
        if building_queue_position is not None and adv.owner_id is not None:
            invalidate_my_activity_cache(adv.owner_id)
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {'action': 'skill_school_queue_joined' if skill_school_queue_position is not None else 'building_usage_queue_joined' if building_queue_position is not None else 'building_usage_start', 'reason': 'mentor_busy' if skill_school_queue_position is not None else 'usage_slots_busy' if building_queue_position is not None else 'usage_dispatch', 'ids': [adventurer_id], 'building_instance_id': inst.id}, namespace='/')
        except Exception:
            pass
        result = {'adventurer': AdventurerS1Service._serialize_adventurer_for_client(adv, user_id, is_admin=is_admin), 'building': inst.to_dict(include_helpers=True, for_list=True, for_client=not is_admin), 'usage_duration_minutes': duration_minutes, 'stamina_cost': stamina_cost}
        if low_consumption_payment:
            result['payment'] = low_consumption_payment
        if skill_school_queue_position is not None:
            result.update({'queued': True, 'queue_position': skill_school_queue_position})
        elif building_queue_position is not None:
            result.update({'queued': True, 'queue_position': building_queue_position})
        return (result, None)

    @staticmethod
    def complete_building_usage(adventurer_id: int, building_instance_id: int, user_id: Optional[int]=None, is_admin: bool=False, extra_params: Optional[dict]=None, *, include_details: bool=True, expected_usage: Optional[tuple[Optional[datetime], str]]=None) -> Tuple[Optional[dict], Optional[str]]:
        """建筑功能使用完成：校验在场与时长 → 结算奖励、记录使用、释放冒险者。

        通用入口——按摩结束、修炼完成、打工结算等场景统一调用。
        scheduler 传 include_details=False，只返回处理标志，避免构建无人消费的详情。
        expected_usage 绑定扫描时的入驻时间和会话标识，锁后拒绝结算已替换的使用。

        异步 handler（is_async_for(extra)=True）：on_complete 只提交 AI 任务，金币仍在事务中扣除，
        但跳过 usage_log 记录和冒险者释放（由 AI worker 负责）。

        防刷校验：
        - 建筑必须已建成（建造中的帮工释放走 recall，不走本入口）
        - 冒险者必须确实在该建筑中使用（status='building' 且 building_instance_id 匹配），
          堵死「跳过 start 直接 complete 白拿奖励、绕过每日上限/CD/精力金币成本」的刷子路径
        - 使用时长必须已满 `usage_duration_minutes`（管理员豁免）
        - 事务开头行锁防并发双结算（2026-06-10 补齐竞态防护）
        """
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and user_id is not None and (adv.owner_id != user_id):
            return (None, '只能操作自己拥有的冒险者')
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.status != 'built':
            return (None, '建筑尚未建成')
        if adv.status != 'building' or adv.building_instance_id != building_instance_id:
            return (None, '该冒险者不在此建筑中使用')
        from plugins.adventurer_s1.services.adventurer_s1_building_handler import BuildingUsageInProgress, TerminalBuildingUsageError, get_handler
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService
        config = BuildingSettlementService.get_reward_config(inst)
        duration_minutes = AdventurerS1Service._effective_usage_duration_minutes(adv, config)
        handler_key = inst.template.handler_key if inst.template else None
        building_category = inst.template.category if inst.template else None
        usage_start = AdventurerS1Service._effective_usage_start_at(adv, handler_key)
        if duration_minutes > 0 and (not is_admin) and usage_start:
            elapsed_seconds = (_portable_utc_now() - usage_start).total_seconds()
            required_seconds = duration_minutes * 60
            if elapsed_seconds < required_seconds:
                remaining_min = max(1, int((required_seconds - elapsed_seconds) / 60))
                return (None, f'使用尚未完成，还需约 {remaining_min} 分钟')
        if not is_admin and user_id is not None:
            stored_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else None
            handler = get_handler(handler_key) if handler_key else None
            if handler and handler.is_async_for(stored_extra or extra_params):
                return (None, '该建筑会由酒馆自动收尾，无需手动重试')
        is_async_handler = False
        async_already_running = False
        terminal_handler_error: Optional[str] = None
        usage_session_id: Optional[str] = None
        handler = get_handler(handler_key) if handler_key else None
        try:
            with db_manager.transaction():
                locked_adv_query = AdventurerS1.query.populate_existing()
                completion_relationships = handler.completion_relationships if handler else ()
                if completion_relationships:
                    from sqlalchemy.orm import selectinload
                    locked_adv_query = locked_adv_query.options(*(selectinload(getattr(AdventurerS1, relationship_name)) for relationship_name in completion_relationships)).populate_existing()
                adv = locked_adv_query.filter(AdventurerS1.id == adventurer_id).with_for_update().first()
                if not adv:
                    raise S1PlayerVisibleError('冒险者不存在（并发冲突）')
                if adv.status != 'building' or adv.building_instance_id != building_instance_id:
                    raise S1PlayerVisibleError('该冒险者已不在此建筑中使用')
                if expected_usage is not None:
                    current_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
                    current_usage = (adv.building_assigned_at, str(current_extra.get('usage_session_id') or ''))
                    if current_usage != expected_usage:
                        raise S1PlayerVisibleError('建筑使用状态已变化，本轮自动结算已跳过')
                stored_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else None
                effective_extra_params = stored_extra if handler_key in {'item_assembly_workshop', 'ember_kitchen'} else extra_params or stored_extra
                is_async_handler = handler.is_async_for(stored_extra or effective_extra_params) if handler else False
                if is_async_handler:
                    usage_session_id = str((stored_extra or {}).get('usage_session_id') or '') or None
                if handler:
                    handler_ok, handler_err = handler.on_complete(adv, inst, config, extra_params=effective_extra_params)
                    if isinstance(handler_err, BuildingUsageInProgress):
                        handler_ok = True
                        async_already_running = True
                    elif handler_err:
                        if isinstance(handler_err, TerminalBuildingUsageError):
                            terminal_handler_error = str(handler_err)
                        if handler_key == 'skill_school':
                            raise RuntimeError(handler_err)
                        raise RuntimeError(f'结算失败: {handler_err}')
                    if not handler_ok:
                        ok, err = BuildingSettlementService.settle(adv, inst, source_type='building_usage_complete')
                        if not ok:
                            raise RuntimeError(f'结算失败: {err}')
                else:
                    ok, err = BuildingSettlementService.settle(adv, inst, source_type='building_usage_complete')
                    if not ok:
                        raise RuntimeError(f'结算失败: {err}')
                if not is_async_handler:
                    adv.building_usage_extra = None
                if not is_async_handler:
                    from plugins.adventurer_s1.models import AdventurerS1BuildingUsageLog
                    usage_log = AdventurerS1BuildingUsageLog(adventurer_id=adventurer_id, building_instance_id=building_instance_id)
                    db_manager.add(usage_log)
                    if handler:
                        AdventurerS1StateTransitionCoordinator.return_from_building(adv)
                    else:
                        AdventurerS1Service._flush_building_rest_like_recovery(adv)
                        AdventurerS1Service.enter_rest(adv)
                        adv.building_instance_id = None
                        adv.building_assigned_at = None
        except Exception as e:
            if terminal_handler_error and is_admin and is_async_handler:
                from plugins.adventurer_s1.services.adventurer_s1_building_ai_recovery_service import AdventurerS1BuildingAiRecoveryService
                aborted = AdventurerS1BuildingAiRecoveryService.abort_stuck_session(adventurer_id, building_instance_id, handler_key, reason='building_usage_input_invalid', usage_session_id=usage_session_id)
                if aborted:
                    current_app.logger.warning('自动中止失效的异步建筑使用 adv=%s inst=%s handler=%s: %s', adventurer_id, building_instance_id, handler_key, terminal_handler_error)
                    aborted_result = {'aborted': True, 'handler_key': handler_key, 'message': '原结算对象已失效，本次使用已取消并完成退款。'}
                    if not include_details:
                        return (aborted_result, None)
                    adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
                    inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
                    return ({'adventurer': AdventurerS1Service._serialize_adventurer_for_client(adv, user_id, is_admin=is_admin), 'building': inst.to_dict(include_helpers=True, for_list=True, for_client=not is_admin), **aborted_result}, None)
            return (None, player_operation_failure(e, '操作失败'))
        if not is_async_handler:
            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                socketio.emit('adventurer_changed', {'action': 'building_usage_complete', 'reason': 'usage_settle', 'ids': [adventurer_id], 'building_instance_id': building_instance_id}, namespace='/')
            except Exception:
                pass
        result = {}
        if include_details:
            result.update({'adventurer': AdventurerS1Service._serialize_adventurer_for_client(adv, user_id, is_admin=is_admin), 'building': inst.to_dict(include_helpers=True, for_list=True, for_client=not is_admin)})
        if is_async_handler:
            result['async'] = True
            if async_already_running:
                result['already_running'] = True
            _async_messages = {'healing_house': '治疗进行中，冒险者正在接受治疗师的诊治…', 'blacksmith': '锻造进行中，铁匠正在熔炉前忙碌…', 'tailor_workshop': '裁制进行中，裁缝师正在灯下细作…', 'alchemy_workshop': '炼金进行中，炼金师正在调配试剂…', 'item_assembly_workshop': '构装进行中，工匠正在按你的排列校准结构…', 'ember_kitchen': '灶火正旺，掌勺人正在收汁装盘…', 'chapel': '祈祷进行中，见习修女正在为冒险者诵经净化…', 'pet_evolution': '嬗变仪式进行中，祭坛守护者正在引导伙伴蜕变…', 'skill_school': '一小时的练习已经结束，导师正替你把最后几处招式理顺…'}
            result['handler_key'] = handler_key
            result['message'] = _async_messages.get(handler_key, '处理中，请稍候…')
        elif building_category == 'gathering':
            from plugins.adventurer_s1.services.adventurer_s1_treasure_map_drop_service import AdventurerS1TreasureMapDropService
            stamina_spent = int(config.get('usage_stamina_cost') or 0)
            if stamina_spent <= 0:
                stamina_spent = 30
            AdventurerS1TreasureMapDropService.try_roll_gathering_complete(adventurer_id, stamina_spent=stamina_spent)
        invalidate_adventurer_detail_cache(adventurer_id)
        invalidate_buildings_list_cache()
        return (result, None)

    @staticmethod
    def auto_complete_expired_usage(building_instance_id: Optional[int]=None) -> int:
        """服务器端自动结算到期的建筑使用（scheduler 调用）。

        扫描 status='building' 且 building_instance_id 非空的冒险者；每人按自己的
        building_assigned_at + usage_duration_minutes 独立到期，互踢无关。

        Args:
            building_instance_id: 若指定则只处理该建筑实例上的冒险者

        Returns:
            自动结算的数量
        """
        from copy import deepcopy
        from sqlalchemy.orm import joinedload, noload, selectinload
        from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1BuildingInstance
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import BuildingSettlementService
        query = AdventurerS1.query.options(joinedload(AdventurerS1.building_instance).joinedload(AdventurerS1BuildingInstance.template), selectinload(AdventurerS1.traits), noload(AdventurerS1.equipment_items), noload(AdventurerS1.skill_items), noload(AdventurerS1.investor_items), noload(AdventurerS1.warehouse_items), noload(AdventurerS1.materials), noload(AdventurerS1.dorm_room), noload(AdventurerS1.story_threads)).filter(AdventurerS1.status == 'building', AdventurerS1.building_instance_id.isnot(None))
        if building_instance_id is not None:
            query = query.filter(AdventurerS1.building_instance_id == building_instance_id)
        active_users = query.all()
        if not active_users:
            db_manager.rollback()
            return 0
        now = _portable_utc_now()
        auto_completed = 0
        settled_adv_ids: list[int] = []
        candidates = []
        for adv in active_users:
            if not adv.building_instance_id:
                continue
            inst = adv.building_instance
            if not inst or inst.status != 'built':
                continue
            adv_id = int(adv.id)
            inst_id = int(inst.id)
            config = BuildingSettlementService.get_reward_config(inst)
            handler_key = inst.template.handler_key if inst.template else None
            usage_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
            usage_identity = (adv.building_assigned_at, str(usage_extra.get('usage_session_id') or ''))
            if handler_key == 'hospital':
                from plugins.adventurer_s1.services.adventurer_s1_building_handler import HospitalHandler
                current_treatment_hp = HospitalHandler.current_treatment_hp(adv, config, now=now)
                if current_treatment_hp >= int(adv.max_hp or 100):
                    candidates.append((adv_id, inst_id, handler_key, None, False, usage_identity))
                    continue
            if handler_key == 'clinic':
                from plugins.adventurer_s1.services.adventurer_s1_building_handler import ClinicHandler
                extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
                target_heal = int(extra.get('heal_hp') or 0)
                if target_heal > 0:
                    max_hp = int(adv.max_hp or 100)
                    base_hp = max(0, min(int(adv.hp or 0), max_hp))
                    current_treatment_hp = ClinicHandler.current_treatment_hp(adv, config, now=now)
                    if current_treatment_hp >= min(max_hp, base_hp + target_heal):
                        candidates.append((adv_id, inst_id, handler_key, None, False, usage_identity))
                        continue
            if handler_key == 'sanatorium':
                from plugins.adventurer_s1.services.adventurer_s1_building_handler import _has_recoverable_traits
                if not _has_recoverable_traits(adv):
                    candidates.append((adv_id, inst_id, handler_key, None, True, usage_identity))
                    continue
            extra_params = adv.building_usage_extra if handler_key in {'healing_house', 'blacksmith', 'tailor_workshop', 'alchemy_workshop', 'chapel', 'pet_evolution'} else None
            if handler_key in ('healing_house', 'chapel') and (not (extra_params or {}).get('target_traits')):
                current_app.logger.warning('自动结算跳过治疗所 adv=%s: 缺少 target_traits', adv_id)
                continue
            if handler_key == 'blacksmith' and (not (extra_params or {}).get('item_refs')):
                current_app.logger.warning('自动结算跳过铁匠铺 adv=%s: 缺少 item_refs', adv_id)
                continue
            if handler_key == 'tailor_workshop' and (not (extra_params or {}).get('item_refs')):
                current_app.logger.warning('自动结算跳过绮罗裁坊 adv=%s: 缺少 item_refs', adv_id)
                continue
            if handler_key == 'alchemy_workshop' and (not (extra_params or {}).get('item_refs')):
                current_app.logger.warning('自动结算跳过炼金工房 adv=%s: 缺少 item_refs', adv_id)
                continue
            if handler_key == 'pet_evolution' and (extra_params or {}).get('companion_id') is None:
                current_app.logger.warning('自动结算跳过嬗变祭坛 adv=%s: 缺少 companion_id', adv_id)
                continue
            duration_minutes = AdventurerS1Service._effective_usage_duration_minutes(adv, config)
            if duration_minutes <= 0:
                continue
            usage_start = AdventurerS1Service._effective_usage_start_at(adv, handler_key)
            if not usage_start:
                continue
            elapsed = (now - usage_start).total_seconds()
            if elapsed < duration_minutes * 60:
                continue
            candidates.append((adv_id, inst_id, handler_key, deepcopy(extra_params), False, usage_identity))
        db_manager.rollback()
        for adv_id, inst_id, handler_key, extra_params, recovered, usage_identity in candidates:
            if recovered:
                from sqlalchemy.orm import lazyload
                from plugins.adventurer_s1.services.adventurer_s1_building_handler import _has_recoverable_traits, get_handler
                with db_manager.transaction():
                    adv = AdventurerS1.query.options(lazyload('*'), selectinload(AdventurerS1.traits)).filter_by(id=adv_id).populate_existing().with_for_update().first()
                    if not adv or adv.status != 'building' or adv.building_instance_id != inst_id:
                        continue
                    current_extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
                    current_usage = (adv.building_assigned_at, str(current_extra.get('usage_session_id') or ''))
                    if current_usage != usage_identity or _has_recoverable_traits(adv):
                        continue
                    inst = AdventurerS1BuildingInstance.query.options(lazyload('*')).filter_by(id=inst_id).populate_existing().with_for_update().first()
                    if not inst or inst.status != 'built' or (not inst.template) or (inst.template.handler_key != 'sanatorium'):
                        continue
                    config = BuildingSettlementService.get_reward_config(inst)
                    handler = get_handler('sanatorium')
                    if handler:
                        handler.on_recall(adv, inst, config)
                        AdventurerS1StateTransitionCoordinator.return_from_building(adv)
                    else:
                        AdventurerS1Service.enter_rest(adv)
                        adv.building_instance_id = None
                        adv.building_assigned_at = None
                        adv.building_usage_extra = None
                auto_completed += 1
                settled_adv_ids.append(adv_id)
                current_app.logger.info('疗养所特质已恢复完毕，自动退出 adv=%s', adv_id)
                continue
            completion, err = AdventurerS1Service.complete_building_usage(adventurer_id=adv_id, building_instance_id=inst_id, is_admin=True, extra_params=extra_params, include_details=False, expected_usage=usage_identity)
            db_manager.rollback()
            if err:
                current_app.logger.warning('自动结算建筑使用失败 adv=%s inst=%s: %s', adv_id, inst_id, err)
            elif completion and completion.get('already_running'):
                current_app.logger.debug('自动结算命中执行中的异步建筑任务 adv=%s inst=%s handler=%s', adv_id, inst_id, handler_key)
            elif completion and completion.get('aborted'):
                current_app.logger.info('自动结算已清理失效的异步建筑使用 adv=%s inst=%s handler=%s', adv_id, inst_id, handler_key)
            else:
                auto_completed += 1
                settled_adv_ids.append(adv_id)
                current_app.logger.info('自动结算建筑使用 adv=%s inst=%s handler=%s', adv_id, inst_id, handler_key)
        db_manager.rollback()
        if auto_completed > 0:
            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                socketio.emit('adventurer_changed', {'action': 'building_auto_complete', 'reason': 'usage_expired', 'ids': settled_adv_ids}, namespace='/')
            except Exception:
                pass
        return auto_completed

    @staticmethod
    def _update_build_eta(inst: AdventurerS1BuildingInstance, helpers: Optional[list]=None, now: Optional[datetime]=None) -> None:
        """按当前在位帮手并发速率更新预计完工时刻（build_complete_at）。

        前端倒计时依赖此字段；实际进度仍由 _tick_building_progress 惰性结算。
        每个仍有精力的帮手每秒贡献 speedup_minutes/60 秒建筑进度（与 tick 公式一致）。
        """
        if inst.status not in ('building', 'upgrading') or not inst.template:
            return
        now = now or _portable_utc_now()
        if inst.status == 'upgrading':
            upgrade_cfg = AdventurerS1Service._building_upgrade_stage_config(inst)
            upgrade_hours = int(upgrade_cfg.get('upgrade_hours', 4))
            total_build_seconds = upgrade_hours * 3600
        else:
            total_build_seconds = inst.template.build_hours * 3600
        remaining_work = max(0, total_build_seconds - inst.accumulated_progress_seconds)
        if remaining_work <= 0:
            if inst.status == 'upgrading':
                inst.upgrade_complete_at = now
            else:
                inst.build_complete_at = now
            return
        if helpers is None:
            helpers = AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').all()
        active_count = sum((1 for a in helpers if a.stamina > 0))
        speedup_minutes = inst.template.speedup_minutes or int(current_app.config.get('ADVENTURER_S1_BUILDING_SPEEDUP_MINUTES', 60))
        progress_rate = active_count * speedup_minutes / 60.0
        if progress_rate <= 0:
            eta = now + timedelta(seconds=remaining_work)
        else:
            eta = now + timedelta(seconds=remaining_work / progress_rate)
        if inst.status == 'upgrading':
            inst.upgrade_complete_at = eta
        else:
            inst.build_complete_at = eta

    @staticmethod
    def _release_building_occupants(inst: AdventurerS1BuildingInstance, now: Optional[datetime]=None, *, close_open_helper_logs: bool=True) -> int:
        """释放仍挂在该建筑上的 building 态冒险者（自然完工 / GM 建成清场）。"""
        from plugins.adventurer_s1.models import AdventurerS1BuildingHelper
        now = now or _portable_utc_now()
        if close_open_helper_logs:
            AdventurerS1BuildingHelper.query.filter(AdventurerS1BuildingHelper.building_instance_id == inst.id, AdventurerS1BuildingHelper.left_at.is_(None)).update({AdventurerS1BuildingHelper.left_at: now}, synchronize_session=False)
        occupants = AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').all()
        for adv in occupants:
            AdventurerS1Service._flush_building_rest_like_recovery(adv, now)
            AdventurerS1Service.enter_rest(adv, now)
            adv.building_instance_id = None
            adv.building_assigned_at = None
            adv.building_usage_extra = None
        return len(occupants)

    @staticmethod
    def _sweep_stale_construction_occupants(inst: AdventurerS1BuildingInstance, open_helper_adv_ids: Optional[set[int]]=None, *, auto_commit: bool=True) -> int:
        """已建成建筑：清走仍有未闭合帮工记录的建造残留（不影响采集/使用）。

        采集派遣不会写 BuildingHelper，因此不会被本方法踢出场。按 adv -> building ->
        helper 固定锁序处理；scheduler 批量调用时由外层统一提交。
        """
        if inst.status != 'built':
            return 0
        from plugins.adventurer_s1.models import AdventurerS1BuildingHelper
        if open_helper_adv_ids is None:
            open_helper_adv_ids = {int(row[0]) for row in AdventurerS1BuildingHelper.query.filter(AdventurerS1BuildingHelper.building_instance_id == inst.id, AdventurerS1BuildingHelper.left_at.is_(None)).with_entities(AdventurerS1BuildingHelper.adventurer_id).all()}
        if not open_helper_adv_ids:
            return 0
        candidate_ids = sorted({int(adv_id) for adv_id in open_helper_adv_ids})
        try:
            locked_adventurers = AdventurerS1.query.filter(AdventurerS1.id.in_(candidate_ids)).order_by(AdventurerS1.id.asc()).populate_existing().with_for_update(skip_locked=True).all()
            locked_inst = AdventurerS1BuildingInstance.query.filter_by(id=inst.id).populate_existing().with_for_update(skip_locked=True).first()
            if not locked_inst or locked_inst.status != 'built':
                if auto_commit:
                    db_manager.rollback()
                return 0
            inst = locked_inst
            helper_rows = AdventurerS1BuildingHelper.query.filter(AdventurerS1BuildingHelper.building_instance_id == inst.id, AdventurerS1BuildingHelper.adventurer_id.in_([adv.id for adv in locked_adventurers]), AdventurerS1BuildingHelper.left_at.is_(None)).order_by(AdventurerS1BuildingHelper.id.asc()).populate_existing().with_for_update(skip_locked=True).all() if locked_adventurers else []
            helper_adv_ids = {int(row.adventurer_id) for row in helper_rows}
            stale = [adv for adv in locked_adventurers if adv.id in helper_adv_ids and adv.building_instance_id == inst.id and (adv.status == 'building')]
            now = _portable_utc_now()
            stale_ids = {int(adv.id) for adv in stale}
            for helper_row in helper_rows:
                if int(helper_row.adventurer_id) in stale_ids:
                    helper_row.left_at = now
            for adv in stale:
                AdventurerS1Service.enter_rest(adv, now)
                adv.building_instance_id = None
                adv.building_assigned_at = None
                adv.building_usage_extra = None
            if auto_commit:
                ok, err = db_manager.commit()
                if not ok:
                    raise RuntimeError(err or '清场建造帮工残留提交失败')
        except Exception as e:
            if not auto_commit:
                raise
            db_manager.rollback()
            if AdventurerS1Service._is_lock_timeout_error(e):
                logger.info('清场建造帮工残留遇到行锁/死锁，已跳过 building=%s', inst.id)
            else:
                logger.warning('清场建造帮工残留失败 building=%s: %s', inst.id, e)
            return 0
        return len(stale)

    @staticmethod
    def _close_open_building_helpers(building_instance_id: Optional[int], adventurer_id: int, now: Optional[datetime]=None) -> int:
        """闭合冒险者未离场的帮工记录；建筑 ID 为空时清理其全部开放记录。"""
        from plugins.adventurer_s1.models import AdventurerS1BuildingHelper
        now = now or _portable_utc_now()
        query = AdventurerS1BuildingHelper.query.filter(AdventurerS1BuildingHelper.adventurer_id == adventurer_id, AdventurerS1BuildingHelper.left_at.is_(None))
        if building_instance_id is not None:
            query = query.filter(AdventurerS1BuildingHelper.building_instance_id == building_instance_id)
        return query.update({AdventurerS1BuildingHelper.left_at: now}, synchronize_session=False)

    @staticmethod
    def _tick_building_progress(inst: AdventurerS1BuildingInstance, helpers: Optional[list]=None, commit: bool=True, treasure_roll_stamina: Optional[dict[int, int]]=None, dormitory_effects: Optional[dict]=None) -> bool:
        """惰性计算建筑进度：更新在位冒险者的精力消耗和建筑进度。

        Args:
            inst: 建筑实例。
            helpers: 预取好的在位冒险者列表（批量场景由调用方传入），未传则现查。
            commit: 是否在末尾 commit（批量场景由调用方关闭，统一 commit）。
            treasure_roll_stamina: 批量场景收集实际消耗精力，统一提交成功后再掷骰。

        Returns:
            若本次完工新建了宿舍房间则 True，否则 False。
            commit=False 时由调用方在统一 commit 后调用 _maybe_auto_draw_after_dormitory_built。
        """
        if helpers is None:
            candidate_helper_ids = [int(row.id) for row in AdventurerS1.query.with_entities(AdventurerS1.id).filter_by(building_instance_id=inst.id, status='building').order_by(AdventurerS1.id.asc()).all()]
        else:
            candidate_helper_ids = sorted({int(adv.id) for adv in helpers if adv is not None and adv.id is not None})
        locked_helpers = AdventurerS1.query.filter(AdventurerS1.id.in_(candidate_helper_ids)).order_by(AdventurerS1.id.asc()).populate_existing().with_for_update(skip_locked=True).all() if candidate_helper_ids else []
        locked_helper_map = {int(adv.id): adv for adv in locked_helpers}
        locked_inst = AdventurerS1BuildingInstance.query.filter_by(id=inst.id).populate_existing().with_for_update(skip_locked=True).first()
        if not locked_inst or locked_inst.status not in ('building', 'upgrading'):
            if commit:
                db_manager.rollback()
            return False
        inst = locked_inst
        current_helper_ids = [int(row.id) for row in AdventurerS1.query.with_entities(AdventurerS1.id).filter_by(building_instance_id=inst.id, status='building').order_by(AdventurerS1.id.asc()).all()]
        if any((adv_id not in locked_helper_map for adv_id in current_helper_ids)):
            if commit:
                db_manager.rollback()
            return False
        helpers = [locked_helper_map[adv_id] for adv_id in current_helper_ids]
        now = _portable_utc_now()
        if inst.template and inst.template.stamina_cost_per_help:
            stamina_per_hour = inst.template.stamina_cost_per_help
        else:
            stamina_per_hour = int(current_app.config.get('ADVENTURER_S1_BUILDING_STAMINA_PER_HOUR', 20))
        speedup_minutes = inst.template.speedup_minutes if inst.template and inst.template.speedup_minutes else int(current_app.config.get('ADVENTURER_S1_BUILDING_SPEEDUP_MINUTES', 60))
        seconds_per_stamina = speedup_minutes * 60 / stamina_per_hour if stamina_per_hour > 0 else 180
        open_helper_recs: dict[int, AdventurerS1BuildingHelper] = {}
        if helpers:
            with db.session.no_autoflush:
                rows = AdventurerS1BuildingHelper.query.filter(AdventurerS1BuildingHelper.building_instance_id == inst.id, AdventurerS1BuildingHelper.adventurer_id.in_([a.id for a in helpers]), AdventurerS1BuildingHelper.left_at.is_(None)).order_by(AdventurerS1BuildingHelper.id.asc()).populate_existing().with_for_update().all()
            open_helper_recs = {r.adventurer_id: r for r in rows}
        if inst.status == 'upgrading':
            upgrade_cfg = AdventurerS1Service._building_upgrade_stage_config(inst)
            upgrade_hours = int(upgrade_cfg.get('upgrade_hours', 4))
            total_build_seconds = upgrade_hours * 3600
        else:
            total_build_seconds = inst.template.build_hours * 3600 if inst.template else 3600
        remaining_credit = max(0, total_build_seconds - inst.accumulated_progress_seconds)
        total_progress_added = 0
        stamina_spent_by_helper: dict[int, int] = {}
        for adv in helpers:
            if not adv.building_assigned_at:
                adv.building_assigned_at = now
                continue
            elapsed_minutes = (now - adv.stamina_updated_at).total_seconds() / 60
            if elapsed_minutes <= 0:
                continue
            current_stamina = adv.stamina
            elapsed_hours = elapsed_minutes / 60
            stamina_consumed_float = elapsed_hours * stamina_per_hour
            integer_consumed = int(stamina_consumed_float)
            if integer_consumed <= 0:
                continue
            actual_consumed = min(integer_consumed, current_stamina)
            progress_added = actual_consumed * seconds_per_stamina
            total_progress_added += progress_added
            helper_rec = open_helper_recs.get(adv.id)
            if helper_rec is not None:
                credit = min(int(progress_added), remaining_credit)
                if credit > 0:
                    helper_rec.contributed_seconds = (helper_rec.contributed_seconds or 0) + credit
                    remaining_credit -= credit
            adv.stamina = max(0, current_stamina - actual_consumed)
            if actual_consumed > 0:
                stamina_spent_by_helper[adv.id] = stamina_spent_by_helper.get(adv.id, 0) + actual_consumed
            consumed_seconds = actual_consumed / stamina_per_hour * 3600
            adv.stamina_updated_at = adv.stamina_updated_at + timedelta(seconds=consumed_seconds)
            if adv.stamina <= 0:
                if helper_rec is not None:
                    helper_rec.left_at = now
                AdventurerS1Service.enter_rest(adv, now)
                adv.building_instance_id = None
                adv.building_assigned_at = None
        if stamina_spent_by_helper:
            db.session.flush()
            from plugins.adventurer_s1.services.s1_contribution_service import S1ContributionService
            help_purpose = AdventurerS1Service._purpose_from_building_status(inst.status)
            help_source = AdventurerS1Service._contribution_source_type(_CONTRIBUTION_SRC_HELP, help_purpose)
            help_desc = '帮忙升级' if help_purpose == _DONATION_PURPOSE_UPGRADE else '帮忙建造'
            for helper_adv_id, stamina_spent in stamina_spent_by_helper.items():
                S1ContributionService.accrue(helper_adv_id, stamina_spent, source_type=help_source, related_id=inst.id, related_type='building_instance', description=help_desc, auto_commit=False)
        inst.accumulated_progress_seconds += int(total_progress_added)
        building_just_completed = False
        new_dorm_room_id = None
        dorm_naming_user_id = None
        dorm_upgrade_naming_user_id = None
        dorm_upgrade_room_id = None
        dorm_room_created = False
        if inst.accumulated_progress_seconds >= total_build_seconds:
            with AdventurerS1Service._building_completion_lock(inst.id):
                expected_status = inst.status
                with db.session.no_autoflush:
                    claimed = AdventurerS1BuildingInstance.query.filter(AdventurerS1BuildingInstance.id == inst.id, AdventurerS1BuildingInstance.status == expected_status).update({AdventurerS1BuildingInstance.status: 'built'}, synchronize_session=False)
                if claimed == 0:
                    try:
                        db_manager.refresh(inst)
                    except Exception:
                        db.session.refresh(inst)
                    return False
                was_upgrading = expected_status == 'upgrading'
                inst.status = 'built'
                inst.accumulated_progress_seconds = total_build_seconds
                building_just_completed = True
                inst.build_complete_at = now
                if was_upgrading:
                    inst.upgrade_level = (inst.upgrade_level or 1) + 1
                    inst.upgrade_started_at = None
                    inst.upgrade_complete_at = None
                    if inst.template and inst.template.category == 'dormitory':
                        dorm_upgrade_room_id, dorm_upgrade_naming_user_id = AdventurerS1Service._finalize_dormitory_upgrade_rooms(inst, now)
                with db.session.no_autoflush:
                    released_ids = [adv.id for adv in AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').all()]
                AdventurerS1Service._release_building_occupants(inst, now)
                if not was_upgrading and inst.template and inst.template.produced_beds and (inst.template.produced_beds > 0):
                    new_dorm_room, dorm_room_created = AdventurerS1Service._create_dorm_room_from_instance(inst)
                    if new_dorm_room:
                        new_dorm_room_id = new_dorm_room.id
                        dorm_naming_user_id = new_dorm_room.naming_user_id
        AdventurerS1Service._update_build_eta(inst, helpers=helpers, now=now)
        dormitory_changed = bool(dorm_upgrade_room_id or dorm_room_created)
        if commit:
            db_manager.commit()
            if dorm_room_created:
                AdventurerS1Service._maybe_auto_draw_after_dormitory_built()
            if dormitory_changed:
                invalidate_dormitory_cache()
            if stamina_spent_by_helper:
                from plugins.adventurer_s1.services.adventurer_s1_treasure_map_drop_service import AdventurerS1TreasureMapDropService
                AdventurerS1TreasureMapDropService.try_roll_building_stamina(stamina_spent_by_helper)
        else:
            if dormitory_changed and dormitory_effects is not None:
                dormitory_effects['dirty'] = True
            if treasure_roll_stamina is not None:
                for adventurer_id, stamina_spent in stamina_spent_by_helper.items():
                    treasure_roll_stamina[adventurer_id] = treasure_roll_stamina.get(adventurer_id, 0) + stamina_spent
        if building_just_completed:
            try:
                from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
                payload = {'action': 'building_completed', 'reason': 'building_finished', 'ids': released_ids}
                if new_dorm_room_id:
                    payload['new_dorm_room_id'] = new_dorm_room_id
                    if dorm_naming_user_id:
                        payload['dorm_naming_user_id'] = dorm_naming_user_id
                if dorm_upgrade_room_id:
                    payload['dorm_upgrade_room_id'] = dorm_upgrade_room_id
                    if dorm_upgrade_naming_user_id:
                        payload['dorm_upgrade_naming_user_id'] = dorm_upgrade_naming_user_id
                socketio.emit('adventurer_changed', payload, namespace='/')
            except Exception:
                pass
        return dorm_room_created

    @staticmethod
    def _create_dorm_room_from_instance(inst: AdventurerS1BuildingInstance) -> Tuple[Optional['AdventurerS1DormRoom'], bool]:
        """宿舍楼完工时按 template.produced_beds 创建一间 DormRoom 进入公共床位池。

        每个 building_instance_id 至多对应一间房；重复调用返回已有房间 (room, False)。

        房间名按容量自动选择格式：单人间/2人间/4人间 + A/B/C... 后缀，
        与种子命名风格保持一致。综合贡献最大的玩家（时长+材料各 50%）获得
        对该房间的一次性命名权（naming_user_id）。返回 (DormRoom|None, 是否新建)。
        """
        if not inst.template or not inst.template.produced_beds:
            return (None, False)
        if int(inst.upgrade_level or 1) > 1:
            return (None, False)
        existing = AdventurerS1DormRoom.query.filter_by(building_instance_id=inst.id).first()
        if existing:
            return (existing, False)
        naming_user_id = None
        try:
            naming_user_id = AdventurerS1Service._pick_dorm_naming_winner(inst)
        except Exception:
            logger.exception('宿舍命名权判定失败: instance_id=%s', inst.id)
        beds = int(inst.template.produced_beds)
        existing_room_names = {r.name for r in AdventurerS1DormRoom.query.filter(AdventurerS1DormRoom.name.like(f'{AdventurerS1Service._dorm_name_prefix(beds)}%')).all()}
        if inst.custom_name and inst.custom_name not in existing_room_names and AdventurerS1Service._is_valid_dorm_display_name(inst.custom_name, beds):
            room_name = inst.custom_name
        else:
            room_name = AdventurerS1Service._allocate_dorm_display_name(beds, exclude_instance_id=inst.id)
        room = AdventurerS1DormRoom(name=room_name, capacity=int(inst.template.produced_beds), level=1, naming_user_id=naming_user_id, building_instance_id=inst.id)
        ok, err = db_manager.add(room)
        if not ok:
            raced = AdventurerS1DormRoom.query.filter_by(building_instance_id=inst.id).first()
            if raced:
                return (raced, False)
            logger.error('宿舍楼完工建房间失败: instance_id=%s err=%s', inst.id, err)
            return (None, False)
        return (room, True)
    _SHOP_BUILDING_WHITELIST = frozenset({'herbalist_shop', 'armory_shop', 'forge_supply_shop'})

    @staticmethod
    def _is_item_sold_by(shop_code: str, item_name: str) -> bool:
        """判断 item_name 是否归属于指定 shop_code 商店的白名单。"""
        from plugins.adventurer_s1.seed import is_armory_item, is_forge_supply_item, is_herbalist_item
        if shop_code == 'herbalist_shop':
            return is_herbalist_item(item_name)
        if shop_code == 'armory_shop':
            return is_armory_item(item_name)
        if shop_code == 'forge_supply_shop':
            return is_forge_supply_item(item_name)
        return False

    @staticmethod
    def _resolve_shop_catalog_price(shop_code: str, item_name: str, template=None) -> Optional[int]:
        """常驻店按种子定价，限时摊位按模板活动配置定价。"""
        if template is not None and template.handler_key == LIMITED_SHOP_HANDLER:
            catalog = limited_shop_catalog(template)
            return catalog[2].get(item_name) if catalog else None
        from plugins.adventurer_s1.seed import get_s1_shop_catalog_price
        price = get_s1_shop_catalog_price(shop_code, item_name)
        if price is None:
            return None
        return int(price)

    @staticmethod
    def _shop_catalog_item_names(shop_code: str, template=None) -> tuple:
        if template is not None and template.handler_key == LIMITED_SHOP_HANDLER:
            catalog = limited_shop_catalog(template)
            return tuple(catalog[2]) if catalog else ()
        from plugins.adventurer_s1.seed import S1_ARMORY_ITEMS, S1_FORGE_SUPPLY_ITEMS, S1_HERBALIST_ITEMS
        if shop_code == 'herbalist_shop':
            return tuple((item[0] for item in S1_HERBALIST_ITEMS))
        if shop_code == 'armory_shop':
            return tuple((item[0] for item in S1_ARMORY_ITEMS))
        if shop_code == 'forge_supply_shop':
            return tuple((item[0] for item in S1_FORGE_SUPPLY_ITEMS))
        return ()

    @staticmethod
    def _check_shop_buy_eligibility(adv: 'AdventurerS1', item: 'AdventurerS1Item', shop_item: 'AdventurerS1ShopItem', shop_code: str, template_name: str, gold_balance: Optional[int]=None, template=None) -> Optional[str]:
        """返回 None 表示可购买，否则为拒绝原因（供 list / buy 共用）。"""
        from plugins.adventurer_s1.services.s1_item_grant_service import resolve_item_storage_destination
        if template is not None and template.handler_key == LIMITED_SHOP_HANDLER:
            rejection = limited_shop_rejection(template)
            if rejection:
                return rejection
        catalog_price = AdventurerS1Service._resolve_shop_catalog_price(shop_code, item.name, template)
        if catalog_price is None:
            return f'「{item.name}」不在「{template_name}」出售'
        if (shop_item.stock or 0) <= 0:
            return f'「{item.name}」已售罄'
        can, reason = adv.can_dispatch()
        if not can:
            return reason
        _, dest_err = resolve_item_storage_destination(adv, item.name)
        if dest_err:
            return dest_err
        if catalog_price < 0:
            return '商品定价异常'
        if gold_balance is not None and gold_balance < catalog_price:
            return f'金币不足（需要 {catalog_price}，当前 {gold_balance}）'
        return None

    @staticmethod
    def list_shop_items(building_instance_id: int, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """列出商店建筑的可售商品（含当前冒险者的购买可行性预览）。"""
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.status != 'built':
            return (None, '建筑尚未建成')
        template = inst.template
        if not template:
            return (None, '建筑模板缺失')
        if template.code not in AdventurerS1Service._SHOP_BUILDING_WHITELIST and template.handler_key != LIMITED_SHOP_HANDLER:
            return (None, f'「{template.name}」目前不出售商品')
        catalog_names = AdventurerS1Service._shop_catalog_item_names(template.code, template)
        if not catalog_names:
            return (None, f'「{template.name}」目前不出售商品')
        shop_rows = AdventurerS1ShopItem.query.options(joinedload(AdventurerS1ShopItem.item)).join(AdventurerS1Item, AdventurerS1ShopItem.item_id == AdventurerS1Item.id).filter(AdventurerS1Item.name.in_(catalog_names)).order_by(AdventurerS1ShopItem.price.asc(), AdventurerS1Item.name.asc()).all()
        adv = AdventurerS1.query.filter_by(owner_id=user_id).filter(AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).order_by(AdventurerS1.id.asc()).first()
        adv_summary = None
        gold_balance = 0
        if adv:
            balance, err = S1CurrencyService.get_balance(adv.id, 'gold')
            if err:
                return (None, err)
            gold_balance = int(balance or 0)
            adv_summary = {'id': adv.id, 'name': adv.name, 'gold': gold_balance}
        items = []
        for shop_item in shop_rows:
            item = shop_item.item
            if not item:
                continue
            catalog_price = AdventurerS1Service._resolve_shop_catalog_price(template.code, item.name, template)
            if catalog_price is None:
                continue
            reject_reason = None
            if adv:
                reject_reason = AdventurerS1Service._check_shop_buy_eligibility(adv, item, shop_item, template.code, template.name, gold_balance=gold_balance, template=template)
            else:
                reject_reason = '你还没有冒险者，请先创建'
            if template.handler_key == LIMITED_SHOP_HANDLER and inst.disabled:
                reject_reason = '摊位已暂停开放'
            from plugins.adventurer_s1.seed import get_s1_shop_catalog_purchase_quantity
            purchase_quantity = get_s1_shop_catalog_purchase_quantity(template.code, item.name)
            item_payload = {'id': shop_item.id, 'name': item.name, 'type': item.type, 'description': item.description or '', 'rarity': item.rarity or 'common', 'price': catalog_price, 'stock': int(shop_item.stock or 0), 'immutable': (item.use_condition or {}).get('immutable') is True, 'can_buy': reject_reason is None, 'reject_reason': reject_reason}
            if purchase_quantity > 1:
                item_payload['quantity'] = purchase_quantity
            items.append(item_payload)
        return ({'building_id': inst.id, 'shop_name': template.name, 'shop_code': template.code, 'sale_ends_at': limited_shop_catalog(template)[1].isoformat() if template.handler_key == LIMITED_SHOP_HANDLER else None, 'sale_starts_at': limited_shop_catalog(template)[0].isoformat() if template.handler_key == LIMITED_SHOP_HANDLER else None, 'server_now': datetime.now(timezone.utc).isoformat(), 'sale_closed_reason': ('摊位已暂停开放' if inst.disabled else limited_shop_rejection(template)) if template.handler_key == LIMITED_SHOP_HANDLER else None, 'adventurer': adv_summary, 'items': items}, None)

    @staticmethod
    def buy_from_shop(building_instance_id: int, adventurer_id: int, shop_item_id: int, user_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """从已建成的商店建筑里购买物品并装到指定冒险者随身或仓库。

        校验（顺序就是错误返回顺序，便于前端给针对性提示）：
        1. 建筑：存在、已建成、是 _SHOP_BUILDING_WHITELIST 里的商店类型
        2. 商品：存在、属于该商店白名单、有库存
        3. 冒险者：存在、属于 user、空闲态；随身满时非草药店类改入仓库
        4. 金币：余额 >= price

        事务体：原子扣库存 + 扣金币 + 创建 Equipment 或 WarehouseItem。
        """
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.status != 'built':
            return (None, '建筑尚未建成')
        template = inst.template
        if not template:
            return (None, '建筑模板缺失')
        if template.code not in AdventurerS1Service._SHOP_BUILDING_WHITELIST and template.handler_key != LIMITED_SHOP_HANDLER:
            return (None, f'「{template.name}」目前不出售商品')
        shop_item = AdventurerS1ShopItem.query.session.get(AdventurerS1ShopItem, shop_item_id)
        if not shop_item:
            return (None, '商品不存在')
        item = shop_item.item
        if not item:
            return (None, '商品图鉴缺失')
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return (None, '冒险者不存在')
        if not is_admin and adv.owner_id != user_id:
            return (None, '只能让自己的冒险者购买')
        reject_reason = AdventurerS1Service._check_shop_buy_eligibility(adv, item, shop_item, template.code, template.name, template=template)
        if reject_reason:
            return (None, reject_reason)
        price = AdventurerS1Service._resolve_shop_catalog_price(template.code, item.name, template)
        if price is None:
            return (None, f'「{item.name}」不在「{template.name}」出售')
        from plugins.adventurer_s1.seed import get_s1_shop_catalog_purchase_quantity
        purchase_quantity = get_s1_shop_catalog_purchase_quantity(template.code, item.name)
        stock_before = int(shop_item.stock or 0)
        balance, err = S1CurrencyService.get_balance(adventurer_id, 'gold')
        if err:
            return (None, err)
        if (balance or 0) < price:
            return (None, f'金币不足（需要 {price}，当前 {int(balance or 0)}）')
        from plugins.adventurer_s1.seed import default_catalog_item_uses_left
        from plugins.adventurer_s1.services.s1_item_grant_service import resolve_item_storage_destination, grant_raw_item
        uses_left = default_catalog_item_uses_left(item.name, item.type or '')
        destination, dest_err = resolve_item_storage_destination(adv, item.name)
        if dest_err:
            return (None, dest_err)
        try:
            with db_manager.transaction():
                if template.handler_key == LIMITED_SHOP_HANDLER:
                    adv = AdventurerS1.query.filter_by(id=adventurer_id).populate_existing().with_for_update().one()
                    inst = AdventurerS1BuildingInstance.query.filter_by(id=building_instance_id).populate_existing().with_for_update(read=True).one()
                    if inst.status != 'built' or inst.disabled:
                        raise S1PlayerVisibleError('摊位暂未开放')
                    template = AdventurerS1BuildingTemplate.query.filter_by(id=template.id).populate_existing().with_for_update(read=True).one()
                    if template.handler_key != LIMITED_SHOP_HANDLER or inst.template_id != template.id:
                        raise S1PlayerVisibleError('摊位配置已变更，请刷新后重试')
                    if not is_admin and adv.owner_id != user_id:
                        raise S1PlayerVisibleError('只能让自己的冒险者购买')
                    reject_reason = AdventurerS1Service._check_shop_buy_eligibility(adv, item, shop_item, template.code, template.name, template=template)
                    if reject_reason:
                        raise S1PlayerVisibleError(reject_reason)
                    price = AdventurerS1Service._resolve_shop_catalog_price(template.code, item.name, template)
                    destination, dest_err = resolve_item_storage_destination(adv, item.name)
                    if dest_err:
                        raise S1PlayerVisibleError(dest_err)
                affected = AdventurerS1ShopItem.query.filter(AdventurerS1ShopItem.id == shop_item_id, AdventurerS1ShopItem.stock > 0).update({AdventurerS1ShopItem.stock: AdventurerS1ShopItem.stock - 1}, synchronize_session=False)
                if affected == 0:
                    raise S1PlayerVisibleError(f'「{item.name}」已售罄')
                if price > 0:
                    _, dedu_err = S1CurrencyService.deduct(adventurer_id, 'gold', float(price), source_type='shop_buy', related_id=shop_item_id, related_type='shop_item', description=f'{template.name}购买「{item.name}」', auto_commit=False)
                    if dedu_err:
                        raise S1PlayerVisibleError(dedu_err)
                if template.handler_key == LIMITED_SHOP_HANDLER:
                    rejection = limited_shop_rejection(template)
                    if rejection:
                        raise S1PlayerVisibleError(rejection)
                grant_data = {'type': item.type, 'rarity': item.rarity or 'common', 'quantity': purchase_quantity}
                if template.handler_key != LIMITED_SHOP_HANDLER:
                    grant_data['uses_left'] = uses_left
                granted_item = {}
                granted_destination, grant_err = grant_raw_item(adv, item.name, grant_data, source_type='shop', description=f'{template.name}购买「{item.name}」', force_destination=destination, granted_item_out=granted_item)
                if grant_err:
                    raise S1PlayerVisibleError(grant_err)
                if template.handler_key == LIMITED_SHOP_HANDLER:
                    destination = granted_destination
                    uses_left = granted_item['item'].uses_left
                    rejection = limited_shop_rejection(template)
                    if rejection:
                        raise S1PlayerVisibleError(rejection)
        except Exception as e:
            return (None, player_operation_failure(e, '购买失败'))
        item_dict, clue_meta = AdventurerS1Service._build_item_catalog_context(adv)
        return ({'adventurer': adv.to_dict(item_dict=item_dict, item_clue_meta=clue_meta, current_user_id=user_id, is_admin=is_admin), 'purchased': {'name': item.name, 'type': item.type, 'rarity': item.rarity or 'common', 'price': price, 'quantity': purchase_quantity, 'uses_left': uses_left, 'destination': destination}, 'remaining_stock': max(0, stock_before - 1)}, None)

    @staticmethod
    def complete_building(building_instance_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """GM 一键建成：把建筑立刻置为 built。

        支持 status='locked'、'building' 和 'upgrading'：
        - locked → 直接跳到 built（跳过建造阶段，无需材料）
        - building → 跳过剩余时长，释放冒险者
        - upgrading → 跳过升级时长，提升等级，释放冒险者
        - 宿舍楼会自动新建 DormRoom（复用 _create_dorm_room_from_instance）
        - 推 WebSocket building_completed 事件
        """
        if not is_admin:
            return (None, '仅管理员可操作')
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.status not in ('locked', 'building', 'upgrading'):
            return (None, f'建筑当前状态为「{inst.status}」，无法一键建成')
        now = _portable_utc_now()
        helpers = AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').all()
        helper_ids = [h.id for h in helpers]
        new_dorm_room_id = None
        dorm_naming_user_id = None
        dorm_upgrade_naming_user_id = None
        dorm_upgrade_room_id = None
        dorm_room_created = False
        try:
            with db_manager.transaction():
                with AdventurerS1Service._building_completion_lock(inst.id):
                    db_manager.refresh(inst)
                    if inst.status == 'built':
                        existing_room = AdventurerS1DormRoom.query.filter_by(building_instance_id=inst.id).first()
                        if existing_room:
                            new_dorm_room_id = existing_room.id
                            dorm_naming_user_id = existing_room.naming_user_id
                    elif inst.status == 'upgrading':
                        inst.status = 'built'
                        inst.upgrade_level = (inst.upgrade_level or 1) + 1
                        inst.upgrade_started_at = None
                        inst.upgrade_complete_at = None
                        inst.accumulated_progress_seconds = 0
                        inst.build_complete_at = now
                        AdventurerS1Service._release_building_occupants(inst, now)
                        if inst.template and inst.template.category == 'dormitory':
                            dorm_upgrade_room_id, dorm_upgrade_naming_user_id = AdventurerS1Service._finalize_dormitory_upgrade_rooms(inst, now)
                    else:
                        total_build_seconds = inst.template.build_hours * 3600 if inst.template else 0
                        inst.status = 'built'
                        inst.build_started_at = now
                        inst.accumulated_progress_seconds = total_build_seconds
                        inst.build_complete_at = now
                        AdventurerS1Service._release_building_occupants(inst, now)
                        if inst.template and inst.template.produced_beds and (inst.template.produced_beds > 0):
                            new_dorm_room, dorm_room_created = AdventurerS1Service._create_dorm_room_from_instance(inst)
                            if new_dorm_room:
                                new_dorm_room_id = new_dorm_room.id
                                dorm_naming_user_id = new_dorm_room.naming_user_id
        except Exception as e:
            return (None, f'操作失败：{e}')
        if dorm_room_created:
            AdventurerS1Service._maybe_auto_draw_after_dormitory_built()
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            payload = {'action': 'building_completed', 'reason': 'admin_force_complete', 'ids': helper_ids}
            if new_dorm_room_id:
                payload['new_dorm_room_id'] = new_dorm_room_id
                if dorm_naming_user_id:
                    payload['dorm_naming_user_id'] = dorm_naming_user_id
            if dorm_upgrade_room_id:
                payload['dorm_upgrade_room_id'] = dorm_upgrade_room_id
                if dorm_upgrade_naming_user_id:
                    payload['dorm_upgrade_naming_user_id'] = dorm_upgrade_naming_user_id
            socketio.emit('adventurer_changed', payload, namespace='/')
        except Exception:
            pass
        invalidate_buildings_list_cache()
        if dorm_upgrade_room_id or dorm_room_created:
            invalidate_dormitory_cache()
        return (inst.to_dict(include_helpers=True), None)

    @staticmethod
    def fill_dormitory_materials(building_instance_id: int, is_admin: bool=False) -> Tuple[Optional[dict], Optional[str]]:
        """GM 一键补满材料：把 locked 宿舍楼缺的材料加到 applicant 第一个非死亡冒险者头上。

        用于调试 / 测试：跳过冒险者去伐木 / 采石的环节。
        - 仅限 dormitory 类、status='locked' 的实例
        - 必须有 applicant_user_id（玩家自申请的实例）
        - 按 owner 名下材料汇总算缺口，缺多少就给多少
        - 已够则原样返回（no-op）
        """
        if not is_admin:
            return (None, '仅管理员可操作')
        from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService, MATERIAL_META
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if not inst.template or inst.template.category != 'dormitory':
            return (None, '仅宿舍楼支持补充材料')
        if inst.status != 'locked':
            return (None, f'宿舍楼当前状态为「{inst.status}」，无法补充材料')
        if not inst.applicant_user_id:
            return (None, '该实例无申请人，无法定位材料归属')
        required = inst.template.required_materials or {}
        if not required:
            return (None, '该宿舍楼模板未配置材料需求')
        target_adv = AdventurerS1.query.filter(AdventurerS1.owner_id == inst.applicant_user_id, AdventurerS1.is_player_created == True, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).order_by(AdventurerS1.id).first()
        if not target_adv:
            return (None, '申请人名下无在场冒险者，无法补充材料')
        totals, err = S1MaterialService.get_owner_totals(inst.applicant_user_id)
        if err:
            return (None, err)
        filled = {}
        for code, need in required.items():
            need = int(need)
            have = int(totals.get(code, 0))
            gap = need - have
            if gap <= 0:
                continue
            _, add_err = S1MaterialService.add(target_adv.id, code, gap, source_type='admin_adjust', related_id=inst.id, related_type='building_instance', description=f"GM 补满{inst.custom_name or '宿舍楼'}材料")
            if add_err:
                return (None, f'补 {code} 失败：{add_err}')
            filled[code] = gap
        return ({'instance': inst.to_dict(), 'target_adventurer_id': target_adv.id, 'target_adventurer_name': target_adv.name, 'filled': filled}, None)

    @staticmethod
    def reset_building(building_instance_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员重置建筑状态为 locked。"""
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        try:
            with db_manager.transaction():
                inst = AdventurerS1BuildingInstance.query.filter_by(id=building_instance_id).populate_existing().with_for_update().first()
                if not inst:
                    return (None, '建筑不存在')
                if AdventurerS1Service._building_usage_queue_waiting_count(inst.id) > 0:
                    return (None, '该建筑仍有冒险者候场，请先等待递补完成或由玩家取消候场')
                if AdventurerS1Service._queueable_building_active_usage_count(inst) > 0:
                    return (None, '该建筑仍有冒险者正在使用，请等待本次使用结束后再重置')
                AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').update({'status': 'resting', 'stamina_updated_at': _portable_utc_now(), 'building_instance_id': None, 'building_assigned_at': None})
                inst.status = 'locked'
                inst.build_started_at = None
                inst.build_complete_at = None
                inst.accumulated_progress_seconds = 0
                AdventurerS1BuildingHelper.query.filter_by(building_instance_id=inst.id).delete()
        except Exception as e:
            return (None, f'操作失败：{e}')
        return (inst.to_dict(), None)

    @staticmethod
    def rename_building_instance(building_instance_id: int, custom_name: str) -> Tuple[Optional[dict], Optional[str]]:
        """管理员重命名建筑实例。"""
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        try:
            with db_manager.transaction():
                inst.custom_name = custom_name if custom_name else None
        except Exception as e:
            return (None, f'操作失败：{e}')
        return (inst.to_dict(), None)

    @staticmethod
    def create_building_instance(template_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员创建建筑实例（仅限可多个建筑类型）。"""
        tpl = AdventurerS1BuildingTemplate.query.session.get(AdventurerS1BuildingTemplate, template_id)
        if not tpl:
            return (None, '模板不存在')
        if tpl.building_type != 'multiple':
            return (None, '该建筑类型为唯一建筑，不能创建多个实例')
        from plugins.adventurer_s1.seed import make_initial_building_instance
        inst = make_initial_building_instance(tpl)
        try:
            with db_manager.transaction():
                db_manager.add(inst)
        except Exception as e:
            return (None, f'创建失败：{e}')
        return (inst.to_dict(), None)

    @staticmethod
    def delete_building_instance(building_instance_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """管理员删除建筑实例（仅限可多个建筑类型，且状态为 locked）。"""
        inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
        if not inst:
            return (None, '建筑不存在')
        if inst.template and inst.template.building_type != 'multiple':
            return (None, '唯一建筑不能删除')
        if inst.status != 'locked':
            return (None, '只能删除未开始建造的建筑')
        try:
            with db_manager.transaction() as session:
                inst = AdventurerS1BuildingInstance.query.filter_by(id=building_instance_id).populate_existing().with_for_update().first()
                if not inst:
                    return (None, '建筑不存在')
                if inst.template and inst.template.building_type != 'multiple':
                    return (None, '唯一建筑不能删除')
                if inst.status != 'locked':
                    return (None, '只能删除未开始建造的建筑')
                if AdventurerS1Service._building_usage_queue_waiting_count(inst.id) > 0:
                    return (None, '该建筑仍有冒险者候场，请先由玩家取消候场')
                AdventurerS1.query.filter_by(building_instance_id=inst.id, status='building').update({'status': 'resting', 'stamina_updated_at': _portable_utc_now(), 'building_instance_id': None, 'building_assigned_at': None})
                session.delete(inst)
        except Exception as e:
            return (None, f'删除失败：{e}')
        return ({'deleted': building_instance_id}, None)

    @staticmethod
    def admin_preview_orphan_lv2_instances() -> Tuple[dict, Optional[str]]:
        """预览将被清理的 Lv2 锁定建筑无主残留实例（只读 dry-run，预览所见即所删）。"""
        from plugins.adventurer_s1.seed import _find_orphan_lv2_locked_instances
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        insts = _find_orphan_lv2_locked_instances()
        orphans = [{'id': i.id, 'code': i.template.code if i.template else None, 'name': i.template.name if i.template else None, 'status': i.status} for i in insts]
        return ({'orphans': orphans, 'count': len(orphans), 'tavern_level': int(S1SettingsService.get('tavern_level') or 1)}, None)

    @staticmethod
    def admin_purge_orphan_lv2_instances() -> Tuple[dict, Optional[str]]:
        """清理 Lv2 锁定建筑的无主残留实例（无帮工 + 无捐献，幂等）。"""
        from plugins.adventurer_s1.seed import _purge_orphan_lv2_locked_instances
        purged = _purge_orphan_lv2_locked_instances()
        return ({'purged': purged}, None)

    @staticmethod
    def list_building_templates() -> Tuple[dict, Optional[str]]:
        """获取所有建筑模板（含已下架的）。"""
        templates = AdventurerS1BuildingTemplate.query.order_by(AdventurerS1BuildingTemplate.sort_order).all()
        return ({'templates': [t.to_dict() for t in templates]}, None)

    @staticmethod
    def create_building_template(code: str, name: str, icon: str, description: str, building_type: str, category: str, build_hours: int, helper_slots: int, stamina_cost_per_help: int, speedup_minutes: int, sort_order: int=0, handler_key: str=None) -> Tuple[Optional[dict], Optional[str]]:
        """新建建筑模板。"""
        if not name or not name.strip():
            return (None, '建筑名称不能为空')
        code = (code or '').strip() or name.strip().replace('·', '_').replace(' ', '_')
        existing = AdventurerS1BuildingTemplate.query.filter_by(code=code).first()
        if existing:
            return (None, f'建筑标识「{code}」已存在（{existing.name}）')
        if building_type not in ('unique', 'multiple'):
            building_type = 'unique'
        if category not in ('general', 'dormitory', 'medical', 'leisure', 'equipment', 'gathering', 'training', 'market', 'work', 'task', 'memorial', 'auction'):
            category = 'general'
        tpl = AdventurerS1BuildingTemplate(code=code, name=name.strip(), icon=icon or '🏗️', description=description or '', building_type=building_type, category=category, build_hours=max(1, build_hours), helper_slots=max(1, helper_slots), stamina_cost_per_help=max(1, stamina_cost_per_help), speedup_minutes=max(1, speedup_minutes), sort_order=sort_order, handler_key=handler_key or None)
        try:
            with db_manager.transaction():
                db_manager.add(tpl)
                if building_type == 'unique':
                    from plugins.adventurer_s1.seed import make_initial_building_instance
                    db_manager.add(make_initial_building_instance(tpl))
        except Exception as e:
            return (None, f'创建失败：{e}')
        return (tpl.to_dict(), None)

    @staticmethod
    def update_building_template(template_id: int, name: str=None, icon: str=None, description: str=None, building_type: str=None, category: str=None, build_hours: int=None, helper_slots: int=None, stamina_cost_per_help: int=None, speedup_minutes: int=None, sort_order: int=None, is_active: bool=None, handler_key: str=None) -> Tuple[Optional[dict], Optional[str]]:
        """编辑建筑模板。"""
        tpl = AdventurerS1BuildingTemplate.query.session.get(AdventurerS1BuildingTemplate, template_id)
        if not tpl:
            return (None, '模板不存在')
        try:
            with db_manager.transaction():
                tpl = AdventurerS1BuildingTemplate.query.filter_by(id=template_id).populate_existing().with_for_update().first()
                if not tpl:
                    return (None, '模板不存在')
                if handler_key is not None or is_active is False:
                    locked_instances = AdventurerS1BuildingInstance.query.filter_by(template_id=template_id).order_by(AdventurerS1BuildingInstance.id.asc()).with_for_update().all()
                    waiting_count, active_count = AdventurerS1Service._queueable_building_usage_counts_for_instances(locked_instances, tpl.handler_key)
                    if waiting_count > 0:
                        return (None, f'该模板下仍有 {waiting_count} 名冒险者候场，请先取消候场再变更处理器或下架模板')
                    if active_count > 0:
                        return (None, f'该模板下仍有 {active_count} 名冒险者正在使用设施，请等待本次使用结束后再变更处理器或下架模板')
                if name is not None:
                    name = name.strip()
                    if not name:
                        return (None, '建筑名称不能为空')
                    dup = AdventurerS1BuildingTemplate.query.filter(AdventurerS1BuildingTemplate.name == name, AdventurerS1BuildingTemplate.id != template_id).first()
                    if dup:
                        return (None, f'建筑名称「{name}」已存在')
                    tpl.name = name
                if icon is not None:
                    tpl.icon = icon
                if description is not None:
                    tpl.description = description
                if building_type is not None and building_type in ('unique', 'multiple'):
                    tpl.building_type = building_type
                if category is not None and category in ('general', 'dormitory', 'medical', 'leisure', 'equipment', 'gathering', 'training', 'market', 'work', 'task'):
                    tpl.category = category
                if handler_key is not None:
                    tpl.handler_key = handler_key or None
                if build_hours is not None:
                    tpl.build_hours = max(1, build_hours)
                if helper_slots is not None:
                    tpl.helper_slots = max(1, helper_slots)
                if stamina_cost_per_help is not None:
                    tpl.stamina_cost_per_help = max(1, stamina_cost_per_help)
                if speedup_minutes is not None:
                    tpl.speedup_minutes = max(1, speedup_minutes)
                if sort_order is not None:
                    tpl.sort_order = sort_order
                if is_active is not None:
                    tpl.is_active = is_active
        except Exception as e:
            return (None, f'更新失败：{e}')
        return (tpl.to_dict(), None)

    @staticmethod
    def delete_building_template(template_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """删除建筑模板（连同实例和帮忙记录）。"""
        tpl = AdventurerS1BuildingTemplate.query.session.get(AdventurerS1BuildingTemplate, template_id)
        if not tpl:
            return (None, '模板不存在')
        try:
            with db_manager.transaction() as session:
                tpl = AdventurerS1BuildingTemplate.query.filter_by(id=template_id).populate_existing().with_for_update().first()
                if not tpl:
                    return (None, '模板不存在')
                locked_instances = AdventurerS1BuildingInstance.query.filter_by(template_id=template_id).order_by(AdventurerS1BuildingInstance.id.asc()).with_for_update().all()
                waiting_count, active_count = AdventurerS1Service._queueable_building_usage_counts_for_instances(locked_instances, tpl.handler_key)
                if waiting_count > 0:
                    return (None, f'该模板下仍有 {waiting_count} 名冒险者候场，请先取消候场')
                if active_count > 0:
                    return (None, f'该模板下仍有 {active_count} 名冒险者正在使用设施，请等待使用结束')
                session.delete(tpl)
        except Exception as e:
            return (None, f'删除失败：{e}')
        return ({'deleted': template_id}, None)
    _ACTIVITY_LIMIT_MAX = 50
    _ACTIVITY_BUILDING_FILTER_HANDLERS = {'building': None, 'blacksmith': frozenset({'blacksmith'}), 'tailoring': frozenset({'tailor_workshop'}), 'alchemy': frozenset({'alchemy_workshop'}), 'assembly': frozenset({'item_assembly_workshop'}), 'cooking': frozenset({'ember_kitchen'}), 'healing': frozenset({'healing_house', 'chapel', 'hospital', 'clinic', 'sanatorium'}), 'training': frozenset({'skill_school', 'training'}), 'pet_evolution': frozenset({'pet_evolution'})}
    _ACTIVITY_LOG_FILTERS = frozenset({'currency', 'materials', 'items', 'trait_recovery'})
    _ACTIVITY_FILTERS = frozenset({'all'}) | frozenset(_ACTIVITY_BUILDING_FILTER_HANDLERS) | _ACTIVITY_LOG_FILTERS
    _BUILDING_REWARD_MERGE_SECONDS = 5
    _BUILDING_ASYNC_COST_MERGE_SECONDS = 7200
    _ASYNC_CRAFT_RESULT_TYPES = frozenset({'blacksmith', 'tailor_workshop', 'alchemy_workshop', 'item_assembly_workshop', 'ember_kitchen', 'skill_training'})
    _BUILDING_USAGE_COST_SOURCE_TYPES = ('building_usage_cost', 'skill_school_learn', 'skill_school_custom_learn', 'skill_school_custom_advance', 'blacksmith_usage_cost', 'tailor_workshop_usage_cost', 'alchemy_workshop_usage_cost', 'assembly_workshop_usage_cost', 'ember_kitchen_usage_cost')
    _BUILDING_USAGE_REFUND_SOURCE_TYPES = ('building_usage_refund', 'building_usage_abort_refund', 'skill_training_system_refund')
    _CRAFT_ACTIVITY_ITEM_SOURCES = frozenset({'blacksmith', 'tailor_workshop', 'alchemy_workshop', 'item_assembly_workshop', 'ember_kitchen'})
    _APPEAL_ITEM_GRANT_ACTIVITY_SOURCES = frozenset({'data_appeal_ai', 'data_appeal_reclaim'})

    @staticmethod
    def _index_building_usage_rewards(currency_logs, material_logs, currency_types: dict) -> dict:
        """把建筑使用产生的货币/材料流水编入索引，供动态时间线折叠进建筑条目。

        同时索引：
        - 结算奖励（source_type='building_usage_complete'）→ 归入 'reward'
        - 使用扣费（source_type in _BUILDING_USAGE_COST_SOURCE_TYPES）→ 归入 'cost'
        """
        from plugins.adventurer_s1.services.adventurer_s1_material_service import MATERIAL_META
        cost_source_types = AdventurerS1Service._BUILDING_USAGE_COST_SOURCE_TYPES
        index: dict = {}
        for log in currency_logs:
            if log.related_type != 'building_instance':
                continue
            if log.source_type == 'building_usage_complete':
                tag = 'reward'
            elif log.source_type in cost_source_types:
                tag = 'cost'
            elif log.source_type in AdventurerS1Service._BUILDING_USAGE_REFUND_SOURCE_TYPES:
                tag = 'refund'
            else:
                continue
            try:
                inst_id = int(log.related_id)
            except (TypeError, ValueError):
                continue
            meta = currency_types.get(log.currency_code, {})
            item = {'kind': 'currency', 'code': log.currency_code, 'display_name': meta.get('name', log.currency_code), 'icon': meta.get('icon', '💰'), 'change_amount': round(float(log.change_amount or 0), 2), 'description': log.description or ''}
            key = (log.adventurer_id, inst_id)
            index.setdefault(key, []).append((log.created_at, 'currency', log.id, item, tag))
        for log in material_logs:
            if log.related_type != 'building_instance':
                continue
            if log.source_type == 'building_usage_complete':
                tag = 'reward'
            elif log.source_type in cost_source_types:
                tag = 'cost'
            else:
                continue
            try:
                inst_id = int(log.related_id)
            except (TypeError, ValueError):
                continue
            meta = MATERIAL_META.get(log.name, {})
            item = {'kind': 'material', 'code': log.name, 'display_name': meta.get('name', log.name), 'icon': meta.get('icon', '📦'), 'change_amount': int(log.change_amount or 0), 'description': log.description or ''}
            key = (log.adventurer_id, inst_id)
            index.setdefault(key, []).append((log.created_at, 'material', log.id, item, tag))
        for bucket in index.values():
            bucket.sort(key=lambda x: x[0])
        return index

    @staticmethod
    def _pick_building_reward(index: dict, adventurer_id: int, building_instance_id: int, usage_at: datetime, consumed: set, *, async_craft: bool=False) -> tuple:
        """为一条建筑使用记录匹配最近的结算/扣费/退款流水。

        同步建筑（采集等）：扣费与 usage_log 同事务，5 秒内匹配。
        异步锻造/炼金：派遣时扣费、AI 完成后写 usage_log，按「同实例、usage 前最近扣费」匹配。

        返回 (reward, cost, refund) 三元组。
        """
        bucket = index.get((adventurer_id, building_instance_id), [])
        tight = AdventurerS1Service._BUILDING_REWARD_MERGE_SECONDS
        async_window = max(1, int(current_app.config.get('ADVENTURER_S1_BUILDING_ASYNC_COST_MERGE_SECONDS', AdventurerS1Service._BUILDING_ASYNC_COST_MERGE_SECONDS)))
        best_reward = None
        best_reward_diff = None
        best_cost = None
        best_cost_diff = None
        best_refund = None
        best_refund_diff = None
        async_cost_candidate = None
        for created_at, kind, log_id, item, tag in bucket:
            token = (kind, log_id)
            if token in consumed:
                continue
            diff = abs((usage_at - created_at).total_seconds())
            if tag == 'reward':
                if diff > tight:
                    continue
                if best_reward is None or diff < best_reward_diff:
                    best_reward = (token, item)
                    best_reward_diff = diff
            elif tag == 'refund':
                if diff > tight:
                    continue
                if best_refund is None or diff < best_refund_diff:
                    best_refund = (token, item)
                    best_refund_diff = diff
            elif tag == 'cost':
                if diff <= tight:
                    if best_cost is None or diff < best_cost_diff:
                        best_cost = (token, item)
                        best_cost_diff = diff
                elif async_craft and created_at <= usage_at:
                    lag = (usage_at - created_at).total_seconds()
                    if lag <= async_window:
                        if async_cost_candidate is None or lag < async_cost_candidate[0]:
                            async_cost_candidate = (lag, token, item)
        reward = None
        cost = None
        refund = None
        if best_reward:
            consumed.add(best_reward[0])
            reward = best_reward[1]
        if best_cost:
            consumed.add(best_cost[0])
            cost = best_cost[1]
        elif async_cost_candidate:
            consumed.add(async_cost_candidate[1])
            cost = async_cost_candidate[2]
        if best_refund:
            consumed.add(best_refund[0])
            refund = best_refund[1]
        return (reward, cost, refund)

    @staticmethod
    def get_my_activity(user_id: int, limit: int=20, before: Optional[str]=None, settle_due_trait_recovery: bool=False, activity_filter: str='all') -> Tuple[Optional[dict], Optional[str]]:
        """聚合当前玩家名下冒险者的最近动态，供「我的」页签时间线渲染。

        合并四张日志表按 created_at 倒序取前 limit 条：
        - AdventurerS1CurrencyLog —— 金币/酒馆币增减（含来源描述）
        - AdventurerS1MaterialLog —— 木材/石料增减
        - AdventurerS1ItemLog —— 物品/装备获得与消耗
        - AdventurerS1BuildingUsageLog —— 建筑使用记录；治疗所/铁匠铺的 result
          带成功与否 + 叙事，离线期间自动结算的结果靠这里回看（socket 弹窗仅在线可见）

        before: ISO 时间游标，只取「严格早于」该时刻的记录，用于加载更多。
        同一秒跨表并发写入时游标可能跳过极少量记录，活动流场景可接受。
        activity_filter: 白名单动态类别；设施类按 handler_key 在分页前过滤，
        流水类只查询对应日志表，避免为单类筛选读取无关数据。
        """
        from sqlalchemy.orm import joinedload
        from plugins.adventurer_s1.models import AdventurerS1BuildingUsageLog, AdventurerS1CurrencyLog, AdventurerS1ItemLog, AdventurerS1MaterialLog, AdventurerS1Trait
        from plugins.adventurer_s1.services.adventurer_s1_material_service import MATERIAL_META
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import build_trait_recovery_activity_description
        from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_clue_meta_by_item_names, sanitize_player_currency_log_entries, sanitize_player_item_log_entry
        from utils.time_utils import to_utc_iso, parse_iso_date
        limit = max(1, min(int(limit or 20), AdventurerS1Service._ACTIVITY_LIMIT_MAX))
        activity_filter = str(activity_filter or 'all').strip().lower()
        if activity_filter not in AdventurerS1Service._ACTIVITY_FILTERS:
            return (None, '不支持的动态筛选')
        before_dt = parse_iso_date(before) if before else None
        cache_key = f"{int(user_id)}:{activity_filter}:{limit}:{before or ''}"
        if not before_dt:
            cached = _my_activity_cache.get(cache_key)
            if cached is not None:
                return (cached, None)
        adv_ids = [row[0] for row in AdventurerS1.query.with_entities(AdventurerS1.id).filter(AdventurerS1.owner_id == user_id).all()]
        if not adv_ids:
            return ({'entries': [], 'has_more': False, 'next_before': None}, None)
        _ = settle_due_trait_recovery

        def _recent(query, model):
            return recent_activity_query(query, model, adv_ids, limit=limit, before=before_dt).all()
        try:
            building_filter_active = activity_filter in AdventurerS1Service._ACTIVITY_BUILDING_FILTER_HANDLERS
            include_all_sources = activity_filter == 'all'
            usage_logs = []
            currency_logs = []
            material_logs = []
            trait_logs = []
            item_logs = []
            if include_all_sources or building_filter_active:
                usage_query = AdventurerS1BuildingUsageLog.query.options(joinedload(AdventurerS1BuildingUsageLog.building_instance).raiseload('*'), joinedload(AdventurerS1BuildingUsageLog.building_instance).joinedload(AdventurerS1BuildingInstance.template).raiseload('*'))
                if building_filter_active:
                    handler_keys = AdventurerS1Service._ACTIVITY_BUILDING_FILTER_HANDLERS[activity_filter]
                    if handler_keys is not None:
                        usage_query = usage_query.join(AdventurerS1BuildingInstance, AdventurerS1BuildingInstance.id == AdventurerS1BuildingUsageLog.building_instance_id).join(AdventurerS1BuildingTemplate, AdventurerS1BuildingTemplate.id == AdventurerS1BuildingInstance.template_id).filter(AdventurerS1BuildingTemplate.handler_key.in_(handler_keys))
                usage_logs = _recent(usage_query, AdventurerS1BuildingUsageLog)
            if building_filter_active:
                if usage_logs:
                    related_ids = {str(log.building_instance_id) for log in usage_logs}
                    usage_start = min((log.created_at for log in usage_logs))
                    usage_end = max((log.created_at for log in usage_logs))
                    async_window = max(1, int(current_app.config.get('ADVENTURER_S1_BUILDING_ASYNC_COST_MERGE_SECONDS', AdventurerS1Service._BUILDING_ASYNC_COST_MERGE_SECONDS)))
                    financial_source_types = ('building_usage_complete', *AdventurerS1Service._BUILDING_USAGE_COST_SOURCE_TYPES, *AdventurerS1Service._BUILDING_USAGE_REFUND_SOURCE_TYPES)
                    financial_filters = (AdventurerS1CurrencyLog.adventurer_id.in_(adv_ids), AdventurerS1CurrencyLog.related_type == 'building_instance', AdventurerS1CurrencyLog.related_id.in_(related_ids), AdventurerS1CurrencyLog.source_type.in_(financial_source_types), AdventurerS1CurrencyLog.created_at >= usage_start - timedelta(seconds=async_window), AdventurerS1CurrencyLog.created_at <= usage_end + timedelta(seconds=AdventurerS1Service._BUILDING_REWARD_MERGE_SECONDS))
                    currency_logs = AdventurerS1CurrencyLog.query.filter(*financial_filters).order_by(AdventurerS1CurrencyLog.created_at.desc(), AdventurerS1CurrencyLog.id.desc()).all()
                    material_logs = AdventurerS1MaterialLog.query.filter(AdventurerS1MaterialLog.adventurer_id.in_(adv_ids), AdventurerS1MaterialLog.related_type == 'building_instance', AdventurerS1MaterialLog.related_id.in_(related_ids), AdventurerS1MaterialLog.source_type.in_(financial_source_types), AdventurerS1MaterialLog.created_at >= usage_start - timedelta(seconds=async_window), AdventurerS1MaterialLog.created_at <= usage_end + timedelta(seconds=AdventurerS1Service._BUILDING_REWARD_MERGE_SECONDS)).order_by(AdventurerS1MaterialLog.created_at.desc(), AdventurerS1MaterialLog.id.desc()).all()
            if include_all_sources or activity_filter == 'currency':
                currency_query = AdventurerS1CurrencyLog.query
                if activity_filter == 'currency':
                    currency_query = currency_query.filter(AdventurerS1CurrencyLog.currency_code != 'contribution_total')
                currency_logs = _recent(currency_query, AdventurerS1CurrencyLog)
            if include_all_sources or activity_filter == 'materials':
                material_logs = _recent(AdventurerS1MaterialLog.query, AdventurerS1MaterialLog)
            if include_all_sources or activity_filter == 'trait_recovery':
                trait_logs = AdventurerS1Trait.query.options(joinedload(AdventurerS1Trait.adventurer)).join(AdventurerS1, AdventurerS1.id == AdventurerS1Trait.adventurer_id).filter(AdventurerS1Trait.adventurer_id.in_(adv_ids), AdventurerS1Trait.is_archived == True, AdventurerS1Trait.archived_reason == '自然休息恢复', AdventurerS1Trait.archived_at.isnot(None))
                if before_dt:
                    trait_logs = trait_logs.filter(AdventurerS1Trait.archived_at < before_dt)
                trait_logs = trait_logs.order_by(AdventurerS1Trait.archived_at.desc(), AdventurerS1Trait.id.desc()).limit(limit).all()
            if include_all_sources or activity_filter == 'items':
                item_logs = _recent(AdventurerS1ItemLog.query.filter(AdventurerS1ItemLog.hidden_from_player.is_(False)), AdventurerS1ItemLog)
        except Exception as e:
            return (None, player_operation_failure(e, '查询动态失败'))
        currency_types = dict(current_app.config.get('ADVENTURER_S1_CURRENCY_TYPES', {}))
        currency_types.setdefault('low_consumption_gold', {'name': '低消费临时金', 'icon': '🪙'})
        desc_item_names: set[str] = set()
        desc_item_candidates_by_log_id: dict[int, list[str]] = {}
        for log in [] if building_filter_active else currency_logs:
            if not log.description:
                continue
            candidates: list[str] = []
            for pattern in ('「([^」]{1,100})」', '（([^（）]{1,100})）'):
                for match in re.findall(pattern, log.description):
                    item_name = str(match or '').strip()
                    if item_name and item_name not in candidates:
                        candidates.append(item_name)
                        desc_item_names.add(item_name)
            if candidates:
                desc_item_candidates_by_log_id[log.id] = candidates
        desc_catalog_rows = AdventurerS1Item.query.with_entities(AdventurerS1Item.name, AdventurerS1Item.rarity, AdventurerS1Item.description, AdventurerS1Item.use_condition).filter(AdventurerS1Item.name.in_(desc_item_names)).all() if desc_item_names else []
        desc_rarity_by_name = {row.name: row.rarity for row in desc_catalog_rows}
        clue_names = set(catalog_clue_meta_by_item_names(desc_item_names))
        desc_item_name_by_log_id = {log_id: matched for log_id, candidates in desc_item_candidates_by_log_id.items() if (matched := next((candidate for candidate in candidates if candidate in desc_rarity_by_name or candidate in clue_names), None)) is not None}
        market_listing_ids: set[int] = set()
        for log in currency_logs:
            if log.source_type not in ('market_buy', 'market_sell'):
                continue
            if log.related_type != 'market_listing' or not log.related_id:
                continue
            try:
                market_listing_ids.add(int(log.related_id))
            except (TypeError, ValueError):
                continue
        market_listing_rarity_by_id: dict[int, str] = {}
        if market_listing_ids:
            from plugins.adventurer_s1.models import AdventurerS1MarketListing
            market_listing_rarity_by_id = {int(row.id): row.rarity for row in AdventurerS1MarketListing.query.with_entities(AdventurerS1MarketListing.id, AdventurerS1MarketListing.rarity).filter(AdventurerS1MarketListing.id.in_(market_listing_ids)).all()}
        reward_index = AdventurerS1Service._index_building_usage_rewards(currency_logs, material_logs, currency_types)
        consumed_rewards: set = set()
        entries: List[dict] = []

        def _append_currency_log(log):
            meta = currency_types.get(log.currency_code, {})
            description = log.description or ''
            if log.source_type in ('low_consumption_accrual', 'low_consumption_split'):
                description = '负债期间，合格净收益按比例划入低消费临时金'
            entry = {'kind': 'currency', 'id': log.id, 'created_at': log.created_at, 'code': log.currency_code, 'display_name': meta.get('name', log.currency_code), 'icon': meta.get('icon', '💰'), 'change_amount': round(float(log.change_amount or 0), 2), 'balance_after': round(float(log.balance_after or 0), 2), 'description': description, 'source_type': log.source_type}
            desc_item_name = desc_item_name_by_log_id.get(log.id)
            if desc_item_name:
                entry['item_name'] = desc_item_name
                listing_rarity = None
                if log.source_type in ('market_buy', 'market_sell') and log.related_type == 'market_listing':
                    try:
                        listing_rarity = market_listing_rarity_by_id.get(int(log.related_id))
                    except (TypeError, ValueError):
                        listing_rarity = None
                entry['item_rarity'] = listing_rarity or desc_rarity_by_name.get(desc_item_name, 'common')
            entries.append(entry)

        def _append_material_log(log):
            meta = MATERIAL_META.get(log.name, {})
            entries.append({'kind': 'material', 'id': log.id, 'created_at': log.created_at, 'code': log.name, 'display_name': meta.get('name', log.name), 'icon': meta.get('icon', '📦'), 'change_amount': int(log.change_amount or 0), 'balance_after': int(log.balance_after or 0), 'description': log.description or '', 'source_type': log.source_type})
        cost_source_types = AdventurerS1Service._BUILDING_USAGE_COST_SOURCE_TYPES
        refund_source_types = AdventurerS1Service._BUILDING_USAGE_REFUND_SOURCE_TYPES
        craft_item_sources = AdventurerS1Service._CRAFT_ACTIVITY_ITEM_SOURCES
        async_craft_types = AdventurerS1Service._ASYNC_CRAFT_RESULT_TYPES
        _help_by_building: dict[int, dict] = {}
        for log in currency_logs:
            if log.currency_code == 'contribution_total':
                continue
            if log.related_type == 'building_instance' and (log.source_type == 'building_usage_complete' or log.source_type in cost_source_types or log.source_type in refund_source_types):
                continue
            if activity_filter != 'currency' and log.source_type in _BUILDING_HELP_CONTRIBUTION_SOURCES and (log.related_type == 'building_instance') and log.related_id:
                bid = log.related_id
                cur = _help_by_building.get(bid)
                amt = float(log.change_amount or 0)
                if not cur or (log.created_at or datetime.min) > cur['latest_at']:
                    _help_by_building[bid] = {'amount': cur['amount'] + amt if cur else amt, 'latest_at': log.created_at or datetime.min, 'latest_id': log.id, 'count': cur['count'] + 1 if cur else 1, 'balance_after': float(log.balance_after or 0)}
                elif cur:
                    cur['amount'] += amt
                    cur['count'] += 1
            else:
                _append_currency_log(log)
        for bid, info in _help_by_building.items():
            meta = currency_types.get('contribution', {})
            entries.append({'kind': 'currency', 'id': info['latest_id'], 'created_at': info['latest_at'], 'code': 'contribution', 'display_name': meta.get('name', '贡献度'), 'icon': meta.get('icon', '⭐'), 'change_amount': round(info['amount'], 2), 'balance_after': round(info['balance_after'], 2), 'description': f"帮工贡献（{info['count']}次）" if info['count'] > 1 else '帮工贡献', 'source_type': _CONTRIBUTION_SRC_HELP})
        for log in material_logs:
            if log.related_type == 'building_instance' and (log.source_type == 'building_usage_complete' or log.source_type in cost_source_types):
                continue
            _append_material_log(log)
        for trait in trait_logs:
            adv_name = trait.adventurer.name if trait.adventurer else '未知冒险者'
            entries.append({'kind': 'trait_recovery', 'id': trait.id, 'created_at': trait.archived_at, 'adventurer_id': trait.adventurer_id, 'adventurer_name': adv_name, 'trait_name': trait.name, 'description': build_trait_recovery_activity_description(adv_name, trait.name), 'source_type': 'natural_rest_recovery'})
        from plugins.adventurer_s1.services.adventurer_s1_building_handler import get_handler
        from plugins.adventurer_s1.services.s1_building_undo_service import S1BuildingUndoService
        undo_now = _portable_utc_now()
        latest_undo_ids = S1BuildingUndoService.latest_candidate_ids(adv_ids, undo_now) if usage_logs else {}
        from plugins.adventurer_s1.services.s1_ai_feedback_service import S1AiFeedbackService
        ai_feedback_by_usage_log = S1AiFeedbackService.get_building_targets(user_id, usage_logs)
        from plugins.adventurer_s1.services.s1_player_projection_service import project_building_result
        for log in usage_logs:
            inst = log.building_instance
            tpl = inst.template if inst else None
            result = project_building_result(log.result)
            entry = {'kind': 'building', 'id': log.id, 'created_at': log.created_at, 'building_name': (inst.custom_name if inst else None) or (tpl.name if tpl else '未知建筑'), 'building_icon': (tpl.icon if tpl else None) or '🏠', 'result': result}
            if log.id in ai_feedback_by_usage_log:
                entry['ai_feedback'] = ai_feedback_by_usage_log[log.id]
            entry.update(S1BuildingUndoService.public_meta(log, latest_undo_ids, undo_now))
            async_craft = bool(result and result.get('type') in async_craft_types)
            reward, cost, refund = AdventurerS1Service._pick_building_reward(reward_index, log.adventurer_id, log.building_instance_id, log.created_at, consumed_rewards, async_craft=async_craft)
            if reward:
                entry['reward'] = reward
            if cost:
                entry['cost'] = cost
            if refund:
                entry['refund'] = refund
            if result is None and tpl is not None:
                handler = get_handler(tpl.handler_key)
                if handler is not None and handler.is_async():
                    entry['legacy_no_result'] = True
            entries.append(entry)
        all_building_source_types = ('building_usage_complete', *refund_source_types, *cost_source_types)
        if not building_filter_active:
            for log in currency_logs:
                if log.source_type not in all_building_source_types or log.related_type != 'building_instance':
                    continue
                if ('currency', log.id) in consumed_rewards:
                    continue
                _append_currency_log(log)
            for log in material_logs:
                if log.source_type not in all_building_source_types or log.related_type != 'building_instance':
                    continue
                if ('material', log.id) in consumed_rewards:
                    continue
                _append_material_log(log)
        for log in item_logs:
            if activity_filter != 'items':
                if log.source_type == 'lucky_wheel':
                    continue
                if log.source_type in craft_item_sources:
                    continue
                if log.source_type == 'building' and log.description:
                    desc = log.description
                    if '铁匠铺' in desc or '炼金工房' in desc:
                        continue
            entry = {'kind': 'item', 'id': log.id, 'created_at': log.created_at, 'item_name': log.item_name, 'item_type': log.item_type, 'item_rarity': log.item_rarity, 'change_type': log.change_type, 'quantity': int(log.quantity or 1), 'source_type': log.source_type, 'description': log.description or ''}
            if log.source_type in AdventurerS1Service._APPEAL_ITEM_GRANT_ACTIVITY_SOURCES and log.change_type in {'gain', 'overflow'} and (log.related_type == 'expedition_log'):
                try:
                    expedition_log_id = int(log.related_id or 0)
                except (TypeError, ValueError):
                    expedition_log_id = 0
                if expedition_log_id > 0:
                    entry['expedition_log_id'] = expedition_log_id
            sanitize_player_item_log_entry(entry)
            entries.append(entry)
        sanitize_player_currency_log_entries(entries, clue_names=clue_names)
        entries.sort(key=lambda e: (e['created_at'] or datetime.min, e['id']), reverse=True)
        if building_filter_active:
            has_more = len(usage_logs) == limit
        elif activity_filter == 'currency':
            has_more = len(currency_logs) == limit
        elif activity_filter == 'materials':
            has_more = len(material_logs) == limit
        elif activity_filter == 'items':
            has_more = len(item_logs) == limit
        elif activity_filter == 'trait_recovery':
            has_more = len(trait_logs) == limit
        else:
            has_more = len(entries) > limit or len(currency_logs) == limit or len(material_logs) == limit or (len(usage_logs) == limit) or (len(trait_logs) == limit) or (len(item_logs) == limit)
        page = entries[:limit]
        next_before = to_utc_iso(page[-1]['created_at']) if page and has_more else None
        for e in page:
            e['created_at'] = to_utc_iso(e['created_at'])
        payload = {'entries': page, 'has_more': has_more, 'next_before': next_before}
        if not before_dt:
            _my_activity_cache.set(cache_key, payload)
        return (payload, None)
