"""精确追回旧版探险 ``consumed_equipments`` 堆叠漏扣。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from collections import defaultdict
from datetime import datetime, timedelta

from db_repository import db_manager

from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionConsumptionRepair,
    AdventurerS1ExpeditionLog,
    AdventurerS1ItemLog,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
from plugins.adventurer_s1.services.s1_item_quantity_service import (
    coerce_item_quantity,
    item_quantity_of,
    split_stack_suffix,
)


class S1ExpeditionConsumptionRepairService:
    """只处理能由冻结快照和当前实例机械证明的旧版整组漏扣。"""

    # 0843a6cf：legacy consumed 从删除整组改成堆叠仅扣 1 的提交时间（UTC）。
    AFFECTED_FROM = datetime(2026, 6, 17, 16, 7, 56)
    SNAPSHOT_KIND = 'expedition_consumption_shortfalls'
    SOURCE_TYPE = 'expedition_consumption_repair'
    RESTORE_SOURCE_TYPE = 'expedition_consumption_restore'
    SETTLEMENT_GRACE = timedelta(seconds=15)
    MAX_SCAN_LOGS = 5000

    @staticmethod
    def _member_updates(full_log: object) -> list[dict]:
        if not isinstance(full_log, dict):
            return []
        rows = full_log.get('member_updates')
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    @staticmethod
    def _member_snapshots(full_log: object) -> dict[int, dict]:
        if not isinstance(full_log, dict):
            return {}
        result = {}
        for row in full_log.get('audit_member_snapshot') or []:
            if not isinstance(row, dict):
                continue
            try:
                result[int(row.get('adventurer_id'))] = row
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _legacy_consumed_names(update: dict) -> set[str]:
        raw = update.get('consumed_equipments')
        rows = raw if isinstance(raw, list) else [raw]
        result = set()
        for value in rows[:100]:
            if not isinstance(value, str):
                continue
            name = value.strip()
            if not name or name[0] in ('{', '['):
                continue
            base_name, quantity = split_stack_suffix(name, 1)
            if base_name == name and quantity == 1:
                result.add(name)
        return result

    @staticmethod
    def _new_equipment_names(update: dict) -> set[str]:
        raw = update.get('new_equipments')
        if not isinstance(raw, dict):
            return set()
        result = set()
        for value in raw:
            base_name, _quantity = split_stack_suffix(str(value or '').strip(), 1)
            if base_name:
                result.add(base_name)
        return result

    @classmethod
    def _declarations(cls, *, expedition_log_id=None, adventurer_id=None):
        query = AdventurerS1ExpeditionLog.query.filter(
            AdventurerS1ExpeditionLog.status == 'completed',
            AdventurerS1ExpeditionLog.created_at >= cls.AFFECTED_FROM,
        )
        if expedition_log_id:
            query = query.filter(AdventurerS1ExpeditionLog.id == expedition_log_id)
        query = query.order_by(AdventurerS1ExpeditionLog.id.desc())
        logs = query.limit(cls.MAX_SCAN_LOGS + 1).all()
        truncated = len(logs) > cls.MAX_SCAN_LOGS
        logs = logs[:cls.MAX_SCAN_LOGS]

        declarations = []
        mechanically_ambiguous = 0
        for log in logs:
            snapshots = cls._member_snapshots(log.full_log)
            for update in cls._member_updates(log.full_log):
                try:
                    member_id = int(update.get('id'))
                except (TypeError, ValueError):
                    continue
                if adventurer_id and member_id != adventurer_id:
                    continue
                snapshot = snapshots.get(member_id) or {}
                equipment_by_name = defaultdict(list)
                for item in snapshot.get('equipment') or []:
                    if isinstance(item, dict) and str(item.get('name') or '').strip():
                        equipment_by_name[str(item['name']).strip()].append(item)
                newly_gained = cls._new_equipment_names(update)
                for name in cls._legacy_consumed_names(update):
                    entries = equipment_by_name.get(name) or []
                    if len(entries) != 1 or name in newly_gained:
                        mechanically_ambiguous += 1
                        continue
                    before_quantity = coerce_item_quantity(entries[0].get('quantity'), 1)
                    if before_quantity <= 1:
                        continue
                    declarations.append({
                        'expedition_log_id': int(log.id),
                        'expedition_created_at': log.created_at,
                        'settled_at': log.updated_at or log.created_at,
                        'adventurer_id': member_id,
                        'adventurer_name': str(snapshot.get('adventurer_name') or f'#{member_id}'),
                        'item_name': name,
                        'snapshot_quantity': before_quantity,
                        'buggy_quantity': before_quantity - 1,
                    })
        return declarations, len(logs), truncated, mechanically_ambiguous

    @classmethod
    def _candidates(cls, *, expedition_log_id=None, adventurer_id=None):
        declarations, scanned_logs, truncated, ambiguous = cls._declarations(
            expedition_log_id=expedition_log_id,
            adventurer_id=adventurer_id,
        )
        if not declarations:
            return [], scanned_logs, truncated, ambiguous

        # 同一物品多次被旧 bug 留存时，只可能由最后一次冻结快照与当前数量精确闭合。
        latest = {}
        for row in declarations:
            key = (row['adventurer_id'], row['item_name'])
            if key not in latest or row['expedition_log_id'] > latest[key]['expedition_log_id']:
                latest[key] = row

        adv_ids = sorted({key[0] for key in latest})
        names = sorted({key[1] for key in latest})
        equipment_by_key = defaultdict(list)
        for item in AdventurerS1Equipment.query.filter(
            AdventurerS1Equipment.adventurer_id.in_(adv_ids),
            AdventurerS1Equipment.name.in_(names),
        ).all():
            equipment_by_key[(int(item.adventurer_id), str(item.name))].append(item)

        active_repairs = {
            (int(row.expedition_log_id), int(row.adventurer_id), str(row.item_name))
            for row in AdventurerS1ExpeditionConsumptionRepair.query.filter(
                AdventurerS1ExpeditionConsumptionRepair.restored_at.is_(None),
                AdventurerS1ExpeditionConsumptionRepair.adventurer_id.in_(adv_ids),
            ).all()
        }
        earliest_settlement = min(row['settled_at'] for row in latest.values())
        later_activity = defaultdict(list)
        for log in AdventurerS1ItemLog.query.filter(
            AdventurerS1ItemLog.adventurer_id.in_(adv_ids),
            AdventurerS1ItemLog.item_name.in_(names),
            AdventurerS1ItemLog.created_at >= earliest_settlement,
            AdventurerS1ItemLog.source_type.notin_((cls.SOURCE_TYPE, cls.RESTORE_SOURCE_TYPE)),
        ).all():
            later_activity[(int(log.adventurer_id), str(log.item_name))].append(log)

        items = []
        for key, declaration in latest.items():
            repair_key = (
                declaration['expedition_log_id'], declaration['adventurer_id'], declaration['item_name'],
            )
            if repair_key in active_repairs:
                continue
            rows = equipment_by_key.get(key) or []
            if len(rows) != 1:
                ambiguous += 1
                continue
            item = rows[0]
            if item_quantity_of(item) != declaration['buggy_quantity']:
                ambiguous += 1
                continue
            cutoff = declaration['settled_at'] + cls.SETTLEMENT_GRACE
            if item.created_at and item.created_at > cutoff:
                ambiguous += 1
                continue
            if any(
                activity.created_at > declaration['settled_at']
                and not (
                    activity.source_type == 'expedition'
                    and activity.created_at <= cutoff
                )
                for activity in later_activity.get(key, [])
            ):
                ambiguous += 1
                continue
            items.append({
                **{k: v for k, v in declaration.items() if k not in ('expedition_created_at', 'settled_at')},
                'expedition_created_at': declaration['expedition_created_at'].isoformat(),
                'settled_at': declaration['settled_at'].isoformat(),
                'equipment_id': int(item.id),
                'equipment_updated_at': item.updated_at.isoformat() if item.updated_at else None,
                'current_quantity': item_quantity_of(item),
                'reclaim_quantity': item_quantity_of(item),
                'item_type': item.type or '',
                'item_rarity': item.rarity or 'common',
                'custom_desc': item.custom_desc or '',
                'uses_left': item.uses_left,
                'evolution_stage': int(item.evolution_stage or 0),
            })
        items.sort(key=lambda row: (row['expedition_log_id'], row['adventurer_id'], row['item_name']))
        return items, scanned_logs, truncated, ambiguous

    @classmethod
    def preview(cls, admin_user_id: int, *, expedition_log_id=None, adventurer_id=None):
        items, scanned_logs, truncated, ambiguous = cls._candidates(
            expedition_log_id=expedition_log_id,
            adventurer_id=adventurer_id,
        )
        result = {
            'affected_from': cls.AFFECTED_FROM.isoformat() + 'Z',
            'scanned_logs': scanned_logs,
            'scan_truncated': truncated,
            'candidate_count': len(items),
            'reclaim_quantity': sum(item['reclaim_quantity'] for item in items),
            'ambiguous_count': ambiguous,
            'items': items,
        }
        result['snapshot_token'] = S1AdminScanSnapshotService.store(
            cls.SNAPSHOT_KIND, admin_user_id, result,
        )
        return result, None

    @classmethod
    def execute(cls, admin_user_id: int, preview_token: str):
        preview = S1AdminScanSnapshotService.load(cls.SNAPSHOT_KIND, admin_user_id, preview_token)
        if preview is None:
            return None, '预览快照已过期，请重新预览后再执行'
        repaired = []
        skipped = 0
        try:
            with db_manager.transaction():
                for candidate in preview.get('items') or []:
                    adv_id = int(candidate['adventurer_id'])
                    adv = AdventurerS1.query.filter_by(id=adv_id).with_for_update().first()
                    if adv is None:
                        skipped += 1
                        continue
                    item = AdventurerS1Equipment.query.filter_by(
                        id=int(candidate['equipment_id']),
                        adventurer_id=adv_id,
                        name=str(candidate['item_name']),
                    ).with_for_update().first()
                    current_updated_at = item.updated_at.isoformat() if item and item.updated_at else None
                    if (
                        item is None
                        or item_quantity_of(item) != int(candidate['current_quantity'])
                        or current_updated_at != candidate.get('equipment_updated_at')
                    ):
                        skipped += 1
                        continue
                    ledger = AdventurerS1ExpeditionConsumptionRepair.query.filter_by(
                        expedition_log_id=int(candidate['expedition_log_id']),
                        adventurer_id=adv_id,
                        item_name=str(candidate['item_name']),
                    ).with_for_update().first()
                    if ledger is not None and ledger.restored_at is None:
                        skipped += 1
                        continue
                    item_data = {
                        'type': item.type or '',
                        'rarity': item.rarity or 'common',
                        'quantity': item_quantity_of(item),
                        'uses_left': item.uses_left,
                        'desc': item.custom_desc or '',
                        'evolution_stage': int(item.evolution_stage or 0),
                    }
                    if ledger is None:
                        ledger = AdventurerS1ExpeditionConsumptionRepair(
                            expedition_log_id=int(candidate['expedition_log_id']),
                            adventurer_id=adv_id,
                            item_name=str(candidate['item_name']),
                            equipment_id=int(item.id),
                            reclaimed_quantity=item_quantity_of(item),
                            item_snapshot=item_data,
                            admin_user_id=admin_user_id,
                        )
                        db_manager.add(ledger)
                    else:
                        ledger.equipment_id = int(item.id)
                        ledger.reclaimed_quantity = item_quantity_of(item)
                        ledger.item_snapshot = item_data
                        ledger.admin_user_id = admin_user_id
                        ledger.restored_at = None
                    db_manager.flush()
                    db_manager.delete_instance(item)
                    db_manager.add(AdventurerS1ItemLog(
                        adventurer_id=adv_id,
                        item_name=ledger.item_name,
                        item_type=item_data['type'],
                        item_rarity=item_data['rarity'],
                        change_type='consume',
                        quantity=ledger.reclaimed_quantity,
                        source_type=cls.SOURCE_TYPE,
                        related_id=str(ledger.id),
                        related_type='expedition_consumption_repair',
                        description=f'追回探险日志 #{ledger.expedition_log_id} 漏扣的「{ledger.item_name}」',
                    ))
                    repaired.append({**candidate, 'repair_id': int(ledger.id)})
        except Exception as exc:  # noqa: BLE001
            return None, f'历史探险漏扣追回失败，已整批回滚：{exc}'
        S1AdminScanSnapshotService.delete(cls.SNAPSHOT_KIND, admin_user_id, preview_token)
        return {
            'repaired_count': len(repaired),
            'reclaimed_quantity': sum(row['reclaim_quantity'] for row in repaired),
            'skipped': skipped,
            'items': repaired,
        }, None

    @classmethod
    def restore(cls, admin_user_id: int):
        ledgers = AdventurerS1ExpeditionConsumptionRepair.query.filter(
            AdventurerS1ExpeditionConsumptionRepair.restored_at.is_(None),
        ).order_by(AdventurerS1ExpeditionConsumptionRepair.id.asc()).all()
        if not ledgers:
            return {'restored_count': 0, 'restored_quantity': 0, 'items': []}, None
        restored = []
        try:
            with db_manager.transaction():
                for ledger in ledgers:
                    adv = AdventurerS1.query.filter_by(id=ledger.adventurer_id).with_for_update().first()
                    if adv is None:
                        raise RuntimeError(f'冒险者 #{ledger.adventurer_id} 不存在')
                    item_data = dict(ledger.item_snapshot or {})
                    item_data['quantity'] = int(ledger.reclaimed_quantity or 1)
                    _destination, grant_err = grant_raw_item(
                        adv,
                        ledger.item_name,
                        item_data,
                        source_type=cls.RESTORE_SOURCE_TYPE,
                        description=f'撤销日志 #{ledger.expedition_log_id} 的历史漏扣追回',
                        force_destination='equipment',
                        register_catalog=False,
                        related_id=str(ledger.id),
                        related_type='expedition_consumption_repair',
                        allow_same_name_stack_merge=True,
                    )
                    if grant_err:
                        raise RuntimeError(grant_err)
                    ledger.restored_at = _portable_utc_now()
                    ledger.restored_by_user_id = admin_user_id
                    restored.append({
                        'repair_id': int(ledger.id),
                        'expedition_log_id': int(ledger.expedition_log_id),
                        'adventurer_id': int(ledger.adventurer_id),
                        'item_name': ledger.item_name,
                        'quantity': int(ledger.reclaimed_quantity),
                    })
        except Exception as exc:  # noqa: BLE001
            return None, f'撤销历史漏扣追回失败，已整批回滚：{exc}'
        return {
            'restored_count': len(restored),
            'restored_quantity': sum(row['quantity'] for row in restored),
            'items': restored,
        }, None
