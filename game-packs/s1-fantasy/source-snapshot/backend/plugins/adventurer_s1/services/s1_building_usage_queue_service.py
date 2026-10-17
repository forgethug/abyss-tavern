"""S1 异步设施候场：显式入队、严格 FIFO、取消退款与空位递补。"""
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingUsageQueueEntry,
)


class S1BuildingUsageQueueService:
    """管理会在建筑计时结束后进入 AI 作业的通用设施候场队列。"""

    QUEUE_STATUS = 'building_queue'
    QUEUE_HANDLER_KEYS = frozenset({
        'healing_house',
        'chapel',
        'blacksmith',
        'tailor_workshop',
        'alchemy_workshop',
        'item_assembly_workshop',
        'ember_kitchen',
        'pet_evolution',
    })
    TASK_TYPE_TO_HANDLER_KEY = {
        'ai_healing_house': 'healing_house',
        'ai_chapel': 'chapel',
        'ai_blacksmith': 'blacksmith',
        'ai_tailor_workshop': 'tailor_workshop',
        'ai_alchemy_workshop': 'alchemy_workshop',
        'ai_item_assembly_workshop': 'item_assembly_workshop',
        'ai_ember_kitchen': 'ember_kitchen',
        'ai_pet_evolution': 'pet_evolution',
    }

    @staticmethod
    def is_queue_handler(handler_key: object) -> bool:
        return str(handler_key or '').strip() in S1BuildingUsageQueueService.QUEUE_HANDLER_KEYS

    @staticmethod
    def format_queue_priority_message(waiting_count: int) -> str:
        return f'已有 {max(1, int(waiting_count or 0))} 人候场，请选择加入队列按顺序等候'

    @staticmethod
    def get_queue_state(
        building_instance_id: int,
        adventurer_id: Optional[int] = None,
    ) -> dict:
        """只返回候场总数与当前玩家位置，不暴露其他玩家资料。"""
        base_query = AdventurerS1BuildingUsageQueueEntry.query.filter_by(
            building_instance_id=building_instance_id,
        )
        waiting_count = base_query.count()
        position = None
        if adventurer_id is not None:
            entry = base_query.filter_by(adventurer_id=adventurer_id).first()
            if entry:
                ahead = base_query.filter(
                    (
                        AdventurerS1BuildingUsageQueueEntry.queued_at < entry.queued_at
                    )
                    | (
                        (
                            AdventurerS1BuildingUsageQueueEntry.queued_at
                            == entry.queued_at
                        )
                        & (AdventurerS1BuildingUsageQueueEntry.id < entry.id)
                    )
                ).count()
                position = int(ahead) + 1
        return {
            'waiting_count': int(waiting_count),
            'position': position,
            'is_waiting': position is not None,
        }

    @staticmethod
    def get_waiting_count_for_buildings(building_instance_ids: list[int]) -> int:
        """批量统计一组建筑的候场总数，供模板级写操作在锁内校验。"""
        normalized_ids = sorted({
            int(building_id)
            for building_id in building_instance_ids
            if building_id is not None
        })
        if not normalized_ids:
            return 0
        return int(AdventurerS1BuildingUsageQueueEntry.query.filter(
            AdventurerS1BuildingUsageQueueEntry.building_instance_id.in_(
                normalized_ids,
            ),
        ).count())

    @staticmethod
    def enqueue(
        building_instance_id: int,
        adv: AdventurerS1,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """在已按 adv→building 顺序持锁的事务内加入 FIFO 队列。"""
        now = now or _portable_utc_now()
        stale = AdventurerS1BuildingUsageQueueEntry.query.filter_by(
            adventurer_id=adv.id,
        ).first()
        if stale:
            ok, err = db_manager.delete_instance(stale)
            if not ok:
                raise RuntimeError(f'清理旧候场记录失败: {err}')

        entry = AdventurerS1BuildingUsageQueueEntry(
            building_instance_id=building_instance_id,
            adventurer_id=adv.id,
            queued_at=now,
        )
        ok, err = db_manager.add(entry)
        if not ok:
            raise RuntimeError(f'加入设施候场失败: {err}')
        ok, err = db_manager.flush()
        if not ok:
            raise RuntimeError(f'加入设施候场失败: {err}')

        ahead = AdventurerS1BuildingUsageQueueEntry.query.filter(
            AdventurerS1BuildingUsageQueueEntry.building_instance_id
            == building_instance_id,
            (
                (AdventurerS1BuildingUsageQueueEntry.queued_at < now)
                | (
                    (AdventurerS1BuildingUsageQueueEntry.queued_at == now)
                    & (AdventurerS1BuildingUsageQueueEntry.id < entry.id)
                )
            ),
        ).count()
        return int(ahead) + 1

    @staticmethod
    def _queue_adventurer_lock_query(adventurer_id: int):
        """非阻塞锁定队首；锁冲突时必须保留队首，禁止越位。"""
        return (
            AdventurerS1.query
            .filter_by(id=adventurer_id)
            .populate_existing()
            .with_for_update(skip_locked=True)
        )

    @staticmethod
    def _invalidate(
        adventurer_id: Optional[int] = None,
        *,
        activity_owner_id: Optional[int] = None,
    ) -> None:
        try:
            from plugins.adventurer_s1.services.adventurer_s1_service import (
                invalidate_adventurer_detail_cache,
                invalidate_buildings_list_cache,
                invalidate_my_activity_cache,
            )
            if adventurer_id is not None:
                invalidate_adventurer_detail_cache(adventurer_id)
            invalidate_buildings_list_cache()
            if activity_owner_id is not None:
                invalidate_my_activity_cache(activity_owner_id)
        except Exception:
            current_app.logger.exception(
                '设施候场缓存失效失败 adv=%s', adventurer_id,
            )

    @staticmethod
    def _emit_change(
        action: str,
        building_instance_id: int,
        adventurer_id: int,
        *,
        reason: str,
    ) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import (
                s1_socketio as socketio,
            )
            socketio.emit('adventurer_changed', {
                'action': action,
                'reason': reason,
                'ids': [adventurer_id],
                'building_instance_id': building_instance_id,
            }, namespace='/')
        except Exception:
            current_app.logger.exception(
                '设施候场推送失败 action=%s inst=%s adv=%s',
                action,
                building_instance_id,
                adventurer_id,
            )

    @staticmethod
    def promote_next_waiting(building_instance_id: int) -> Optional[int]:
        """有空位时原子晋升一个队首；队首锁冲突则整条队列留待重试。"""
        while True:
            head = (
                AdventurerS1BuildingUsageQueueEntry.query
                .filter_by(building_instance_id=building_instance_id)
                .order_by(
                    AdventurerS1BuildingUsageQueueEntry.queued_at.asc(),
                    AdventurerS1BuildingUsageQueueEntry.id.asc(),
                )
                .first()
            )
            if not head:
                return None

            head_id = int(head.id)
            adventurer_id = int(head.adventurer_id)
            promoted = False
            stale_removed = False
            try:
                with db_manager.transaction():
                    adv = S1BuildingUsageQueueService._queue_adventurer_lock_query(
                        adventurer_id,
                    ).first()
                    if adv is None:
                        adv_still_exists = (
                            AdventurerS1.query
                            .with_entities(AdventurerS1.id)
                            .filter_by(id=adventurer_id)
                            .first()
                            is not None
                        )
                        if adv_still_exists:
                            current_app.logger.info(
                                '设施候场晋升延后：队首正由其它事务处理 inst=%s adv=%s',
                                building_instance_id,
                                adventurer_id,
                            )
                            return None

                    inst = (
                        AdventurerS1BuildingInstance.query
                        .filter_by(id=building_instance_id)
                        .populate_existing()
                        .with_for_update()
                        .first()
                    )
                    locked_head = (
                        AdventurerS1BuildingUsageQueueEntry.query
                        .filter_by(building_instance_id=building_instance_id)
                        .order_by(
                            AdventurerS1BuildingUsageQueueEntry.queued_at.asc(),
                            AdventurerS1BuildingUsageQueueEntry.id.asc(),
                        )
                        .populate_existing()
                        .with_for_update()
                        .first()
                    )
                    if not locked_head or int(locked_head.id) != head_id:
                        return None

                    if (
                        not adv
                        or adv.status != S1BuildingUsageQueueService.QUEUE_STATUS
                        or adv.building_instance_id != building_instance_id
                    ):
                        ok, err = db_manager.delete_instance(locked_head)
                        if not ok:
                            raise RuntimeError(f'清理失效候场记录失败: {err}')
                        stale_removed = True
                    elif (
                        not inst
                        or inst.status != 'built'
                        or inst.disabled
                        or not inst.template
                        or not S1BuildingUsageQueueService.is_queue_handler(
                            inst.template.handler_key,
                        )
                    ):
                        # 冒险者与候场记录仍一致时保留现场，让玩家可主动取消并拿回预留资源。
                        return None
                    else:
                        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import (
                            BuildingSettlementService,
                        )
                        from plugins.adventurer_s1.services.adventurer_s1_service import (
                            AdventurerS1Service,
                        )

                        config = BuildingSettlementService.get_reward_config(inst)
                        usage_slots = AdventurerS1Service._effective_usage_slots(inst, config)
                        active_count = AdventurerS1.query.filter_by(
                            building_instance_id=building_instance_id,
                            status='building',
                        ).count()
                        if usage_slots > 0 and active_count >= usage_slots:
                            return None

                        extra = dict(adv.building_usage_extra or {})
                        extra.pop('queue_state', None)
                        extra.pop('queued_at', None)
                        extra.pop('queue_handler_key', None)
                        adv.status = 'building'
                        adv.building_assigned_at = _portable_utc_now()
                        adv.building_usage_extra = extra
                        ok, err = db_manager.delete_instance(locked_head)
                        if not ok:
                            raise RuntimeError(f'移除已晋升候场记录失败: {err}')
                        promoted = True
            except Exception:
                current_app.logger.exception(
                    '设施候场晋升失败 inst=%s adv=%s',
                    building_instance_id,
                    adventurer_id,
                )
                return None

            if promoted:
                S1BuildingUsageQueueService._invalidate(adventurer_id)
                S1BuildingUsageQueueService._emit_change(
                    'building_usage_queue_promoted',
                    building_instance_id,
                    adventurer_id,
                    reason='usage_slot_available',
                )
                return adventurer_id
            if not stale_removed:
                return None

    @staticmethod
    def promote_waiting_for_building(building_instance_id: int) -> list[int]:
        """持续递补一个建筑，直到槽位满、无队列或队首暂时被锁。"""
        promoted: list[int] = []
        while True:
            adventurer_id = S1BuildingUsageQueueService.promote_next_waiting(
                building_instance_id,
            )
            if adventurer_id is None:
                return promoted
            promoted.append(adventurer_id)

    @staticmethod
    def promote_waiting_queues_job() -> int:
        """每分钟补扫全部候场建筑；每次晋升由独立短事务显式提交。"""
        building_ids = [
            int(row[0])
            for row in (
                AdventurerS1BuildingUsageQueueEntry.query
                .with_entities(
                    AdventurerS1BuildingUsageQueueEntry.building_instance_id,
                )
                .distinct()
                .all()
            )
        ]
        return sum(
            len(S1BuildingUsageQueueService.promote_waiting_for_building(building_id))
            for building_id in building_ids
        )

    @staticmethod
    def promote_after_task_completion(task_type: object, payload: object) -> list[int]:
        """异步设施作业释放连接后即时递补；非目标任务为无操作。"""
        handler_key = S1BuildingUsageQueueService.TASK_TYPE_TO_HANDLER_KEY.get(
            str(task_type or '').strip(),
        )
        if not handler_key or not isinstance(payload, dict):
            return []
        try:
            building_instance_id = int(payload.get('building_instance_id'))
        except (TypeError, ValueError):
            return []
        return S1BuildingUsageQueueService.promote_waiting_for_building(
            building_instance_id,
        )

    @staticmethod
    def cancel_queue(
        adventurer_id: int,
        building_instance_id: int,
        *,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """取消未入席的设施候场，原样退还预留金币、精力和嬗变辅助素材。"""
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if not is_admin and user_id is not None and adv.owner_id != user_id:
            return None, '只能操作自己拥有的冒险者'

        refunded_gold = 0.0
        refunded_stamina = 0
        auxiliary_material_refund = None
        try:
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .filter_by(id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not adv:
                    return None, '冒险者不存在'
                inst = (
                    AdventurerS1BuildingInstance.query
                    .filter_by(id=building_instance_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                entry = (
                    AdventurerS1BuildingUsageQueueEntry.query
                    .filter_by(
                        adventurer_id=adventurer_id,
                        building_instance_id=building_instance_id,
                    )
                    .with_for_update()
                    .first()
                )
                if (
                    not entry
                    or adv.status != S1BuildingUsageQueueService.QUEUE_STATUS
                    or adv.building_instance_id != building_instance_id
                ):
                    return None, '这名冒险者当前不在该设施的候场队列中'

                extra = (
                    adv.building_usage_extra
                    if isinstance(adv.building_usage_extra, dict)
                    else {}
                )
                refunded_gold = max(0.0, float(extra.get('gold_cost') or 0))
                refunded_stamina = max(0, int(extra.get('stamina_cost') or 0))
                if refunded_gold > 0:
                    from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
                        S1CurrencyService,
                    )
                    _, refund_err = S1CurrencyService.add(
                        adv.id,
                        'gold',
                        refunded_gold,
                        source_type='building_queue_refund',
                        related_id=building_instance_id,
                        related_type='building_instance',
                        description='取消设施候场，退回预留使用费用',
                        auto_commit=False,
                    )
                    if refund_err:
                        raise RuntimeError(refund_err)
                if refunded_stamina > 0:
                    max_stamina = int(current_app.config.get(
                        'ADVENTURER_S1_STAMINA_MAX',
                        100,
                    ))
                    adv.stamina = min(
                        max_stamina,
                        int(adv.stamina or 0) + refunded_stamina,
                    )

                handler_key = (
                    inst.template.handler_key
                    if inst and inst.template
                    else str(extra.get('queue_handler_key') or '')
                )
                if handler_key == 'pet_evolution':
                    from plugins.adventurer_s1.services.adventurer_s1_building_ai_recovery_service import (
                        AdventurerS1BuildingAiRecoveryService,
                    )
                    auxiliary_material_refund = (
                        AdventurerS1BuildingAiRecoveryService
                        ._refund_pet_evolution_auxiliary_material(
                            adv,
                            extra,
                            str(extra.get('usage_session_id') or ''),
                            refund_source_type='pet_evolution_queue_refund',
                            refund_description='取消嬗变祭坛候场，退还辅助素材',
                        )
                    )

                ok, delete_err = db_manager.delete_instance(entry)
                if not ok:
                    raise RuntimeError(delete_err or '移除候场记录失败')
                from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
                    AdventurerS1StateTransitionCoordinator,
                )
                AdventurerS1StateTransitionCoordinator.return_from_building(adv)
        except Exception as exc:
            return None, player_operation_failure(exc, '取消候场失败')

        S1BuildingUsageQueueService._invalidate(
            adventurer_id,
            activity_owner_id=adv.owner_id,
        )
        S1BuildingUsageQueueService._emit_change(
            'building_usage_queue_cancelled',
            building_instance_id,
            adventurer_id,
            reason='player_cancelled',
        )
        S1BuildingUsageQueueService.promote_waiting_for_building(
            building_instance_id,
        )

        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        result = {
            'adventurer': AdventurerS1Service._serialize_adventurer_for_client(
                adv,
                user_id,
                is_admin=is_admin,
            ),
            'gold_refunded': round(refunded_gold, 2),
            'stamina_refunded': refunded_stamina,
            'auxiliary_material_refunded': bool(auxiliary_material_refund),
        }
        if auxiliary_material_refund:
            result['auxiliary_material_refund'] = auxiliary_material_refund
        return result, None
