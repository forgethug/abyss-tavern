"""深渊温泉座位服务。

照抄深渊茶舍座位服务，区别：
- 同时恢复生命值（1 点 / 2 分钟）与精力（1 点 / 1 分钟）；
- 入场费 200 金币；
- 座位布局为两个 4 人小池 + 一个 24 人大池，无 Lv2 锁桌；
- 除超 6 小时上限外，生命与精力均回满也会被 scheduler 自动清座（列表只读）。
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
    AdventurerS1DormRoom,
    AdventurerS1HotSpringSeat,
)


_SEAT_CLEAR_FIELDS = {
    'adventurer_id': None,
    'seated_at': None,
    'hp_settled_at': None,
    'stamina_settled_at': None,
    'entry_fee_paid': False,
    'drink_key': None,
    'drink_text': None,
    'ordered_at': None,
}


# 温泉饮品：纯展示文案，无数值效果（入场费已含）。
S1_HOT_SPRING_DRINKS = [
    {
        'key': 'spring_egg',
        'name': '温泉蛋',
        'icon': '🥚',
        'price': 0,
        'desc': '在泉眼里煨到半熟的蛋，蛋黄绵软，泡汤时剥着吃最惬意。',
        'texts': [
            '小心翼翼地剥着蛋壳，热气在指间散开。',
            '咬了一口蛋黄，满足地眯起眼睛靠回池壁。',
            '把蛋在水面上转了转，像在数今天还剩多少好运。',
        ],
    },
    {
        'key': 'cold_sake',
        'name': '冰镇清酒',
        'icon': '🍶',
        'price': 0,
        'desc': '泡在热汤里却喝着冰酒，冷热交替最是上瘾。',
        'texts': [
            '抿了一口冰酒，又沉进热汤，舒服地长出一口气。',
            '把酒杯搁在池沿，看水汽把杯壁蒙得朦胧。',
            '酒过半杯，话也多了起来，笑声在雾里回荡。',
        ],
    },
    {
        'key': 'herbal_cooler',
        'name': '凉草饮',
        'icon': '🌿',
        'price': 0,
        'desc': '清火的草本凉饮，专治泡得头晕脑胀。',
        'texts': [
            '咕咚喝下半杯，被热气熏红的脸总算缓了过来。',
            '用凉饮贴了贴额头，舒服得直哼哼。',
            '小口抿着，说这味道像雨后山涧的青苔。',
        ],
    },
    {
        'key': 'plum_juice',
        'name': '酸梅汤',
        'icon': '🥤',
        'price': 0,
        'desc': '酸甜冰镇的酸梅汤，解腻又解乏。',
        'texts': [
            '喝得太急被酸得皱眉，却又忍不住续了一口。',
            '把冰块含在嘴里，眯着眼看池面的雾。',
            '一口气见底，长长地舒了口气。',
        ],
    },
    {
        'key': 'hot_milk',
        'name': '温牛乳',
        'icon': '🥛',
        'price': 0,
        'desc': '温热的奶，配着泉气喝下去，整个人都软下来。',
        'texts': [
            '捧着杯子小口喝着，眼神渐渐变得困倦。',
            '喝完抹了抹嘴边的奶痕，不好意思地笑了。',
            '把杯子贴在脸颊上取暖，舒服得叹气。',
        ],
    },
    {
        'key': 'ginger_tea',
        'name': '姜汤',
        'icon': '🍵',
        'price': 0,
        'desc': '驱寒的姜汤，泡完汤再喝一碗，寒气全无。',
        'texts': [
            '被姜味呛了一下，却很认真地喝完了整碗。',
            '喝完额头沁出薄汗，整个人都暖透了。',
            '捧着碗发呆，神情比入池时柔和了许多。',
        ],
    },
    {
        'key': 'osmanthus_wine',
        'name': '桂花酿',
        'icon': '🌼',
        'price': 0,
        'desc': '带着桂花甜香的米酿，配着雾气像把秋天泡进了汤里。',
        'texts': [
            '闻了许久才入口，像怕错过那缕桂香。',
            '靠在池边慢慢呷着，眼神沉进很远的回忆。',
            '酒香落下后轻轻笑了一声，没解释想到了什么。',
        ],
    },
    {
        'key': 'iced_fruit',
        'name': '冰镇鲜果',
        'icon': '🍉',
        'price': 0,
        'desc': '冰盆里湃着的时令鲜果，泡汤时啃一口最是爽快。',
        'texts': [
            '咬了一大口冰果，凉意顺着喉咙压下了暑气。',
            '把果核弹进木桶，得意地比了个手势。',
            '挑了最大的一块，认真地吃得满手是汁。',
        ],
    },
    {
        'key': 'honey_water',
        'name': '蜂蜜水',
        'icon': '🍯',
        'price': 0,
        'desc': '温温的蜂蜜水，甜得很克制，适合泡到犯困时提神。',
        'texts': [
            '小口抿着甜水，肩膀一点点松了下来。',
            '把杯底的蜜搅匀，看糖丝在水里慢慢化开。',
            '喝完舔了舔唇角的甜，露出一点困意。',
        ],
    },
    {
        'key': 'bamboo_water',
        'name': '竹沥清水',
        'icon': '💧',
        'price': 0,
        'desc': '温泉主人私藏的竹沥清水，味极淡，却压得住一身燥意。',
        'texts': [
            '喝下去后安静了很久，像终于听见自己的呼吸。',
            '水色几乎透明，却让眼底的疲惫淡了一层。',
            '轻轻搁下杯子，没有评价，只是把背舒展开来。',
        ],
    },
]

S1_HOT_SPRING_DRINK_MAP = {d['key']: d for d in S1_HOT_SPRING_DRINKS}


def _entry_fee() -> float:
    return float(current_app.config.get('ADVENTURER_S1_HOT_SPRING_ENTRY_FEE', 200))


def _max_stay_hours() -> int:
    return int(current_app.config.get('ADVENTURER_S1_HOT_SPRING_MAX_STAY_HOURS', 6))


def _minutes_per_hp() -> int:
    return max(1, int(current_app.config.get('ADVENTURER_S1_HOT_SPRING_MINUTES_PER_HP', 2)))


def _stamina_per_minute() -> int:
    return max(1, int(current_app.config.get('ADVENTURER_S1_HOT_SPRING_STAMINA_PER_MINUTE', 1)))


def _max_stamina() -> int:
    return int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))


class AdventurerS1HotSpringSeatService:
    """深渊温泉座位：入座 / 离座 / 点饮品 / 全景列表。"""

    @staticmethod
    def get_drinks_menu() -> Tuple[Optional[list], Optional[str]]:
        """饮品单（不含文案池本体）。静态数据，前端可一次拉取后缓存。"""
        menu = [
            {
                'key': d['key'],
                'name': d['name'],
                'icon': d['icon'],
                'price': d['price'],
                'desc': d['desc'],
            }
            for d in S1_HOT_SPRING_DRINKS
        ]
        return menu, None

    @staticmethod
    def _drink_brief(drink_key: str, drink_text: Optional[str]) -> Optional[dict]:
        """座位上展示的饮品信息：饮品单元数据 + 该次点单抽到的文案。"""
        meta = S1_HOT_SPRING_DRINK_MAP.get(drink_key)
        if not meta:
            return None
        return {
            'key': meta['key'],
            'name': meta['name'],
            'icon': meta['icon'],
            'price': meta['price'],
            'text': drink_text or '',
        }

    @staticmethod
    def _ensure_hot_spring_built() -> Optional[str]:
        exists = (
            AdventurerS1BuildingInstance.query
            .join(AdventurerS1BuildingTemplate)
            .filter(
                AdventurerS1BuildingTemplate.handler_key == 'hot_spring',
                AdventurerS1BuildingTemplate.is_active.is_(True),
                AdventurerS1BuildingInstance.status == 'built',
            )
            .first()
        )
        if not exists:
            return '深渊温泉尚未建成'
        return None

    @staticmethod
    def _settle_hp(
        seat: AdventurerS1HotSpringSeat,
        adv: AdventurerS1,
        now: datetime,
        *,
        settle_until: Optional[datetime] = None,
    ) -> bool:
        """把温泉回血落账一次，返回是否修改了数据。

        `seated_at` 保留为原始入座时间用于 6 小时上限判定；
        `hp_settled_at` 是增量结算基准，避免刷新列表重复回血。
        回血速率为整数（每 N 分钟 1 点），结算基准只推进「已消耗的整分钟数」，
        保留不足 N 分钟的余数累积到下次结算，避免高频轮询把零头吞掉。
        """
        if not seat.seated_at:
            return False

        start = seat.hp_settled_at or seat.seated_at
        end = settle_until or now
        if end <= start:
            return False

        elapsed_minutes = int((end - start).total_seconds() / 60)
        if elapsed_minutes <= 0:
            return False

        minutes_per_hp = _minutes_per_hp()
        recover_points = elapsed_minutes // minutes_per_hp
        if recover_points <= 0:
            return False

        max_hp = int(adv.max_hp or 100)
        current_hp = max(0, int(adv.hp or 0))
        new_hp = min(max_hp, current_hp + recover_points)

        if new_hp != current_hp:
            adv.hp = new_hp
            adv.hp_updated_at = end

        consumed_minutes = recover_points * minutes_per_hp
        seat.hp_settled_at = start + timedelta(minutes=consumed_minutes)
        return True

    @staticmethod
    def _settle_stamina(
        seat: AdventurerS1HotSpringSeat,
        adv: AdventurerS1,
        now: datetime,
        *,
        settle_until: Optional[datetime] = None,
    ) -> bool:
        """把温泉恢复精力落账一次，返回是否修改了数据。

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

        current_stamina = max(0, int(adv.stamina or 0))
        recovery = elapsed_minutes * _stamina_per_minute()
        new_stamina = min(_max_stamina(), current_stamina + recovery)

        if new_stamina != current_stamina:
            adv.stamina = new_stamina
            adv.stamina_updated_at = end

        seat.stamina_settled_at = end
        return True

    @staticmethod
    def _seat_needs_settlement(seat: AdventurerS1HotSpringSeat, settle_until: datetime) -> bool:
        """是否已累积足够时间，值得落账一次（避免无增量时 flush/commit）。"""
        if not seat.seated_at:
            return False
        start_hp = seat.hp_settled_at or seat.seated_at
        start_sta = seat.stamina_settled_at or seat.seated_at
        elapsed_hp = int((settle_until - start_hp).total_seconds() / 60)
        elapsed_sta = int((settle_until - start_sta).total_seconds() / 60)
        return elapsed_hp >= _minutes_per_hp() or elapsed_sta >= 1

    @staticmethod
    def _settle_seat_recovery(
        seat: AdventurerS1HotSpringSeat,
        adv: AdventurerS1,
        now: datetime,
        *,
        settle_until: Optional[datetime] = None,
        force: bool = False,
    ) -> bool:
        """落账宿舍惰性恢复 + 温泉 HP/精力，返回是否有写入。"""
        end = settle_until or now
        if not force and not AdventurerS1HotSpringSeatService._seat_needs_settlement(seat, end):
            return False

        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
            flush_hp_to_db,
            flush_stamina_to_db,
        )
        flush_hp_to_db(adv, now=now, tavern_seated=False)
        flush_stamina_to_db(adv, now=now, tavern_seated=False)
        changed = AdventurerS1HotSpringSeatService._settle_hp(
            seat, adv, now, settle_until=settle_until,
        )
        changed = AdventurerS1HotSpringSeatService._settle_stamina(
            seat, adv, now, settle_until=settle_until,
        ) or changed
        return changed

    @staticmethod
    def _is_fully_recovered(adv: AdventurerS1) -> bool:
        max_hp = int(adv.max_hp or 100)
        current_hp = int(adv.hp or 0)
        current_stamina = int(adv.stamina or 0)
        return current_hp >= max_hp and current_stamina >= _max_stamina()

    @staticmethod
    def get_seated_adventurer_ids(adventurer_ids=None) -> set:
        """批量查询当前占温泉座位的冒险者 ID。"""
        q = AdventurerS1HotSpringSeat.query.with_entities(
            AdventurerS1HotSpringSeat.adventurer_id,
        ).filter(AdventurerS1HotSpringSeat.adventurer_id.isnot(None))
        if adventurer_ids is not None:
            ids = list(adventurer_ids)
            if not ids:
                return set()
            q = q.filter(AdventurerS1HotSpringSeat.adventurer_id.in_(ids))
        return {row[0] for row in q.all()}

    @staticmethod
    def clear_seats_for_adventurer(adventurer_id: int) -> None:
        """清掉指定冒险者占用的所有温泉座位，不做收益结算。"""
        AdventurerS1HotSpringSeat.query.filter(
            AdventurerS1HotSpringSeat.adventurer_id == adventurer_id,
        ).update(_SEAT_CLEAR_FIELDS, synchronize_session=False)

    @staticmethod
    def settle_and_clear_seats_for_adventurer(
        adv: AdventurerS1,
        now: Optional[datetime] = None,
        *,
        locked_seats: Optional[list[AdventurerS1HotSpringSeat]] = None,
    ) -> None:
        """冒险者离开休息态前调用：结算温泉回血并清座。"""
        now = now or _portable_utc_now()
        seats = list(locked_seats) if locked_seats is not None else (
            AdventurerS1HotSpringSeat.query.filter(
                AdventurerS1HotSpringSeat.adventurer_id == adv.id,
            ).order_by(
                AdventurerS1HotSpringSeat.id.asc(),
            ).populate_existing().with_for_update().all()
        )
        for seat in seats:
            settle_until = now
            if seat.seated_at:
                max_until = seat.seated_at + timedelta(hours=_max_stay_hours())
                settle_until = min(now, max_until)
            AdventurerS1HotSpringSeatService._settle_seat_recovery(
                seat, adv, now, settle_until=settle_until, force=True,
            )
            seat.adventurer_id = None
            seat.seated_at = None
            seat.hp_settled_at = None
            seat.stamina_settled_at = None
            seat.entry_fee_paid = False
            seat.drink_key = None
            seat.drink_text = None
            seat.ordered_at = None

    @staticmethod
    def tick_hot_spring_recovery_job() -> None:
        """Scheduler：按冒险者加锁结算温泉恢复并回收陈旧座位。"""
        candidate_rows = AdventurerS1HotSpringSeat.query.with_entities(
            AdventurerS1HotSpringSeat.id,
            AdventurerS1HotSpringSeat.adventurer_id,
        ).filter(
            AdventurerS1HotSpringSeat.adventurer_id.isnot(None),
        ).order_by(AdventurerS1HotSpringSeat.id.asc()).all()
        if not candidate_rows:
            return

        seat_ids_by_adv: dict[int, list[int]] = {}
        for row in candidate_rows:
            seat_ids_by_adv.setdefault(int(row.adventurer_id), []).append(int(row.id))

        now = _portable_utc_now()
        max_stay = timedelta(hours=_max_stay_hours())
        facility_available = AdventurerS1HotSpringSeatService._ensure_hot_spring_built() is None
        cleared_adv_ids: set[int] = set()

        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        from sqlalchemy.orm import load_only, noload, selectinload

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
                        AdventurerS1.building_assigned_at,
                        AdventurerS1.dorm_room_id,
                        AdventurerS1.bed_index,
                        AdventurerS1.stamina,
                        AdventurerS1.stamina_updated_at,
                        AdventurerS1.hp,
                        AdventurerS1.max_hp,
                        AdventurerS1.hp_updated_at,
                    ),
                    noload(AdventurerS1.traits),
                    noload(AdventurerS1.equipment_items),
                    noload(AdventurerS1.skill_items),
                    noload(AdventurerS1.investor_items),
                    noload(AdventurerS1.active_title_config),
                    selectinload(AdventurerS1.dorm_room).options(
                        noload(AdventurerS1DormRoom.occupants),
                        selectinload(AdventurerS1DormRoom.building_instance)
                        .selectinload(AdventurerS1BuildingInstance.template),
                    ),
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
                AdventurerS1HotSpringSeat.query
                .filter(
                    AdventurerS1HotSpringSeat.id.in_(candidate_ids),
                    AdventurerS1HotSpringSeat.adventurer_id.in_(
                        [*adv_map, *missing_adv_ids]
                    ),
                )
                .order_by(AdventurerS1HotSpringSeat.id.asc())
                .populate_existing()
                .with_for_update(skip_locked=True)
                .all()
            ) if candidate_ids else []
            active_adv_ids: set[int] = set()

            for seat in seats:
                expected_adv_id = candidate_adv_by_seat.get(seat.id)
                if not expected_adv_id or seat.adventurer_id != expected_adv_id:
                    continue
                adv = adv_map.get(expected_adv_id)
                if adv is None:
                    for field, value in _SEAT_CLEAR_FIELDS.items():
                        setattr(seat, field, value)
                    continue

                should_clear = (
                    not facility_available
                    or not seat.seated_at
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
                    if timed_out or AdventurerS1HotSpringSeatService._seat_needs_settlement(
                        seat,
                        settle_until,
                    ):
                        AdventurerS1HotSpringSeatService._settle_seat_recovery(
                            seat,
                            adv,
                            now,
                            settle_until=settle_until,
                        )
                    if timed_out or AdventurerS1HotSpringSeatService._is_fully_recovered(adv):
                        should_clear = True

                if should_clear:
                    for field, value in _SEAT_CLEAR_FIELDS.items():
                        setattr(seat, field, value)
                    cleared_adv_ids.add(adv.id)

            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or '温泉恢复 tick 提交失败')
        except Exception:
            db_manager.rollback()
            raise

        if cleared_adv_ids:
            AdventurerS1HotSpringSeatService._emit_hot_spring_changed(
                'hot_spring_auto_leave',
                ids=sorted(cleared_adv_ids),
            )

    @staticmethod
    def _emit_hot_spring_changed(action: str, **payload) -> None:
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
        """只读返回座位全景；陈旧座位仅在响应中投影为空位。"""
        try:
            built_err = AdventurerS1HotSpringSeatService._ensure_hot_spring_built()
            if built_err:
                return None, built_err

            seats = AdventurerS1HotSpringSeat.query.order_by(
                AdventurerS1HotSpringSeat.sort_order.asc(),
            ).all()

            seated_ids = [s.adventurer_id for s in seats if s.adventurer_id]
            adv_map = {}
            if seated_ids:
                rows = AdventurerS1.query.with_entities(
                    AdventurerS1.id,
                    AdventurerS1.name,
                    AdventurerS1.status,
                    AdventurerS1.owner_id,
                    AdventurerS1.building_instance_id,
                    AdventurerS1.hp,
                    AdventurerS1.max_hp,
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
                if (
                    adv is None
                    or not seat.seated_at
                    or adv.status not in AdventurerS1Service.FREE_STATUSES
                    or bool(adv.building_instance_id)
                    or now > seat.seated_at + max_stay
                    or AdventurerS1HotSpringSeatService._is_fully_recovered(adv)
                ):
                    stale_seat_ids.append(seat.id)

            adv_name_map = {adv.id: adv.name for adv in adv_map.values()}
            from plugins.adventurer_s1.services.adventurer_s1_hot_spring_chatter_service import (
                AdventurerS1HotSpringChatterService,
            )
            chatter_by_seat = AdventurerS1HotSpringChatterService.generate_for_seats(
                seats, adv_name_map, stale_seat_ids,
            )

            result = []
            for seat in seats:
                adventurer = None
                drink = None
                if seat.adventurer_id and seat.id not in stale_seat_ids:
                    adv = adv_map.get(seat.adventurer_id)
                    if adv:
                        adventurer = {
                            'id': adv.id,
                            'name': adv.name,
                            'is_mine': adv.owner_id == user_id,
                        }
                        if seat.drink_key:
                            drink = AdventurerS1HotSpringSeatService._drink_brief(
                                seat.drink_key, seat.drink_text,
                            )
                seat_data = seat.to_dict(adventurer=adventurer, drink=drink)
                seat_data['chatter'] = chatter_by_seat.get(seat.id)
                result.append(seat_data)

            return {'seats': result}, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def sit(user_id: int, seat_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """让自己的空闲冒险者入座；已坐在别处则自动换座。"""
        try:
            built_err = AdventurerS1HotSpringSeatService._ensure_hot_spring_built()
            if built_err:
                return None, built_err

            seat_row = AdventurerS1HotSpringSeat.query.session.get(AdventurerS1HotSpringSeat, seat_id)
            if not seat_row:
                return None, '座位不存在'

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
            if int(adv.hp or 0) >= int(adv.max_hp or 100) and int(adv.stamina or 0) >= _max_stamina():
                return None, '该冒险者生命与精力均已满，无需泡温泉'

            seated_now = _portable_utc_now()
            affected = AdventurerS1HotSpringSeat.query.filter(
                AdventurerS1HotSpringSeat.id == seat_id,
                AdventurerS1HotSpringSeat.adventurer_id.is_(None),
            ).update({
                'adventurer_id': adventurer_id,
                'seated_at': seated_now,
                'hp_settled_at': seated_now,
                'stamina_settled_at': seated_now,
                'entry_fee_paid': False,
                'drink_key': None,
                'drink_text': None,
                'ordered_at': None,
            }, synchronize_session='fetch')
            if not affected:
                seat = AdventurerS1HotSpringSeat.query.session.get(AdventurerS1HotSpringSeat, seat_id)
                db_manager.rollback()
                if not seat:
                    return None, '座位不存在'
                return None, '这个座位已经有人了'

            AdventurerS1HotSpringSeat.query.filter(
                AdventurerS1HotSpringSeat.adventurer_id == adventurer_id,
                AdventurerS1HotSpringSeat.id != seat_id,
            ).update(_SEAT_CLEAR_FIELDS, synchronize_session='fetch')

            from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                AdventurerS1StateTransitionCoordinator,
            )
            AdventurerS1StateTransitionCoordinator.switch_rest_source(
                adv,
                'hot_spring',
                seated_now,
            )

            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            fee = _entry_fee()
            new_balance, err = S1CurrencyService.deduct(
                adv.id, 'gold', fee,
                source_type='hot_spring_entry',
                related_id=seat_id,
                related_type='hot_spring_seat',
                description='温泉入场费',
                auto_commit=False,
            )
            if err:
                db_manager.rollback()
                if '余额不足' in err:
                    return None, f'金币不足，温泉入场费需要 {fee:g} 金币'
                return None, err

            seat = AdventurerS1HotSpringSeat.query.session.get(AdventurerS1HotSpringSeat, seat_id)
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
                'gold_balance': new_balance,
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def leave(user_id: int, seat_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """让自己的冒险者离座。离座时结算温泉期间的回血。"""
        try:
            built_err = AdventurerS1HotSpringSeatService._ensure_hot_spring_built()
            if built_err:
                return None, built_err

            seat_ref = AdventurerS1HotSpringSeat.query.with_entities(
                AdventurerS1HotSpringSeat.adventurer_id,
            ).filter(AdventurerS1HotSpringSeat.id == seat_id).first()
            if not seat_ref:
                return None, '座位不存在'
            if not seat_ref.adventurer_id:
                return None, '这个座位上没有人'
            adv = AdventurerS1.query.filter(
                AdventurerS1.id == seat_ref.adventurer_id,
            ).populate_existing().with_for_update().first()
            seat = AdventurerS1HotSpringSeat.query.filter(
                AdventurerS1HotSpringSeat.id == seat_id,
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
            AdventurerS1HotSpringSeatService._settle_seat_recovery(
                seat, adv, now, settle_until=settle_until, force=True,
            )

            seat.adventurer_id = None
            seat.seated_at = None
            seat.hp_settled_at = None
            seat.stamina_settled_at = None
            seat.entry_fee_paid = False
            seat.drink_key = None
            seat.drink_text = None
            seat.ordered_at = None

            from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                AdventurerS1StateTransitionCoordinator,
            )
            AdventurerS1StateTransitionCoordinator.resume_dorm_rest(adv, now)

            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err

            return {'seat_id': seat_id}, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')

    @staticmethod
    def order_drink(user_id: int, seat_id: int, drink_key: str) -> Tuple[Optional[dict], Optional[str]]:
        """给座位上自己的冒险者点饮品。饮品免费，只更新座位展示文案，无数值效果。"""
        try:
            built_err = AdventurerS1HotSpringSeatService._ensure_hot_spring_built()
            if built_err:
                return None, built_err

            meta = S1_HOT_SPRING_DRINK_MAP.get(drink_key)
            if not meta:
                return None, '饮品单上没有这种饮品'

            seat_ref = AdventurerS1HotSpringSeat.query.with_entities(
                AdventurerS1HotSpringSeat.adventurer_id,
            ).filter(AdventurerS1HotSpringSeat.id == seat_id).first()
            if not seat_ref:
                return None, '座位不存在'
            if not seat_ref.adventurer_id:
                return None, '先安排冒险者入座才能点饮品'
            adv = AdventurerS1.query.filter(
                AdventurerS1.id == seat_ref.adventurer_id,
            ).populate_existing().with_for_update().first()
            seat = AdventurerS1HotSpringSeat.query.filter(
                AdventurerS1HotSpringSeat.id == seat_id,
            ).populate_existing().with_for_update().first()
            if not seat or seat.adventurer_id != seat_ref.adventurer_id:
                return None, '座位状态已经变化，请重试'
            if not adv:
                return None, '座位上的冒险者不存在'
            if adv.owner_id != user_id:
                return None, '只能给自己的冒险者点饮品'

            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            if adv.status not in AdventurerS1Service.FREE_STATUSES or adv.building_instance_id:
                seat.adventurer_id = None
                seat.seated_at = None
                seat.hp_settled_at = None
                seat.stamina_settled_at = None
                seat.entry_fee_paid = False
                seat.drink_key = None
                seat.drink_text = None
                seat.ordered_at = None
                ok, err = player_database_result(db_manager.commit())
                if not ok:
                    return None, err
                return None, '冒险者已经离开温泉了'

            now = _portable_utc_now()
            settle_until = now
            if seat.seated_at:
                max_until = seat.seated_at + timedelta(hours=_max_stay_hours())
                settle_until = min(now, max_until)
            AdventurerS1HotSpringSeatService._settle_seat_recovery(
                seat, adv, now, settle_until=settle_until, force=True,
            )

            seat.drink_key = drink_key
            seat.drink_text = random.choice(meta['texts'])
            seat.ordered_at = now
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return None, err

            AdventurerS1HotSpringSeatService._emit_hot_spring_changed(
                'hot_spring_order_drink',
                seat_id=seat_id,
                adventurer_id=adv.id,
            )
            return {
                'seat_id': seat_id,
                'drink': AdventurerS1HotSpringSeatService._drink_brief(seat.drink_key, seat.drink_text),
            }, None
        except Exception as e:
            db_manager.rollback()
            return None, player_operation_failure(e, '操作失败')
