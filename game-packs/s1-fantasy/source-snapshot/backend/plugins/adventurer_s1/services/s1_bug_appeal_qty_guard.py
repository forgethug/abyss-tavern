"""异常收益回收 vs 申诉漏补数量：互斥守卫（防回收后又补回 / 同一缺口双重处理）。"""
from __future__ import annotations

from plugins.adventurer_s1.models import (
    AdventurerS1BugQtyShortfallItem,
    AdventurerS1BugReclaimItem,
    AdventurerS1DataAppealAuditLog,
)


def _slot(adventurer_id: int, target_type: str, target_id: int) -> tuple[int, str, int]:
    return (int(adventurer_id), str(target_type), int(target_id))


def _appeal_slot(adventurer_id: int, appeal_id: int, target_type: str, target_id: int) -> tuple[int, int, str, int]:
    return (int(adventurer_id), int(appeal_id), str(target_type), int(target_id))


class S1BugAppealQtyGuard:
    @staticmethod
    def applied_quantity_by_slot(rows: list[tuple] | None = None) -> dict[tuple[int, str, int], int]:
        """任意申诉 audit 已写入的 quantity 上限（跨 appeal，用于漏补判定是否仍有缺口）。"""
        if rows is None:
            from db_repository import db_manager
            from plugins.adventurer_s1.models import AdventurerS1DataAppeal

            rows = (
                db_manager.query(AdventurerS1DataAppealAuditLog, AdventurerS1DataAppeal)
                .join(AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id)
                .filter(
                    AdventurerS1DataAppeal.status == 'approved',
                    AdventurerS1DataAppealAuditLog.reverted.is_(False),
                    AdventurerS1DataAppealAuditLog.op_type == 'update',
                    AdventurerS1DataAppealAuditLog.target_type.in_(('equipment', 'warehouse')),
                )
                .order_by(AdventurerS1DataAppealAuditLog.id.asc())
                .all()
            )

        applied: dict[tuple[int, str, int], int] = {}
        for audit, _appeal in rows:
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            if 'quantity' not in after:
                continue
            try:
                target_id = int(audit.target_id or after.get('id') or 0)
            except (TypeError, ValueError):
                continue
            if target_id <= 0:
                continue
            try:
                qty = max(0, int(after.get('quantity') if after.get('quantity') is not None else 0))
            except (TypeError, ValueError):
                continue
            key = _slot(int(audit.adventurer_id), str(audit.target_type), target_id)
            applied[key] = max(qty, applied.get(key, 0))
        return applied

    @staticmethod
    def executed_reclaim_slots() -> set[tuple[int, str, int]]:
        """已执行/已误报回收的 (冒险者, 目标类型, 目标 id)。"""
        from db_repository import db_manager

        rows = (
            db_manager.query(
                AdventurerS1BugReclaimItem.adventurer_id,
                AdventurerS1BugReclaimItem.audit_log_id,
                AdventurerS1BugReclaimItem.before_json,
            )
            .filter(AdventurerS1BugReclaimItem.status.in_(('executed', 'dismissed')))
            .all()
        )
        audit_ids = {int(r[1]) for r in rows if r[1]}
        audit_map: dict[int, AdventurerS1DataAppealAuditLog] = {}
        if audit_ids:
            audit_map = {
                int(a.id): a
                for a in db_manager.query(AdventurerS1DataAppealAuditLog)
                .filter(AdventurerS1DataAppealAuditLog.id.in_(audit_ids))
                .all()
            }

        slots: set[tuple[int, str, int]] = set()
        for adv_id, audit_log_id, before_json in rows:
            audit = audit_map.get(int(audit_log_id or 0))
            target_type = None
            target_id = None
            if audit:
                target_type = audit.target_type
                target_id = audit.target_id
            before = before_json if isinstance(before_json, dict) else {}
            if not target_type:
                target_type = before.get('target_type')
            if target_id is None:
                target_id = before.get('target_id') or before.get('id')
            try:
                tid = int(target_id or 0)
            except (TypeError, ValueError):
                continue
            if not target_type or tid <= 0:
                continue
            slots.add(_slot(int(adv_id), str(target_type), tid))
        return slots

    @staticmethod
    def executed_shortfall_by_appeal_slot() -> dict[tuple[int, int, str, int], int]:
        """已执行漏补：(冒险者, appeal, 目标类型, 目标 id) → 实发数量。"""
        from db_repository import db_manager

        rows = (
            db_manager.query(
                AdventurerS1BugQtyShortfallItem.adventurer_id,
                AdventurerS1BugQtyShortfallItem.appeal_id,
                AdventurerS1BugQtyShortfallItem.audit_log_id,
                AdventurerS1BugQtyShortfallItem.granted_quantity,
            )
            .filter(AdventurerS1BugQtyShortfallItem.status == 'executed')
            .all()
        )
        audit_ids = {int(r[2]) for r in rows if r[2]}
        audit_map: dict[int, AdventurerS1DataAppealAuditLog] = {}
        if audit_ids:
            audit_map = {
                int(a.id): a
                for a in db_manager.query(AdventurerS1DataAppealAuditLog)
                .filter(AdventurerS1DataAppealAuditLog.id.in_(audit_ids))
                .all()
            }

        out: dict[tuple[int, int, str, int], int] = {}
        for adv_id, appeal_id, audit_log_id, granted in rows:
            if not appeal_id:
                continue
            audit = audit_map.get(int(audit_log_id or 0))
            if not audit or not audit.target_type or not audit.target_id:
                continue
            key = _appeal_slot(int(adv_id), int(appeal_id), str(audit.target_type), int(audit.target_id))
            out[key] = out.get(key, 0) + max(0, int(granted or 0))
        return out

    @staticmethod
    def shortfall_blocked_reason(
        *,
        adventurer_id: int,
        appeal_id: int,
        target_type: str,
        target_id: int,
        intended_quantity: int | None,
        applied_by_slot: dict[tuple[int, str, int], int] | None = None,
        reclaim_slots: set[tuple[int, str, int]] | None = None,
    ) -> str | None:
        slot = _slot(adventurer_id, target_type, target_id)
        if reclaim_slots is None:
            reclaim_slots = S1BugAppealQtyGuard.executed_reclaim_slots()
        if slot in reclaim_slots:
            return '该物品堆已执行异常收益回收，不再漏补（避免回收后又补回）'

        if applied_by_slot is None:
            applied_by_slot = S1BugAppealQtyGuard.applied_quantity_by_slot()
        applied = applied_by_slot.get(slot)
        if intended_quantity is not None and applied is not None and int(applied) >= int(intended_quantity):
            return f'该堆已有申诉 audit 落库 quantity={applied}（跨申诉），漏补缺口已不存在'

        appeal_key = _appeal_slot(adventurer_id, appeal_id, target_type, target_id)
        granted = S1BugAppealQtyGuard.executed_shortfall_by_appeal_slot().get(appeal_key, 0)
        if granted > 0:
            return f'该申诉 #{appeal_id} 已在该堆执行过漏补 ×{granted}，跳过重复补发'
        return None

    @staticmethod
    def reclaim_blocked_reason(
        *,
        adventurer_id: int,
        appeal_id: int | None,
        target_type: str | None,
        target_id: int | None,
        illegal_quantity: int,
        executed_shortfalls: dict[tuple[int, int, str, int], int] | None = None,
    ) -> str | None:
        if illegal_quantity <= 0 or not appeal_id or not target_type or not target_id:
            return None
        appeal_key = _appeal_slot(adventurer_id, int(appeal_id), str(target_type), int(target_id))
        if executed_shortfalls is None:
            executed_shortfalls = S1BugAppealQtyGuard.executed_shortfall_by_appeal_slot()
        granted = executed_shortfalls.get(appeal_key, 0)
        if granted <= 0:
            return None
        if granted >= int(illegal_quantity):
            return (
                f'申诉 #{appeal_id} 已在该堆执行漏补 ×{granted}，'
                f'与本次非法增量 +{illegal_quantity} 重叠，不再回收（避免与漏补冲突）'
            )
        return None
