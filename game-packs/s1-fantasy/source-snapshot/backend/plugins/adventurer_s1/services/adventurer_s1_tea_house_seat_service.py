"""深渊酒馆 S1 深渊茶舍座位服务。

除超 6 小时上限外，精力回满也会被 scheduler 自动清座（列表仅做只读投影）。
"""
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_database_result, player_operation_failure,
)
import random
from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1TeaHouseSeat,
)


from utils.cache_manager import CacheManager


# 茶桌最低开放等级（未列出的清风/明月两桌为 Lv1）。
_TEA_HOUSE_TABLE_MIN_LEVEL = {
    'tea_c': 2,
    'tea_d': 2,
    'tea_e': 3,
    'tea_f': 3,
}


def _table_min_level(table_key: str) -> int:
    return int(_TEA_HOUSE_TABLE_MIN_LEVEL.get(table_key, 1))


_SEAT_CLEAR_FIELDS = {
    'adventurer_id': None,
    'seated_at': None,
    'stamina_settled_at': None,
    'entry_fee_paid': False,
    'tea_key': None,
    'tea_text': None,
    'ordered_at': None,
}


S1_TEA_HOUSE_TEAS = [
    {
        'key': 'mist_green',
        'name': '雾岭青芽',
        'icon': '🍵',
        'price': 0,
        'desc': '清浅的绿茶，入口像山雾刚散，适合把急躁压下去。',
        'texts': [
            '捧着茶杯轻轻吹气，像怕惊散杯口那层薄雾。',
            '喝了一小口，肩膀慢慢松下来，眼神也清明了些。',
            '把茶杯贴近鼻尖闻了闻，低声说有新雨后的草木味。',
        ],
    },
    {
        'key': 'ember_black',
        'name': '炭炉红茶',
        'icon': '🫖',
        'price': 0,
        'desc': '用小炭炉温着的红茶，厚实暖胃，适合雨夜归来。',
        'texts': [
            '双手拢着热杯，指节被暖意一点点烘开。',
            '喝完半杯才长长呼出一口气，像把寒意也吐了出去。',
            '盯着茶汤的红褐色看了许久，终于露出一点困意。',
        ],
    },
    {
        'key': 'snow_jasmine',
        'name': '雪瓣茉莉',
        'icon': '🌼',
        'price': 0,
        'desc': '茉莉香很轻，像雪落在花瓣上，安静但不寡淡。',
        'texts': [
            '闻到花香后眨了眨眼，像突然想起很远的春天。',
            '小口抿着茶，连说话声音都放轻了。',
            '把杯盖掀开一线，任花香慢慢散在桌边。',
        ],
    },
    {
        'key': 'ginger_honey',
        'name': '姜蜜暖茶',
        'icon': '🍯',
        'price': 0,
        'desc': '姜味不冲，蜂蜜收尾，专治一身冷汗和硬撑。',
        'texts': [
            '第一口被姜味呛得皱眉，第二口却喝得很认真。',
            '摸了摸胃口的位置，像终于确认自己还活得暖和。',
            '把杯底的蜜味喝干净，嘴角不自觉翘了一下。',
        ],
    },
    {
        'key': 'lotus_oolong',
        'name': '莲心乌龙',
        'icon': '🪷',
        'price': 0,
        'desc': '乌龙茶里压着一点莲心苦味，回甘来得很慢。',
        'texts': [
            '苦得沉默了一会儿，回甘上来后才点点头。',
            '把茶杯转在掌心，像在等某个迟到的答案。',
            '喝到最后一口时才评价：「这茶后劲不错。」',
        ],
    },
    {
        'key': 'date_barley',
        'name': '红枣麦茶',
        'icon': '🌾',
        'price': 0,
        'desc': '不含茶叶的香麦热饮，红枣味朴素，适合真正想休息的人。',
        'texts': [
            '喝得很快，像终于碰到不用逞强的东西。',
            '捧着杯子发了会儿呆，神情比刚入座时柔和许多。',
            '认真咬着杯里的红枣片，像在补回今天丢掉的力气。',
        ],
    },
    {
        'key': 'osmanthus_puer',
        'name': '桂花普洱',
        'icon': '🌙',
        'price': 0,
        'desc': '陈香里浮着一点桂花甜，像旧书页里夹着秋天。',
        'texts': [
            '闻了许久才入口，像怕错过第一缕桂香。',
            '靠在椅背上慢慢喝，眼神沉进很远的回忆。',
            '茶香落下后轻轻笑了一声，没解释想到了什么。',
        ],
    },
    {
        'key': 'mint_cold_brew',
        'name': '薄荷冷泡',
        'icon': '🌿',
        'price': 0,
        'desc': '冷泡茶带薄荷清凉，适合刚从热闹里逃出来。',
        'texts': [
            '冰凉茶水一下肚，整个人都清醒了半寸。',
            '把杯壁上的水珠抹开，在桌上划出一道浅痕。',
            '轻轻吸了口薄荷香，像把烦躁也一并吸走了。',
        ],
    },
    {
        'key': 'pine_smoke',
        'name': '松烟岩茶',
        'icon': '🌲',
        'price': 0,
        'desc': '带一点松烟和石壁气味，喝起来像在山洞口避雨。',
        'texts': [
            '第一口下去皱了眉，随后又忍不住续了一口。',
            '说这茶有篝火味，听起来不像抱怨。',
            '盯着茶汤里沉下去的叶片，像在看一小堆余烬。',
        ],
    },
    {
        'key': 'white_peach',
        'name': '白桃清茶',
        'icon': '🍑',
        'price': 0,
        'desc': '淡淡白桃香，甜味很克制，适合心情不好时不承认。',
        'texts': [
            '喝了一口后故作平静，耳根却悄悄放松了。',
            '把茶杯抱得很稳，像护着一点不愿说出口的甜。',
            '评价「还行」，却很快把整杯喝完了。',
        ],
    },
    {
        'key': 'wolfberry_chrysanthemum',
        'name': '枸杞菊花',
        'icon': '🌻',
        'price': 0,
        'desc': '清火明目的老派茶饮，常被伤员和熬夜者抢光。',
        'texts': [
            '认真数了数杯里的枸杞，像在确认今天还剩多少运气。',
            '喝完后揉了揉眼睛，假装自己只是有点累。',
            '把菊花推到杯边，低声说这茶像医嘱，但好喝。',
        ],
    },
    {
        'key': 'abyss_clear',
        'name': '深渊清供',
        'icon': '💧',
        'price': 0,
        'desc': '茶舍老板私藏的清茶，味道极淡，余韵却压得住深渊的噪声。',
        'texts': [
            '喝下去后安静了很久，像终于听见自己的呼吸。',
            '杯中茶色几乎透明，却让眼底的疲惫淡了一层。',
            '轻轻合上杯盖，没有评价，只是把背挺直了一点。',
        ],
    },
]

S1_TEA_HOUSE_TEA_MAP = {tea['key']: tea for tea in S1_TEA_HOUSE_TEAS}

_TEA_HOUSE_SEATS_SHARED_CACHE_TTL_SECONDS = 60
_tea_house_seats_shared_cache = CacheManager(
    ttl_seconds=_TEA_HOUSE_SEATS_SHARED_CACHE_TTL_SECONDS, max_size=4,
)


def invalidate_tea_house_seats_cache() -> None:
    """茶舍座位共享读缓存失效（入座/离座/点茶/scheduler tick 后调用）。"""
    _tea_house_seats_shared_cache.clear()


def _entry_fee() -> float:
    return float(current_app.config.get('ADVENTURER_S1_TEA_HOUSE_ENTRY_FEE', 30))


def _max_stay_hours() -> int:
    return int(current_app.config.get('ADVENTURER_S1_TEA_HOUSE_MAX_STAY_HOURS', 6))


def _stamina_per_minute() -> int:
    return int(current_app.config.get('ADVENTURER_S1_TEA_HOUSE_STAMINA_PER_MINUTE', 2))


def _max_stamina() -> int:
    return int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))


class AdventurerS1TeaHouseSeatService:
    """深渊茶舍座位：入座 / 离座 / 点茶 / 全景列表。"""

    @staticmethod
    def get_teas_menu() -> Tuple[Optional[list], Optional[str]]:
        """茶单（不含文案池本体）。静态数据，前端可一次拉取后缓存。"""
        menu = [
            {
                'key': tea['key'],
                'name': tea['name'],
                'icon': tea['icon'],
                'price': tea['price'],
                'desc': tea['desc'],
            }
            for tea in S1_TEA_HOUSE_TEAS
        ]
        return menu, None

    @staticmethod
    def _tea_brief(tea_key: str, tea_text: Optional[str]) -> Optional[dict]:
        """座位上展示的茶信息：茶单元数据 + 该次点茶抽到的文案。"""
        meta = S1_TEA_HOUSE_TEA_MAP.get(tea_key)
        if not meta:
            return None
        return {
            'key': meta['key'],
            'name': meta['name'],
            'icon': meta['icon'],
            'price': meta['price'],
            'text': tea_text or '',
        }

    @staticmethod
    def _tea_house_built_level() -> Tuple[Optional[int], Optional[str]]:
        """一次查询：茶舍是否已建成 + 当前 upgrade_level。未建成返回 (None, err)。"""
        inst = (
            AdventurerS1BuildingInstance.query
            .join(AdventurerS1BuildingTemplate)
            .with_entities(AdventurerS1BuildingInstance.upgrade_level)
            .filter(
                AdventurerS1BuildingTemplate.handler_key == 'tea_house',
                AdventurerS1BuildingTemplate.is_active.is_(True),
                AdventurerS1BuildingInstance.status == 'built',
            )
            .first()
        )
        if not inst:
            return None, '深渊茶舍尚未建成'
        return int(inst[0] or 1), None

    @staticmethod
    def _ensure_tea_house_built() -> Optional[str]:
        _, err = AdventurerS1TeaHouseSeatService._tea_house_built_level()
        return err

    @staticmethod
    def _tea_house_upgrade_level() -> int:
        level, err = AdventurerS1TeaHouseSeatService._tea_house_built_level()
        return level if err is None else 1

    @staticmethod
    def _merge_tea_house_is_mine(seats: list, user_id: int) -> list:
        merged = []
        for seat in seats:
            row = dict(seat)
            adv = row.get('adventurer')
            if adv:
                row['adventurer'] = {
                    'id': adv['id'],
                    'name': adv['name'],
                    'is_mine': adv.get('owner_id') == user_id,
                }
            merged.append(row)
        return merged

    @staticmethod
    def _build_shared_seats_payload(level: int) -> list:
        """只读构建可跨用户缓存的茶舍座位快照（含 owner_id 供 is_mine merge）。"""
        seats = AdventurerS1TeaHouseSeat.query.order_by(
            AdventurerS1TeaHouseSeat.sort_order.asc(),
        ).all()

        seated_ids = [s.adventurer_id for s in seats if s.adventurer_id]
        adv_map = {}
        if seated_ids:
            rows = AdventurerS1.query.with_entities(
                AdventurerS1.id, AdventurerS1.name, AdventurerS1.status,
                AdventurerS1.owner_id, AdventurerS1.building_instance_id,
                AdventurerS1.stamina,
            ).filter(AdventurerS1.id.in_(seated_ids)).all()
            adv_map = {r.id: r for r in rows}

        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        max_stay = timedelta(hours=_max_stay_hours())
        now = _portable_utc_now()
        stale_seat_ids = []

        for seat in seats:
            if not seat.adventurer_id:
                continue
            adv = adv_map.get(seat.adventurer_id)
            should_clear = False
            settle_until = now

            if adv is None or not seat.seated_at:
                should_clear = True
            elif adv.status not in AdventurerS1Service.FREE_STATUSES or adv.building_instance_id:
                should_clear = True
            elif level < _table_min_level(seat.table_key):
                should_clear = True
            elif seat.seated_at and now > seat.seated_at + max_stay:
                should_clear = True
                settle_until = seat.seated_at + max_stay
            elif adv is not None and AdventurerS1TeaHouseSeatService._would_stamina_be_full(
                seat, adv, now, settle_until=settle_until,
            ):
                should_clear = True

            if should_clear:
                stale_seat_ids.append(seat.id)

        active_seated_ids = [
            s.adventurer_id for s in seats
            if s.adventurer_id and s.id not in stale_seat_ids
        ]
        adv_name_map = {r.id: r.name for r in adv_map.values()}
        from plugins.adventurer_s1.services.adventurer_s1_tea_house_chatter_service import (
            AdventurerS1TeaHouseChatterService,
        )
        chatter_by_seat = AdventurerS1TeaHouseChatterService.generate_for_seats(
            seats, adv_name_map, stale_seat_ids,
        )

        result = []
        for seat in seats:
            adventurer = None
            tea = None
            if seat.adventurer_id and seat.id not in stale_seat_ids:
                adv = adv_map.get(seat.adventurer_id)
                if adv:
                    adventurer = {
                        'id': adv.id,
                        'name': adv.name,
                        'owner_id': adv.owner_id,
                    }
                    if seat.tea_key:
                        tea = AdventurerS1TeaHouseSeatService._tea_brief(
                            seat.tea_key, seat.tea_text,
                        )
            seat_locked = level < _table_min_level(seat.table_key)
            seat_data = seat.to_dict(adventurer=adventurer, tea=tea, locked=seat_locked)
            seat_data['chatter'] = chatter_by_seat.get(seat.id)
            result.append(seat_data)
        return result

    @staticmethod
    def tick_tea_house_stamina_job() -> None:
        """Scheduler：按冒险者加锁结算茶舍精力并回收陈旧座位。"""
        candidate_rows = AdventurerS1TeaHouseSeat.query.with_entities(
            AdventurerS1TeaHouseSeat.id,
            AdventurerS1TeaHouseSeat.adventurer_id,
        ).filter(
            AdventurerS1TeaHouseSeat.adventurer_id.isnot(None),
        ).order_by(AdventurerS1TeaHouseSeat.id.asc()).all()
        if not candidate_rows:
            return

        seat_ids_by_adv: dict[int, list[int]] = {}
        for row in candidate_rows:
            seat_ids_by_adv.setdefault(int(row.adventurer_id), []).append(int(row.id))

        now = _portable_utc_now()
        max_stay = timedelta(hours=_max_stay_hours())
        level, built_err = AdventurerS1TeaHouseSeatService._tea_house_built_level()
        if built_err:
            level = 0
        changed = False
        cleared_adv_ids: set[int] = set()

        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        from sqlalchemy.orm import load_only, noload

        try:
            # 固定 adv -> seat 锁序；被其它写路径占用的冒险者整组跳过，下轮重试。
            adv_ids = sorted(seat_ids_by_adv)
            existing_adv_ids = {
                int(row.id) for row in AdventurerS1.query.with_entities(
                    AdventurerS1.id,
                ).filter(AdventurerS1.id.in_(adv_ids)).all()
            }
            adv_rows = (
                AdventurerS1.query
                .options(
                    load_only(
                        AdventurerS1.id,
                        AdventurerS1.status,
                        AdventurerS1.building_instance_id,
                        AdventurerS1.stamina,
                        AdventurerS1.stamina_updated_at,
                    ),
                    noload('*'),
                )
                .filter(AdventurerS1.id.in_(adv_ids))
                .order_by(AdventurerS1.id.asc())
                .populate_existing()
                .with_for_update(skip_locked=True)
                .all()
            )
            adv_map = {adv.id: adv for adv in adv_rows}
            missing_adv_ids = set(adv_ids) - existing_adv_ids
            candidate_adv_by_seat = {
                seat_id: adv_id
                for adv_id, seat_ids in seat_ids_by_adv.items()
                if adv_id in adv_map or adv_id in missing_adv_ids
                for seat_id in seat_ids
            }
            candidate_ids = sorted(candidate_adv_by_seat)
            seats = (
                AdventurerS1TeaHouseSeat.query
                .filter(
                    AdventurerS1TeaHouseSeat.id.in_(candidate_ids),
                    AdventurerS1TeaHouseSeat.adventurer_id.in_(
                        [*adv_map, *missing_adv_ids]
                    ),
                )
                .order_by(AdventurerS1TeaHouseSeat.id.asc())
                .populate_existing()
                .with_for_update(skip_locked=True)
                .all()
            ) if candidate_ids else []
            active_adv_ids: set[int] = set()

            for seat in seats:
                expected_adv_id = candidate_adv_by_seat.get(seat.id)
                # 候选快照之后座位可能已被换人；未锁新冒险者时绝不触碰该行。
                if not expected_adv_id or seat.adventurer_id != expected_adv_id:
                    continue
                adv = adv_map.get(expected_adv_id)
                if adv is None:
                    for field, value in _SEAT_CLEAR_FIELDS.items():
                        setattr(seat, field, value)
                    changed = True
                    continue

                should_clear = (
                    not seat.seated_at
                    or level < _table_min_level(seat.table_key)
                    or adv.status not in AdventurerS1Service.FREE_STATUSES
                    or bool(adv.building_instance_id)
                    or adv.id in active_adv_ids
                )
                settle_until = now
                if seat.seated_at:
                    settle_until = min(now, seat.seated_at + max_stay)
                timed_out = bool(seat.seated_at and now > seat.seated_at + max_stay)

                if not should_clear:
                    active_adv_ids.add(adv.id)
                    if AdventurerS1TeaHouseSeatService._settle_stamina(
                        seat,
                        adv,
                        now,
                        settle_until=settle_until,
                    ):
                        changed = True
                    if timed_out or AdventurerS1TeaHouseSeatService._is_stamina_full(adv):
                        should_clear = True

                if should_clear:
                    for field, value in _SEAT_CLEAR_FIELDS.items():
                        setattr(seat, field, value)
                    cleared_adv_ids.add(adv.id)
                    changed = True

            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or '茶舍精力 tick 提交失败')
        except Exception:
            db_manager.rollback()
            if changed:
                invalidate_tea_house_seats_cache()
            raise

        if changed:
            invalidate_tea_house_seats_cache()
            if cleared_adv_ids:
                AdventurerS1TeaHouseSeatService._emit_tea_house_changed(
                    'tea_house_auto_leave',
                    ids=sorted(cleared_adv_ids),
                )

    @staticmethod
    def _is_stamina_full(adv: AdventurerS1) -> bool:
        return int(adv.stamina or 0) >= _max_stamina()

    @staticmethod
    def _would_stamina_be_full(
        seat: AdventurerS1TeaHouseSeat,
        adv: AdventurerS1,
        now: datetime,
        *,
        settle_until: Optional[datetime] = None,
    ) -> bool:
        """Read-only：按待结算时长推算茶舍恢复后精力是否已满。"""
        if not seat.seated_at:
            return AdventurerS1TeaHouseSeatService._is_stamina_full(adv)
        start = seat.stamina_settled_at or seat.seated_at
        end = settle_until or now
        if end <= start:
            return AdventurerS1TeaHouseSeatService._is_stamina_full(adv)
        elapsed_minutes = int((end - start).total_seconds() / 60)
        if elapsed_minutes <= 0:
            return AdventurerS1TeaHouseSeatService._is_stamina_full(adv)
        current_stamina = max(0, int(adv.stamina or 0))
        projected = min(_max_stamina(), current_stamina + elapsed_minutes * _stamina_per_minute())
        return projected >= _max_stamina()

    @staticmethod
    def _settle_stamina(
        seat: AdventurerS1TeaHouseSeat,
        adv: AdventurerS1,
        now: datetime,
        *,
        settle_until: Optional[datetime] = None,
    ) -> bool:
        """把茶舍恢复精力落账一次，返回是否修改了数据。

        `seated_at` 保留为原始入座时间用于 6 小时上限判定；
        `stamina_settled_at` 是增量结算基准，避免刷新列表重复加精力。
        """
        if not seat.seated_at:
            return False

        start = seat.stamina_settled_at or seat.seated_at
        end = settle_until or now
        if end <= start:
            return False

        elapsed_minutes = int((end - start).total_seconds() / 60)
        if elapsed_minutes <= 0:
            return False

        settled_through = start + timedelta(minutes=elapsed_minutes)
        current_stamina = max(0, int(adv.stamina or 0))
        recovery = elapsed_minutes * _stamina_per_minute()
        new_stamina = min(_max_stamina(), current_stamina + recovery)

        if new_stamina != current_stamina:
            adv.stamina = new_stamina
            adv.stamina_updated_at = settled_through

        # 只推进已经兑换成精力的完整分钟，保留 scheduler 抖动产生的余秒。
        seat.stamina_settled_at = settled_through
        return True

    @staticmethod
    def get_seated_adventurer_ids(adventurer_ids=None) -> set:
        """批量查询当前占茶舍座位的冒险者 ID。"""
        q = AdventurerS1TeaHouseSeat.query.with_entities(
            AdventurerS1TeaHouseSeat.adventurer_id,
        ).filter(AdventurerS1TeaHouseSeat.adventurer_id.isnot(None))
        if adventurer_ids is not None:
            ids = list(adventurer_ids)
            if not ids:
                return set()
            q = q.filter(AdventurerS1TeaHouseSeat.adventurer_id.in_(ids))
        return {row[0] for row in q.all()}

    @staticmethod
    def clear_seats_for_adventurer(adventurer_id: int) -> None:
        """清掉指定冒险者占用的所有茶舍座位，不做收益结算。"""
        AdventurerS1TeaHouseSeat.query.filter(
            AdventurerS1TeaHouseSeat.adventurer_id == adventurer_id,
        ).update(_SEAT_CLEAR_FIELDS, synchronize_session=False)

    @staticmethod
    def settle_and_clear_seats_for_adventurer(
        adv: AdventurerS1,
        now: Optional[datetime] = None,
        *,
        locked_seats: Optional[list[AdventurerS1TeaHouseSeat]] = None,
    ) -> bool:
        """冒险者离开休息态前调用：结算茶舍恢复并清座。"""
        now = now or _portable_utc_now()
        seats = list(locked_seats) if locked_seats is not None else (
            AdventurerS1TeaHouseSeat.query.filter(
                AdventurerS1TeaHouseSeat.adventurer_id == adv.id,
            ).order_by(
                AdventurerS1TeaHouseSeat.id.asc(),
            ).populate_existing().with_for_update().all()
        )
        was_seated = bool(seats)
        for seat in seats:
            settle_until = now
            if seat.seated_at:
                max_until = seat.seated_at + timedelta(hours=_max_stay_hours())
                settle_until = min(now, max_until)
            AdventurerS1TeaHouseSeatService._settle_stamina(
                seat, adv, now, settle_until=settle_until,
            )
            seat.adventurer_id = None
            seat.seated_at = None
            seat.stamina_settled_at = None
            seat.entry_fee_paid = False
            seat.tea_key = None
            seat.tea_text = None
            seat.ordered_at = None
        return was_seated

    @staticmethod
    def _emit_tea_house_changed(action: str, **payload) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
            socketio.emit('adventurer_changed', {
                'action': action,
                'reason': action,
                **payload,
            }, namespace='/')
        except Exception:
            pass

    @staticmethod
    def list_seats(user_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """座位全景：所有座位 + 在座冒险者 + 闲聊。

        读路径只投影陈旧座位为空位；精力落账与实际清座统一走 scheduler tick。
        """
        try:
            level, built_err = AdventurerS1TeaHouseSeatService._tea_house_built_level()
            if built_err:
                return None, built_err

            cache_key = f'lv{level}'
            shared = _tea_house_seats_shared_cache.get(cache_key)
            if shared is None:
                shared = AdventurerS1TeaHouseSeatService._build_shared_seats_payload(level)
                _tea_house_seats_shared_cache.set(cache_key, shared)

            from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
                S1CurrencyService,
            )
            my_adventurer_row = (
                AdventurerS1.query
                .with_entities(AdventurerS1.id)
                .filter(
                    AdventurerS1.owner_id == user_id,
                    AdventurerS1.is_player_created.is_(True),
                    AdventurerS1.status != 'dead',
                )
                .order_by(AdventurerS1.id.asc())
                .first()
            )
            my_adventurer_id = int(my_adventurer_row[0]) if my_adventurer_row else None
            payment_balances = None
            if my_adventurer_id:
                payment_balances, balance_err = (
                    S1CurrencyService.get_low_consumption_payment_balances(my_adventurer_id)
                )
                if balance_err:
                    return None, balance_err

            return {
                'seats': AdventurerS1TeaHouseSeatService._merge_tea_house_is_mine(shared, user_id),
                'stamina_per_minute': _stamina_per_minute(),
                'entry_fee': _entry_fee(),
                'low_consumption_gold': float(
                    (payment_balances or {}).get('low_consumption_balance') or 0
                ),
                'low_consumption_gold_cap': S1CurrencyService.low_consumption_cap(),
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def sit(user_id: int, seat_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """让自己的空闲冒险者入座；已坐在别处则自动换座。"""
        try:
            built_err = AdventurerS1TeaHouseSeatService._ensure_tea_house_built()
            if built_err:
                return None, built_err

            seat_row = AdventurerS1TeaHouseSeat.query.session.get(AdventurerS1TeaHouseSeat, seat_id)
            if not seat_row:
                return None, '座位不存在'
            required_level = _table_min_level(seat_row.table_key)
            if AdventurerS1TeaHouseSeatService._tea_house_upgrade_level() < required_level:
                return None, f'这桌需茶舍升至 Lv{required_level} 后开放'

            adv = AdventurerS1.query.filter(
                AdventurerS1.id == adventurer_id,
            ).populate_existing().with_for_update().first()
            if not adv:
                return None, '冒险者不存在'
            if adv.owner_id != user_id:
                return None, '只能安排自己的冒险者入座'
            ok, reason = adv.can_dispatch()
            if not ok:
                return None, reason
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
            if get_live_stamina(adv, max_stamina=_max_stamina()) >= _max_stamina():
                return None, '该冒险者精力已满，无需来茶舍休息'

            seated_now = _portable_utc_now()
            affected = AdventurerS1TeaHouseSeat.query.filter(
                AdventurerS1TeaHouseSeat.id == seat_id,
                AdventurerS1TeaHouseSeat.adventurer_id.is_(None),
            ).update({
                'adventurer_id': adventurer_id,
                'seated_at': seated_now,
                'stamina_settled_at': seated_now,
                'entry_fee_paid': False,
                'tea_key': None,
                'tea_text': None,
                'ordered_at': None,
            }, synchronize_session='fetch')
            if not affected:
                seat = AdventurerS1TeaHouseSeat.query.session.get(AdventurerS1TeaHouseSeat, seat_id)
                db_manager.rollback()
                if not seat:
                    return None, '座位不存在'
                return None, '这个座位已经有人了'

            AdventurerS1TeaHouseSeat.query.filter(
                AdventurerS1TeaHouseSeat.adventurer_id == adventurer_id,
                AdventurerS1TeaHouseSeat.id != seat_id,
            ).update(_SEAT_CLEAR_FIELDS, synchronize_session='fetch')

            from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                AdventurerS1StateTransitionCoordinator,
            )
            AdventurerS1StateTransitionCoordinator.switch_rest_source(
                adv,
                'tea_house',
                seated_now,
            )

            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            fee = _entry_fee()
            payment, err = S1CurrencyService.deduct_low_consumption(
                adv.id,
                fee,
                facility_key='tea_house',
                source_type='tea_house_entry',
                related_id=seat_id,
                related_type='tea_house_seat',
                description='茶舍入场费',
                auto_commit=False,
            )
            if err:
                db_manager.rollback()
                if '金额不足' in err:
                    return None, f'金币与低消费临时金不足，茶舍入场费需要 {fee:g} 金币'
                return None, err

            seat = AdventurerS1TeaHouseSeat.query.session.get(AdventurerS1TeaHouseSeat, seat_id)
            if seat:
                seat.entry_fee_paid = True

            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
            AdventurerS1StateTransitionCoordinator.invalidate_rest_source_cache()
            return {
                'seat_id': seat_id,
                'adventurer_id': adventurer_id,
                'entry_fee': fee,
                'gold_balance': payment['gold_balance'],
                'low_consumption_gold': payment['low_consumption_balance'],
                'gold_spent': payment['gold_spent'],
                'low_consumption_spent': payment['low_consumption_spent'],
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def leave(user_id: int, seat_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """让自己的冒险者离座。离座时结算茶舍期间的精力恢复。"""
        try:
            built_err = AdventurerS1TeaHouseSeatService._ensure_tea_house_built()
            if built_err:
                return None, built_err

            seat_ref = AdventurerS1TeaHouseSeat.query.with_entities(
                AdventurerS1TeaHouseSeat.adventurer_id,
            ).filter(AdventurerS1TeaHouseSeat.id == seat_id).first()
            if not seat_ref:
                return None, '座位不存在'
            if not seat_ref.adventurer_id:
                return None, '这个座位上没有人'
            adv = AdventurerS1.query.filter(
                AdventurerS1.id == seat_ref.adventurer_id,
            ).populate_existing().with_for_update().first()
            seat = AdventurerS1TeaHouseSeat.query.filter(
                AdventurerS1TeaHouseSeat.id == seat_id,
            ).populate_existing().with_for_update().first()
            if not seat or seat.adventurer_id != seat_ref.adventurer_id:
                return None, '座位状态已经变化，请重试'
            if not adv:
                return None, '座位上的冒险者不存在'
            if adv.owner_id != user_id:
                return None, '只能叫走自己的冒险者'

            now = _portable_utc_now()
            settle_until = now
            if seat.seated_at:
                max_until = seat.seated_at + timedelta(hours=_max_stay_hours())
                settle_until = min(now, max_until)
            AdventurerS1TeaHouseSeatService._settle_stamina(
                seat, adv, now, settle_until=settle_until,
            )

            seat.adventurer_id = None
            seat.seated_at = None
            seat.stamina_settled_at = None
            seat.entry_fee_paid = False
            seat.tea_key = None
            seat.tea_text = None
            seat.ordered_at = None

            from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                AdventurerS1StateTransitionCoordinator,
            )
            AdventurerS1StateTransitionCoordinator.resume_dorm_rest(adv, now)

            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
            invalidate_tea_house_seats_cache()

            return {'seat_id': seat_id}, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def order_tea(user_id: int, seat_id: int, tea_key: str) -> Tuple[Optional[dict], Optional[str]]:
        """给座位上自己的冒险者点茶。茶品免费，只更新座位展示文案，无数值效果。"""
        try:
            built_err = AdventurerS1TeaHouseSeatService._ensure_tea_house_built()
            if built_err:
                return None, built_err

            meta = S1_TEA_HOUSE_TEA_MAP.get(tea_key)
            if not meta:
                return None, '茶单上没有这种茶'

            seat_ref = AdventurerS1TeaHouseSeat.query.with_entities(
                AdventurerS1TeaHouseSeat.adventurer_id,
            ).filter(AdventurerS1TeaHouseSeat.id == seat_id).first()
            if not seat_ref:
                return None, '座位不存在'
            if not seat_ref.adventurer_id:
                return None, '先安排冒险者入座才能点茶'
            adv = AdventurerS1.query.filter(
                AdventurerS1.id == seat_ref.adventurer_id,
            ).populate_existing().with_for_update().first()
            seat = AdventurerS1TeaHouseSeat.query.filter(
                AdventurerS1TeaHouseSeat.id == seat_id,
            ).populate_existing().with_for_update().first()
            if not seat or seat.adventurer_id != seat_ref.adventurer_id:
                return None, '座位状态已经变化，请重试'
            if not adv:
                return None, '座位上的冒险者不存在'
            if adv.owner_id != user_id:
                return None, '只能给自己的冒险者点茶'

            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            if adv.status not in AdventurerS1Service.FREE_STATUSES or adv.building_instance_id:
                seat.adventurer_id = None
                seat.seated_at = None
                seat.stamina_settled_at = None
                seat.entry_fee_paid = False
                seat.tea_key = None
                seat.tea_text = None
                seat.ordered_at = None
                ok, err = player_database_result(db_manager.commit())
                if not ok:
                    return None, err
                return None, '冒险者已经离开茶舍了'

            now = _portable_utc_now()
            settle_until = now
            if seat.seated_at:
                max_until = seat.seated_at + timedelta(hours=_max_stay_hours())
                settle_until = min(now, max_until)
            AdventurerS1TeaHouseSeatService._settle_stamina(
                seat, adv, now, settle_until=settle_until,
            )

            seat.tea_key = tea_key
            seat.tea_text = random.choice(meta['texts'])
            seat.ordered_at = now
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err
            invalidate_tea_house_seats_cache()

            AdventurerS1TeaHouseSeatService._emit_tea_house_changed(
                'tea_house_order_tea',
                seat_id=seat_id,
                adventurer_id=adv.id,
            )
            return {
                'seat_id': seat_id,
                'tea': AdventurerS1TeaHouseSeatService._tea_brief(seat.tea_key, seat.tea_text),
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
