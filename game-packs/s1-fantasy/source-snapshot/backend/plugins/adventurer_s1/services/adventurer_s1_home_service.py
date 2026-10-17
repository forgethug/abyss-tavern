"""深渊酒馆 S1 首页初始化数据聚合。"""

import logging
import threading
from typing import Optional, Tuple

from flask import current_app
from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
)
from plugins.adventurer_s1.services.adventurer_s1_material_service import S1MaterialService
from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import AdventurerS1TavernSeatService
from plugins.adventurer_s1.services.adventurer_s1_tavern_service import AdventurerS1TavernService
from plugins.adventurer_s1.services.s1_realm_service import (
    S1RealmAssignmentError,
    S1RealmService,
)
from plugins.adventurer_s1.services.s1_secret_auction_service import S1SecretAuctionService
from utils.cache_manager import CacheManager

logger = logging.getLogger(__name__)

_HOME_BADGES_CACHE_TTL_SECONDS = 60
_home_badges_cache = CacheManager(
    ttl_seconds=_HOME_BADGES_CACHE_TTL_SECONDS, max_size=1000,
)
_home_task_badges_cache = CacheManager(
    ttl_seconds=_HOME_BADGES_CACHE_TTL_SECONDS, max_size=1000,
)
_home_task_badges_build_locks = tuple(threading.Lock() for _ in range(64))
_S1_INIT_CACHE_TTL_SECONDS = 60
_s1_init_cache = CacheManager(
    ttl_seconds=_S1_INIT_CACHE_TTL_SECONDS, max_size=500,
)
_S1_INIT_SHARED_CACHE_TTL_SECONDS = 90
_s1_init_shared_cache = CacheManager(
    ttl_seconds=_S1_INIT_SHARED_CACHE_TTL_SECONDS, max_size=8,
)


def invalidate_home_badges_cache(user_id: Optional[int] = None) -> None:
    """首页角标缓存失效；user_id 省略时清空全表。"""
    from plugins.adventurer_s1.services.adventurer_s1_service import invalidate_my_activity_cache

    if user_id is None:
        _home_badges_cache.clear()
        _home_task_badges_cache.clear()
        invalidate_s1_init_cache()
        invalidate_my_activity_cache()
    else:
        _home_badges_cache.delete(user_id)
        _home_task_badges_cache.delete(user_id)
        invalidate_s1_init_cache(user_id)
        invalidate_my_activity_cache(user_id)


def invalidate_home_auction_badges_cache() -> None:
    """仅失效包含拍卖角标的首页缓存，不影响任务角标与个人动态。"""
    _home_badges_cache.clear()


def invalidate_s1_init_cache(user_id: Optional[int] = None) -> None:
    """S1 首页 init 聚合缓存失效。"""
    if user_id is None:
        _s1_init_cache.clear()
        _s1_init_shared_cache.clear()
    else:
        for is_admin in (False, True):
            # 兼容进程滚动升级期间尚存的旧二元 key。
            _s1_init_cache.delete((user_id, is_admin))
            _s1_init_cache.delete((user_id, is_admin, False))
            _s1_init_cache.delete((user_id, is_admin, True))


class AdventurerS1HomeService:
    """为首页提供一次请求可消费的最小数据集合。"""

    def _load_init_shared(is_admin: bool) -> Tuple[dict, Optional[str]]:
        """全服共享的 init 块（酒单 / 酒馆升级 / 拍卖角标），跨用户复用。"""
        cache_key = int(is_admin)
        cached = _s1_init_shared_cache.get(cache_key)
        if cached is not None:
            return cached, None

        drink_menu, drink_err = AdventurerS1TavernSeatService.get_drinks_menu()
        if drink_err:
            return {}, drink_err
        food_menu, food_err = AdventurerS1TavernSeatService.get_foods_menu()
        if food_err:
            return {}, food_err
        tavern_upgrade, err = AdventurerS1TavernService.get_tavern_upgrade_progress()
        if err:
            return {}, err

        auction_summary, auction_err = S1SecretAuctionService.open_lot_summary()
        if auction_err:
            return {}, auction_err
        auction_open_count = 0
        if auction_summary and auction_summary.get('available'):
            auction_open_count = int(auction_summary.get('open_count', 0) or 0)

        payload = {
            'drink_menu': drink_menu or [],
            'food_menu': food_menu or [],
            'tavern_upgrade': tavern_upgrade,
            'auction_open_count': auction_open_count,
        }
        _s1_init_shared_cache.set(cache_key, payload)
        return payload, None

    @staticmethod
    def _primary_playable_adventurer_id(user_id: int) -> Optional[int]:
        """玩家首个可玩冒险者 ID（1:1 绑定场景下即唯一冒险者）。"""
        row = (
            db_manager.query(AdventurerS1.id)
            .filter(
                AdventurerS1.owner_id == user_id,
                AdventurerS1.is_player_created.is_(True),
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .order_by(AdventurerS1.created_at.desc())
            .first()
        )
        return row[0] if row else None

    @staticmethod
    def _task_summary_from_adv_id(adv_id: int) -> tuple[int, list[str], Optional[str]]:
        if AdventurerS1TaskService.has_active_accepted_task(adv_id):
            return 0, [], None
        return AdventurerS1TaskService.summarize_acceptable_tasks(adv_id)

    @staticmethod
    def _task_summary(adventurers: list) -> tuple[int, list[str], Optional[str]]:
        adv = next(
            (item for item in adventurers if item.get('status') not in ('dead', 'archived')),
            None,
        )
        if not adv:
            return 0, [], None
        return AdventurerS1HomeService._task_summary_from_adv_id(adv['id'])

    @staticmethod
    def get_init_data(user_id: int, is_admin: bool) -> Tuple[Optional[dict], Optional[str]]:
        """聚合首页数据；任一数据源失败时明确返回错误，避免伪装成空数据。"""
        squad_enabled = bool(
            current_app.config.get('ADVENTURER_S1_SQUADS_ENABLED', False)
        )
        cache_key = (user_id, bool(is_admin), squad_enabled)
        cached = _s1_init_cache.get(cache_key)
        if cached is not None:
            from db_repository.s1_party_preference_repository import S1PartyPreferenceRepository

            cards = [card for card in (cached.get('adventurers') or []) if 'party_preference' in card]
            preferences = S1PartyPreferenceRepository.get_owned_preferences(
                [card['id'] for card in cards], user_id, S1RealmService.current_runtime_realm(),
            )
            # 批量操作可在另一 worker 完成；只刷新现有私人字段，不重读完整首页。
            for card in cards:
                if card['id'] in preferences:
                    card['party_preference'] = preferences[card['id']]
            if squad_enabled:
                # init 缓存是进程内缓存；徽章在命中时重新白名单投影，避免另一
                # worker 完成小队变更后继续返回最长 60 秒的旧名片。
                from plugins.adventurer_s1.services.s1_squad_service import (
                    S1SquadService,
                )

                S1SquadService.attach_badges(cached.get('adventurers') or [])
            return cached, None

        shared, shared_err = AdventurerS1HomeService._load_init_shared(is_admin)
        if shared_err:
            return None, f'首页数据加载失败（shared）：{shared_err}'

        try:
            from plugins.adventurer_s1.services.s1_realm_gateway_service import (
                S1RealmGatewayService,
            )

            realm_assignment = S1RealmGatewayService.get_request_assignment_preview(user_id)
            if realm_assignment is None:
                realm_assignment = S1RealmService.get_assignment_preview(user_id)
        except S1RealmAssignmentError as exc:
            return None, f'首页数据加载失败（realm_assignment）：{exc}'

        calls = (
            ('notice', lambda: AdventurerS1TavernService.get_notice(user_id=user_id)),
            ('adventurers', lambda: AdventurerS1Service.get_adventurer_list(user_id=user_id)),
            ('buildings', lambda: AdventurerS1Service.get_buildings(user_id=user_id, is_admin=is_admin)),
            ('tavern_seats', lambda: AdventurerS1TavernSeatService.list_seats(user_id=user_id)),
            ('activity', lambda: AdventurerS1Service.get_my_activity(
                user_id=user_id,
                limit=10,
                settle_due_trait_recovery=False,
            )),
            ('materials', lambda: S1MaterialService.get_owner_totals(user_id)),
        )
        loaded = {}
        for section, loader in calls:
            data, err = loader()
            if err:
                logger.warning('S1 首页初始化失败 section=%s user_id=%s error=%s', section, user_id, err)
                return None, f'首页数据加载失败（{section}）：{err}'
            loaded[section] = data

        adventurers = loaded['adventurers'] or []
        task_payload = _home_task_badges_cache.get(user_id)
        if task_payload is None:
            task_count, task_ids, task_err = AdventurerS1HomeService._task_summary(adventurers)
            if task_err:
                return None, f'首页数据加载失败（task_summary）：{task_err}'
            task_payload = {
                'task_acceptable_count': task_count,
                'task_acceptable_ids': task_ids,
            }
            # init 已经完成同一份角标计算，供随后 Socket/轮询直接复用。
            _home_task_badges_cache.set(user_id, task_payload)

        buildings = loaded['buildings'] or {}
        seats = loaded['tavern_seats'] or {}
        from plugins.adventurer_s1.services.s1_cross_realm_mirror_service import (
            is_cross_realm_mirror_control_visible,
        )
        payload = {
            'notice': loaded['notice'],
            'realm_assignment': realm_assignment,
            'adventurers': adventurers,
            'buildings': buildings.get('buildings', []),
            'tavern_seats': seats.get('seats', []),
            'drink_menu': shared.get('drink_menu') or [],
            'tavern_upgrade': shared.get('tavern_upgrade'),
            'activity': loaded['activity'] or [],
            'materials': loaded['materials'] or {},
            'task_acceptable_count': task_payload['task_acceptable_count'],
            'task_acceptable_ids': task_payload['task_acceptable_ids'],
            'auction_open_count': int(shared.get('auction_open_count') or 0),
            'squad_feature_enabled': squad_enabled,
            'cross_realm_mirror': {
                'visible': is_cross_realm_mirror_control_visible(user_id),
            },
            # 历史客户端字段保留为恒定值；结算审核功能已下线。
            'pending_audit_count': 0,
        }
        _s1_init_cache.set(cache_key, payload)
        return payload, None

    @staticmethod
    def get_task_badge_data(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """仅任务栏角标（不含拍卖行），供 task_board_changed 等场景。"""
        cached = _home_task_badges_cache.get(user_id)
        if cached is not None:
            return cached, None

        # 同一 worker 内多个 Socket/轮询请求可能同时冷启动；双检锁避免重复跑整套任务统计。
        build_lock = _home_task_badges_build_locks[user_id % len(_home_task_badges_build_locks)]
        with build_lock:
            cached = _home_task_badges_cache.get(user_id)
            if cached is not None:
                return cached, None

            adv_id = AdventurerS1HomeService._primary_playable_adventurer_id(user_id)
            if adv_id:
                task_count, task_ids, task_err = AdventurerS1HomeService._task_summary_from_adv_id(adv_id)
            else:
                task_count, task_ids, task_err = 0, [], None
            if task_err:
                return None, task_err

            payload = {
                'task_acceptable_count': task_count,
                'task_acceptable_ids': task_ids,
            }
            _home_task_badges_cache.set(user_id, payload)
            return payload, None

    @staticmethod
    def get_badge_data(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """轻量角标：任务栏可接数 + 拍卖行进行中拍品数（不含完整任务板/建筑列表）。"""
        cached = _home_badges_cache.get(user_id)
        if cached is not None:
            return cached, None

        task_payload, task_err = AdventurerS1HomeService.get_task_badge_data(user_id)
        if task_err:
            return None, task_err

        auction_summary, auction_err = S1SecretAuctionService.open_lot_summary()
        if auction_err:
            return None, auction_err
        auction_open_count = 0
        if auction_summary and auction_summary.get('available'):
            auction_open_count = int(auction_summary.get('open_count', 0) or 0)

        payload = {
            **task_payload,
            'auction_open_count': auction_open_count,
        }
        _home_badges_cache.set(user_id, payload)
        return payload, None
