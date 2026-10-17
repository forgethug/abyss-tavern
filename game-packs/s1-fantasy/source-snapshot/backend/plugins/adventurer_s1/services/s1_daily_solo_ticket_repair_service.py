"""管理员预览并补发“每日随机单排”任务遗漏的抽奖券。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1ItemLog,
    AdventurerS1MatchTicket,
    AdventurerS1MatchTicketMember,
    AdventurerS1TaskInstance,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.adventurer_s1_task_service import (
    AdventurerS1TaskService,
    _daily_reset_family_filter,
    _json_array_is_empty_or_null,
)

_TASK_ID = 'daily_solo_random_wheel'
_REWARD_ITEM_NAME = '大转盘抽奖券'
_REWARD_QUANTITY = 2


class S1DailySoloTicketRepairService:
    """只修复有任务实例、完成日志及随机单排票据三方证据的遗漏。"""

    @staticmethod
    def _days(value: object) -> Tuple[Optional[int], Optional[str]]:
        default_days = int(current_app.config.get(
            'ADVENTURER_S1_DAILY_SOLO_TICKET_REPAIR_DEFAULT_DAYS', 7,
        ))
        max_days = int(current_app.config.get(
            'ADVENTURER_S1_DAILY_SOLO_TICKET_REPAIR_MAX_DAYS', 30,
        ))
        if value in (None, ''):
            return default_days, None
        try:
            days = int(value)
        except (TypeError, ValueError):
            return None, 'days 必须是整数'
        if days < 1 or days > max_days:
            return None, f'days 必须在 1 到 {max_days} 之间'
        return days, None

    @classmethod
    def preview(cls, *, days: object = None) -> Tuple[Optional[dict], Optional[str]]:
        days_value, err = cls._days(days)
        if err:
            return None, err
        cutoff = _portable_utc_now() - timedelta(days=days_value)
        candidate_limit = int(current_app.config.get(
            'ADVENTURER_S1_DAILY_SOLO_TICKET_REPAIR_CANDIDATE_LIMIT', 500,
        ))

        from models.user import User

        ticket_matches_log = or_(
            AdventurerS1MatchTicket.matched_party_id == AdventurerS1ExpeditionLog.party_id,
            (
                (AdventurerS1MatchTicket.matched_at <= AdventurerS1ExpeditionLog.created_at)
                & AdventurerS1MatchTicket.cancelled_at.isnot(None)
                & (AdventurerS1MatchTicket.cancelled_at >= AdventurerS1ExpeditionLog.created_at)
            ),
        )
        rows = (
            db_manager.query(
                AdventurerS1TaskInstance,
                AdventurerS1,
                User,
                AdventurerS1ExpeditionLog,
                AdventurerS1MatchTicket,
            )
            .join(AdventurerS1, AdventurerS1.id == AdventurerS1TaskInstance.adventurer_id)
            .outerjoin(User, User.id == AdventurerS1.owner_id)
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.adventurer_id == AdventurerS1.id,
            )
            .join(
                AdventurerS1ExpeditionLog,
                AdventurerS1ExpeditionLog.id
                == AdventurerS1ExpeditionLogMember.expedition_log_id,
            )
            .join(
                AdventurerS1MatchTicketMember,
                AdventurerS1MatchTicketMember.adventurer_id == AdventurerS1.id,
            )
            .join(
                AdventurerS1MatchTicket,
                AdventurerS1MatchTicket.id == AdventurerS1MatchTicketMember.ticket_id,
            )
            .filter(
                AdventurerS1TaskInstance.status == 'accepted',
                _daily_reset_family_filter(_TASK_ID),
                AdventurerS1.status != 'dead',
                AdventurerS1ExpeditionLog.status == 'completed',
                AdventurerS1ExpeditionLog.updated_at >= cutoff,
                AdventurerS1ExpeditionLog.created_at >= AdventurerS1TaskInstance.started_at,
                AdventurerS1MatchTicket.queue_type == 'solo',
                AdventurerS1MatchTicket.matched_at.isnot(None),
                AdventurerS1MatchTicket.preferred_dungeon.is_(None),
                _json_array_is_empty_or_null(AdventurerS1MatchTicket.preferred_dungeons),
                ticket_matches_log,
            )
            .order_by(
                AdventurerS1ExpeditionLog.updated_at.desc(),
                AdventurerS1MatchTicket.matched_at.desc(),
            )
            .all()
        )

        # 同一任务可能命中多个日志/票据，只保留最新、证据最接近的一组。
        unique_rows = {}
        for row in rows:
            instance = row[0]
            unique_rows.setdefault(int(instance.id), row)
        truncated = len(unique_rows) > candidate_limit
        selected = list(unique_rows.values())[:candidate_limit]
        instance_ids = [int(row[0].id) for row in selected]
        adventurer_ids = sorted({int(row[1].id) for row in selected})

        rewarded_ids = set()
        if instance_ids:
            rewarded_ids = {
                int(related_id)
                for (related_id,) in db_manager.query(AdventurerS1ItemLog.related_id).filter(
                    AdventurerS1ItemLog.related_id.in_([str(value) for value in instance_ids]),
                    AdventurerS1ItemLog.related_type == 'task_instance',
                    AdventurerS1ItemLog.source_type == 'task_reward',
                    AdventurerS1ItemLog.item_name == _REWARD_ITEM_NAME,
                ).all()
                if str(related_id or '').isdigit()
            }

        ticket_totals = {}
        if adventurer_ids:
            for adventurer_id, quantity in db_manager.query(
                AdventurerS1WarehouseItem.adventurer_id,
                AdventurerS1WarehouseItem.quantity,
            ).filter(
                AdventurerS1WarehouseItem.adventurer_id.in_(adventurer_ids),
                AdventurerS1WarehouseItem.name == _REWARD_ITEM_NAME,
            ).all():
                key = int(adventurer_id)
                ticket_totals[key] = ticket_totals.get(key, 0) + int(quantity or 0)

        items = []
        for instance, adventurer, user, log, ticket in selected:
            already_rewarded = int(instance.id) in rewarded_ids
            items.append({
                'task_instance_id': int(instance.id),
                'adventurer_id': int(adventurer.id),
                'adventurer_name': adventurer.name,
                'owner_id': adventurer.owner_id,
                'owner_nickname': (user.nickname or user.username) if user else '已删除用户',
                'expedition_log_id': int(log.id),
                'target_dungeon': log.target_dungeon,
                'expedition_completed_at': log.updated_at.isoformat() if log.updated_at else None,
                'match_ticket_id': int(ticket.id),
                'current_ticket_quantity': ticket_totals.get(int(adventurer.id), 0),
                'reward_quantity': _REWARD_QUANTITY,
                'action': 'mark_completed' if already_rewarded else 'grant_reward',
            })

        return {
            'days': days_value,
            'max_days': int(current_app.config.get(
                'ADVENTURER_S1_DAILY_SOLO_TICKET_REPAIR_MAX_DAYS', 30,
            )),
            'cutoff': cutoff.isoformat(),
            'candidate_count': len(items),
            'grant_count': sum(item['action'] == 'grant_reward' for item in items),
            'mark_completed_count': sum(item['action'] == 'mark_completed' for item in items),
            'truncated': truncated,
            'items': items,
        }, None

    @classmethod
    def repair(cls, *, days: object = None) -> Tuple[Optional[dict], Optional[str]]:
        preview, err = cls.preview(days=days)
        if err:
            return None, err

        repaired = []
        skipped = []
        errors = []
        for candidate in preview.get('items') or []:
            instance_id = int(candidate['task_instance_id'])
            instance = AdventurerS1TaskInstance.query.session.get(AdventurerS1TaskInstance, instance_id)
            if instance is None:
                skipped.append(instance_id)
                continue
            ok, reconcile_err = AdventurerS1TaskService.reconcile_stale_expedition_task(instance)
            if reconcile_err:
                errors.append({'task_instance_id': instance_id, 'message': reconcile_err})
            elif ok:
                repaired.append(instance_id)
            else:
                skipped.append(instance_id)

        return {
            'days': preview['days'],
            'candidate_count': preview['candidate_count'],
            'repaired_count': len(repaired),
            'skipped_count': len(skipped),
            'error_count': len(errors),
            'repaired_task_instance_ids': repaired,
            'skipped_task_instance_ids': skipped,
            'errors': errors,
        }, None
