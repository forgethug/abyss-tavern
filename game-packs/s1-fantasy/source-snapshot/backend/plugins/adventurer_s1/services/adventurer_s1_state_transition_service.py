"""S1 冒险者状态切换协调器。

本模块只编排既有休息恢复、座位清场和状态字段写入；事务、行锁与提交仍由
调用方持有，不在这里建立第二套状态机。
"""
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime
from typing import Optional

from plugins.adventurer_s1.models import AdventurerS1


class AdventurerS1StateTransitionCoordinator:
    """复用现有状态与锁序的统一切换入口。"""

    @staticmethod
    def switch_rest_source(
        adv: AdventurerS1,
        target_source: str,
        now: datetime,
        *,
        previous_tavern_seats: Optional[list] = None,
    ) -> None:
        """按既有顺序结清其它休息来源；调用方仍负责目标座位与事务。"""
        if target_source == 'tavern':
            from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import (
                AdventurerS1TeaHouseSeatService,
            )
            from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import (
                AdventurerS1HotSpringSeatService,
            )
            AdventurerS1TeaHouseSeatService.settle_and_clear_seats_for_adventurer(
                adv, now,
            )
            AdventurerS1HotSpringSeatService.settle_and_clear_seats_for_adventurer(
                adv, now,
            )

            previous_seats = list(previous_tavern_seats or ())
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                flush_recovery_to_db,
            )
            # 第一次进入酒馆才冻结此前恢复来源；酒馆内换座保持连续恢复区间。
            if not previous_seats:
                flush_recovery_to_db(adv, now=now, tavern_seated=False)
            from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
                AdventurerS1TavernSeatService,
            )
            AdventurerS1TavernSeatService.settle_and_clear_seat_rows(
                previous_seats,
                adv,
                now,
            )
            from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
                flush_dorm_rest_session,
            )
            flush_dorm_rest_session(
                adv,
                now=now,
                tavern_seated=bool(previous_seats),
            )
            return

        if target_source == 'tea_house':
            from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
                AdventurerS1TavernSeatService,
            )
            from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import (
                AdventurerS1HotSpringSeatService,
            )
            AdventurerS1TavernSeatService.clear_seats_for_adventurer(
                adv.id,
                now=now,
                locked_adv=adv,
            )
            AdventurerS1HotSpringSeatService.settle_and_clear_seats_for_adventurer(
                adv, now,
            )
            from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
                flush_dorm_rest_session,
            )
            flush_dorm_rest_session(adv, now=now)
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                flush_stamina_to_db,
            )
            flush_stamina_to_db(adv, now=now, tavern_seated=False)
            return

        if target_source == 'hot_spring':
            from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
                AdventurerS1TavernSeatService,
            )
            from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import (
                AdventurerS1TeaHouseSeatService,
            )
            AdventurerS1TavernSeatService.clear_seats_for_adventurer(
                adv.id,
                now=now,
                locked_adv=adv,
            )
            AdventurerS1TeaHouseSeatService.settle_and_clear_seats_for_adventurer(
                adv, now,
            )
            from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
                flush_dorm_rest_session,
            )
            flush_dorm_rest_session(adv, now=now)
            from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
                flush_recovery_to_db,
            )
            flush_recovery_to_db(adv, now=now, tavern_seated=False)
            return

        raise ValueError(f'未知休息来源: {target_source}')

    @staticmethod
    def resume_dorm_rest(
        adv: AdventurerS1,
        now: Optional[datetime] = None,
    ) -> bool:
        """离开实体场所后恢复宿舍休息段计时；返回是否改写。"""
        if (
            adv.status != 'resting'
            or adv.dorm_room_id is None
            or adv.bed_index is None
        ):
            return False
        adv.rest_start_time = now or _portable_utc_now()
        return True

    @staticmethod
    def invalidate_rest_source_cache() -> None:
        """保持既有三类入座成功后的共享座位缓存失效。"""
        from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import (
            invalidate_tea_house_seats_cache,
        )
        invalidate_tea_house_seats_cache()

    @staticmethod
    def enter_rest(adv: AdventurerS1, now: Optional[datetime] = None) -> None:
        """让冒险者回到床位休息态，并重建惰性恢复计时。"""
        rest_now = now or _portable_utc_now()
        if adv.status == 'resting':
            if adv.rest_start_time is None:
                adv.rest_start_time = rest_now
            if not adv.stamina_updated_at:
                adv.stamina_updated_at = rest_now
            if not adv.hp_updated_at:
                adv.hp_updated_at = rest_now
            return

        # 重置休息计时前先落账未写入 DB 的惰性恢复；标量已一致则跳过写库。
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
        )
        AdventurerS1Service._flush_building_rest_like_recovery(adv, now=rest_now)
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
            flush_recovery_to_db,
            s1_hp_recovery_minutes_per_point,
            s1_max_stamina,
            s1_stamina_recovery_per_minute,
        )
        live_stamina = int(adv.get_current_stamina(
            max_stamina=s1_max_stamina(),
            recovery_per_minute=s1_stamina_recovery_per_minute(),
            now=rest_now,
        ))
        live_hp = int(adv.get_current_hp(
            recovery_minutes_per_point=s1_hp_recovery_minutes_per_point(),
            now=rest_now,
        ))
        if live_stamina != int(adv.stamina or 0) or live_hp != int(adv.hp or 0):
            flush_recovery_to_db(adv, now=rest_now)

        adv.status = 'resting'
        adv.stamina_updated_at = rest_now
        adv.hp_updated_at = rest_now
        adv.rest_start_time = rest_now

    @staticmethod
    def return_from_building(
        adv: AdventurerS1,
        now: Optional[datetime] = None,
    ) -> None:
        """在专属 Handler 收尾后回到休息态，并清除通用建筑占用字段。

        本入口只编排通用状态与 assignment 清理；材料、费用、治疗、奖励、槽位等
        建筑专属逻辑必须由调用方先通过对应 Handler 完成。
        """
        AdventurerS1StateTransitionCoordinator.enter_rest(adv, now)
        adv.building_instance_id = None
        adv.building_assigned_at = None
        adv.building_usage_extra = None

    @staticmethod
    def leave_rest(
        adv: AdventurerS1,
        new_status: str,
        now: Optional[datetime] = None,
    ) -> None:
        """结清当前休息来源后，把冒险者切换到目标状态。"""
        now = now or _portable_utc_now()
        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
            flush_recovery_to_db,
        )
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
            AdventurerS1TavernSeatService,
        )
        tavern_seated = AdventurerS1TavernSeatService.clear_seats_for_adventurer(
            adv.id,
            now=now,
            locked_adv=adv,
        )
        from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import (
            AdventurerS1TeaHouseSeatService,
        )
        tea_house_seated = AdventurerS1TeaHouseSeatService.settle_and_clear_seats_for_adventurer(
            adv, now,
        )
        if not tavern_seated and not tea_house_seated:
            flush_recovery_to_db(adv, now=now, tavern_seated=False)
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
            flush_dorm_rest_session,
        )
        flush_dorm_rest_session(adv, now=now, tavern_seated=tavern_seated)
        # 状态转换与业务写入同属调用方事务。这里绝不能提交当前 scoped session，
        # 否则会提前释放调用方持有的 adv / ticket / party 行锁。
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
        )
        AdventurerS1Service._settle_due_trait_recovery_for_adventurers(
            [adv],
            auto_commit=False,
        )
        from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import (
            AdventurerS1HotSpringSeatService,
        )
        AdventurerS1HotSpringSeatService.settle_and_clear_seats_for_adventurer(adv, now)
        adv.rest_start_time = None
        adv.status = new_status

    @staticmethod
    def leave_rest_batch(
        adventurers: list[AdventurerS1],
        new_status: str,
        now: Optional[datetime] = None,
        *,
        tavern_seated_ids: Optional[set[int]] = None,
    ) -> None:
        """批量离开休息态；特殊座位仍复用完整单人退出语义。"""
        if not adventurers:
            return
        now = now or _portable_utc_now()
        adventurer_ids = [int(adv.id) for adv in adventurers if adv.id is not None]
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
            AdventurerS1TavernSeatService,
        )
        from plugins.adventurer_s1.services.adventurer_s1_tea_house_seat_service import (
            AdventurerS1TeaHouseSeatService,
        )
        from plugins.adventurer_s1.services.adventurer_s1_hot_spring_seat_service import (
            AdventurerS1HotSpringSeatService,
        )
        tavern_ids = (
            set(tavern_seated_ids)
            if tavern_seated_ids is not None
            else AdventurerS1TavernSeatService.get_seated_adventurer_ids(adventurer_ids)
        )
        tea_house_ids = AdventurerS1TeaHouseSeatService.get_seated_adventurer_ids(
            adventurer_ids,
        )
        hot_spring_ids = AdventurerS1HotSpringSeatService.get_seated_adventurer_ids(
            adventurer_ids,
        )
        special_seated_ids = set(tavern_ids) | set(tea_house_ids) | set(hot_spring_ids)
        ordinary_resting = [
            adv for adv in adventurers if int(adv.id) not in special_seated_ids
        ]
        special_seated = [
            adv for adv in adventurers if int(adv.id) in special_seated_ids
        ]

        from plugins.adventurer_s1.services.adventurer_s1_recovery_service import (
            flush_recovery_to_db,
        )
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import (
            flush_dorm_rest_session,
        )
        for adv in ordinary_resting:
            flush_recovery_to_db(adv, now=now, tavern_seated=False)
            flush_dorm_rest_session(adv, now=now, tavern_seated=False)
        from plugins.adventurer_s1.services.adventurer_s1_service import (
            AdventurerS1Service,
        )
        AdventurerS1Service._settle_due_trait_recovery_for_adventurers(
            ordinary_resting,
            auto_commit=False,
        )
        for adv in ordinary_resting:
            adv.rest_start_time = None
            adv.status = new_status

        # 座位可能携带食物、茶饮或温泉结算，必须复用各自完整退出语义。
        for adv in special_seated:
            AdventurerS1Service.leave_rest(adv, new_status, now=now)
