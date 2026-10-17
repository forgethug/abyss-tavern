"""特殊副本未扣门票扫描与金币追缴。"""

from collections import Counter
from datetime import timedelta

from flask import current_app
from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1ItemLog,
    AdventurerS1SpecialDungeonTicketPenalty,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import S1AdminScanSnapshotService


class S1SpecialDungeonTicketPenaltyService:
    RULES = {
        '转职试炼场': {'ticket_item': '转职试炼凭证', 'penalty_config': 'ADVENTURER_S1_SPECIAL_TICKET_PENALTY_CLASS_GOLD'},
        '顿悟秘境': {'ticket_item': '顿悟之钥', 'penalty_config': 'ADVENTURER_S1_SPECIAL_TICKET_PENALTY_SKILL_GOLD'},
        '祛厄药庐': {'ticket_item': '祛厄引路符', 'penalty_config': 'ADVENTURER_S1_SPECIAL_TICKET_PENALTY_TRAIT_GOLD'},
    }
    MATCH_SECONDS = 5
    SOURCE_TYPE = 'special_ticket_penalty'

    @staticmethod
    def _penalty_gold(rule):
        from plugins.adventurer_s1.plugin import AdventurerS1Plugin
        key = rule['penalty_config']
        return float(current_app.config.get(key, AdventurerS1Plugin.default_config[key]) or 0)

    @staticmethod
    def _candidates():
        dungeon_names = list(S1SpecialDungeonTicketPenaltyService.RULES)
        logs = (
            AdventurerS1ExpeditionLog.query
            .filter(AdventurerS1ExpeditionLog.target_dungeon.in_(dungeon_names))
            .order_by(AdventurerS1ExpeditionLog.created_at.asc(), AdventurerS1ExpeditionLog.id.asc())
            .all()
        )
        if not logs:
            return []

        log_by_id = {row.id: row for row in logs}
        log_ids = list(log_by_id)
        members = (
            AdventurerS1ExpeditionLogMember.query
            .filter(AdventurerS1ExpeditionLogMember.expedition_log_id.in_(log_ids))
            .all()
        )
        adv_ids = sorted({row.adventurer_id for row in members})
        adventurers = {
            row.id: row for row in AdventurerS1.query.filter(AdventurerS1.id.in_(adv_ids)).all()
        }
        existing = {
            (row.expedition_log_id, row.adventurer_id)
            for row in AdventurerS1SpecialDungeonTicketPenalty.query
            .filter(AdventurerS1SpecialDungeonTicketPenalty.expedition_log_id.in_(log_ids))
            .all()
        }

        start_at = min(row.created_at for row in logs) - timedelta(seconds=S1SpecialDungeonTicketPenaltyService.MATCH_SECONDS)
        end_at = max(row.created_at for row in logs) + timedelta(seconds=S1SpecialDungeonTicketPenaltyService.MATCH_SECONDS)
        consume_logs = (
            AdventurerS1ItemLog.query
            .filter(
                AdventurerS1ItemLog.adventurer_id.in_(adv_ids),
                AdventurerS1ItemLog.source_type == 'special_dungeon_entry',
                AdventurerS1ItemLog.change_type == 'consume',
                AdventurerS1ItemLog.created_at.between(start_at, end_at),
            )
            .all()
        )
        consumed = {}
        for row in consume_logs:
            consumed.setdefault((row.adventurer_id, row.item_name), []).append(row.created_at)

        result = []
        for member in members:
            log = log_by_id.get(member.expedition_log_id)
            adv = adventurers.get(member.adventurer_id)
            if not log or not adv or (log.id, adv.id) in existing:
                continue
            rule = S1SpecialDungeonTicketPenaltyService.RULES[log.target_dungeon]
            penalty_gold = S1SpecialDungeonTicketPenaltyService._penalty_gold(rule)
            times = consumed.get((adv.id, rule['ticket_item']), [])
            matched = any(
                abs((created_at - log.created_at).total_seconds()) <= S1SpecialDungeonTicketPenaltyService.MATCH_SECONDS
                for created_at in times
            )
            if matched:
                continue
            result.append({
                'expedition_log_id': log.id,
                'expedition_created_at': log.created_at.isoformat(),
                'dungeon_name': log.target_dungeon,
                'ticket_item': rule['ticket_item'],
                'penalty_gold': penalty_gold,
                'adventurer_id': adv.id,
                'adventurer_name': adv.name,
                'owner_id': adv.owner_id,
                'balance_before': round(float(adv.gold or 0), 2),
            })
        return sorted(result, key=lambda row: (row['expedition_log_id'], row['adventurer_id']))

    @staticmethod
    def preview(admin_user_id: int | None = None):
        items = S1SpecialDungeonTicketPenaltyService._candidates()
        by_dungeon = Counter(row['dungeon_name'] for row in items)
        result = {
            'candidate_count': len(items),
            'total_penalty_gold': round(sum(row['penalty_gold'] for row in items), 2),
            'by_dungeon': dict(by_dungeon),
            'rules': {
                name: {
                    'ticket_item': rule['ticket_item'],
                    'penalty_gold': S1SpecialDungeonTicketPenaltyService._penalty_gold(rule),
                }
                for name, rule in S1SpecialDungeonTicketPenaltyService.RULES.items()
            },
            'items': items,
        }
        if admin_user_id is not None:
            result['snapshot_token'] = S1AdminScanSnapshotService.store(
                'special_dungeon_ticket_penalties', admin_user_id, result,
            )
        return result, None

    @staticmethod
    def execute(admin_user_id: int, preview_token: str = ''):
        preview = None
        if preview_token:
            preview = S1AdminScanSnapshotService.load(
                'special_dungeon_ticket_penalties', admin_user_id, preview_token,
            )
            if preview is None:
                return None, '预览快照已过期，请重新预览后再执行'
        if preview is None:
            preview, err = S1SpecialDungeonTicketPenaltyService.preview()
            if err:
                return None, err
        items = preview['items']
        if not items:
            S1AdminScanSnapshotService.delete(
                'special_dungeon_ticket_penalties', admin_user_id, preview_token,
            )
            return {'executed_count': 0, 'total_penalty_gold': 0, 'items': []}, None

        results = []
        try:
            with db_manager.transaction():
                log_ids = {int(item['expedition_log_id']) for item in items}
                adventurer_ids = {int(item['adventurer_id']) for item in items}
                existing = {
                    (int(row.expedition_log_id), int(row.adventurer_id))
                    for row in db_manager.query(AdventurerS1SpecialDungeonTicketPenalty).filter(
                        AdventurerS1SpecialDungeonTicketPenalty.expedition_log_id.in_(log_ids),
                        AdventurerS1SpecialDungeonTicketPenalty.adventurer_id.in_(adventurer_ids),
                    ).all()
                }
                log_created_at = {
                    int(row.id): row.created_at
                    for row in db_manager.query(AdventurerS1ExpeditionLog).filter(
                        AdventurerS1ExpeditionLog.id.in_(log_ids),
                    ).all()
                }
                ticket_items = {str(item['ticket_item']) for item in items}
                consumed = {}
                if log_created_at:
                    match_seconds = S1SpecialDungeonTicketPenaltyService.MATCH_SECONDS
                    start_at = min(log_created_at.values()) - timedelta(seconds=match_seconds)
                    end_at = max(log_created_at.values()) + timedelta(seconds=match_seconds)
                    for row in db_manager.query(AdventurerS1ItemLog).filter(
                        AdventurerS1ItemLog.adventurer_id.in_(adventurer_ids),
                        AdventurerS1ItemLog.item_name.in_(ticket_items),
                        AdventurerS1ItemLog.source_type == 'special_dungeon_entry',
                        AdventurerS1ItemLog.change_type == 'consume',
                        AdventurerS1ItemLog.created_at.between(start_at, end_at),
                    ).all():
                        consume_key = (int(row.adventurer_id), str(row.item_name))
                        consumed.setdefault(consume_key, []).append(row.created_at)
                for item in items:
                    key = (int(item['expedition_log_id']), int(item['adventurer_id']))
                    if key in existing:
                        continue
                    expedition_at = log_created_at.get(key[0])
                    if expedition_at is None:
                        continue
                    if any(
                        abs((created_at - expedition_at).total_seconds())
                        <= S1SpecialDungeonTicketPenaltyService.MATCH_SECONDS
                        for created_at in consumed.get((key[1], str(item['ticket_item'])), [])
                    ):
                        continue
                    penalty = AdventurerS1SpecialDungeonTicketPenalty(
                        expedition_log_id=item['expedition_log_id'],
                        adventurer_id=item['adventurer_id'],
                        dungeon_name=item['dungeon_name'],
                        ticket_item=item['ticket_item'],
                        penalty_gold=item['penalty_gold'],
                        admin_user_id=admin_user_id,
                    )
                    db_manager.add(penalty)
                    db_manager.flush()
                    balance, deduct_err = S1CurrencyService.deduct(
                        item['adventurer_id'], 'gold', item['penalty_gold'],
                        source_type=S1SpecialDungeonTicketPenaltyService.SOURCE_TYPE,
                        related_id=item['expedition_log_id'],
                        related_type='special_dungeon_expedition',
                        description=f'未消耗「{item["ticket_item"]}」进入【{item["dungeon_name"]}】追缴',
                        auto_commit=False,
                        allow_negative=True,
                    )
                    if deduct_err:
                        raise RuntimeError(deduct_err)
                    results.append({**item, 'balance_after': round(float(balance), 2)})
                    existing.add(key)
        except Exception as exc:
            return None, f'特殊副本门票追缴失败，已整批回滚：{exc}'

        S1AdminScanSnapshotService.delete(
            'special_dungeon_ticket_penalties', admin_user_id, preview_token,
        )
        return {
            'executed_count': len(results),
            'total_penalty_gold': round(sum(row['penalty_gold'] for row in results), 2),
            'items': results,
        }, None
