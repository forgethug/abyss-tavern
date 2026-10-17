"""Repair historical S1 equipment updates that overwrote immutable/public fields."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import json
from datetime import datetime

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1Settings,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)
from plugins.adventurer_s1.services.s1_existing_asset_update_contract import (
    preserve_s1_equipment_public_description,
)
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of


class S1ExpeditionEquipmentUpdateRepairService:
    """Use one expedition's frozen stable-ID snapshot for conditional recovery."""

    SNAPSHOT_OPERATION = 'expedition_equipment_update_repair'
    BACKUP_KEY = 'expedition_equipment_update_repair_backup'

    @staticmethod
    def _text(value: object) -> str:
        return str(value or '').strip()

    @staticmethod
    def _integer(value: object, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _hash(value: object) -> str:
        return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()

    @classmethod
    def _snapshot_state(cls, row: AdventurerS1Equipment) -> dict:
        return {
            'name': cls._text(row.name),
            'type': cls._text(row.type),
            'rarity': cls._text(row.rarity or 'common'),
            'custom_desc': cls._text(row.custom_desc),
            'uses_left': row.uses_left,
            'evolution_stage': cls._integer(row.evolution_stage),
            'quantity': item_quantity_of(row),
            'recovery_minutes': max(0, cls._integer(row.recovery_minutes)),
        }

    @classmethod
    def _expected_settled_state(cls, frozen: dict, update: dict) -> dict:
        return {
            'name': cls._text(update.get('name')),
            'type': cls._text(update.get('type')),
            'rarity': cls._text(update.get('rarity') or 'common'),
            'custom_desc': cls._text(update.get('desc')),
            'uses_left': update.get('uses_left'),
            'evolution_stage': cls._integer(update.get('evolution_stage')),
            'quantity': max(1, cls._integer(frozen.get('quantity'), 1)),
            'recovery_minutes': max(
                0, cls._integer(update.get('recovery_minutes')),
            ),
        }

    @classmethod
    def _repair_state(cls, frozen: dict, settled: dict) -> tuple[dict, list[str]]:
        repair = dict(settled)
        fields: list[str] = []
        frozen_type = cls._text(frozen.get('type'))
        frozen_stage = cls._integer(frozen.get('evolution_stage'))
        frozen_desc = cls._text(
            frozen.get('description') or frozen.get('custom_desc')
        )
        repaired_desc = preserve_s1_equipment_public_description(
            frozen_desc,
            settled.get('custom_desc'),
        )
        if settled.get('type') != frozen_type:
            repair['type'] = frozen_type
            fields.append('type')
        if settled.get('evolution_stage') != frozen_stage:
            repair['evolution_stage'] = frozen_stage
            fields.append('evolution_stage')
        if settled.get('custom_desc') != repaired_desc:
            repair['custom_desc'] = repaired_desc
            fields.append('custom_desc')
        return repair, fields

    @classmethod
    def _build_candidates(cls, expedition_log_id: int) -> tuple[list[dict], dict]:
        log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(
            id=expedition_log_id,
        ).first()
        if log is None:
            raise ValueError('探险日志不存在')
        if log.status != 'completed':
            raise ValueError('仅支持已完成的探险日志')
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        snapshots: dict[int, dict] = {}
        for snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            adventurer_id = cls._integer(snapshot.get('adventurer_id'))
            if adventurer_id > 0:
                snapshots[adventurer_id] = snapshot

        declarations: list[dict] = []
        target_ids: set[int] = set()
        scan = {
            'equipment_update_rows': 0,
            'immutable_field_issues': 0,
            'description_loss_issues': 0,
            'missing_frozen_asset': 0,
            'current_state_drifted': 0,
            'already_repaired': 0,
        }
        for member_update in full_log.get('member_updates') or []:
            if not isinstance(member_update, dict):
                continue
            adventurer_id = cls._integer(member_update.get('id'))
            frozen_member = snapshots.get(adventurer_id) or {}
            frozen_by_id = {
                cls._integer(row.get('asset_id')): row
                for row in frozen_member.get('equipment') or []
                if isinstance(row, dict) and cls._integer(row.get('asset_id')) > 0
            }
            for update in member_update.get('equipment_updates') or []:
                if not isinstance(update, dict):
                    continue
                scan['equipment_update_rows'] += 1
                equipment_id = cls._integer(update.get('equipment_id'))
                frozen = frozen_by_id.get(equipment_id)
                if frozen is None:
                    scan['missing_frozen_asset'] += 1
                    continue
                settled = cls._expected_settled_state(frozen, update)
                repair, repair_fields = cls._repair_state(frozen, settled)
                if not repair_fields:
                    continue
                if 'custom_desc' in repair_fields:
                    scan['description_loss_issues'] += 1
                if any(field in repair_fields for field in ('type', 'evolution_stage')):
                    scan['immutable_field_issues'] += 1
                declarations.append({
                    'expedition_log_id': int(log.id),
                    'adventurer_id': adventurer_id,
                    'adventurer_name': cls._text(frozen_member.get('adventurer_name')),
                    'equipment_id': equipment_id,
                    'equipment_name': settled['name'],
                    'expected_state': settled,
                    'repair_state': repair,
                    'repair_fields': repair_fields,
                })
                target_ids.add(equipment_id)

        rows = (
            db_manager.query(AdventurerS1Equipment)
            .filter(AdventurerS1Equipment.id.in_(sorted(target_ids)))
            .all()
            if target_ids else []
        )
        current_by_key = {
            (int(row.adventurer_id), int(row.id)): row
            for row in rows
        }
        candidates: list[dict] = []
        for item in declarations:
            row = current_by_key.get((item['adventurer_id'], item['equipment_id']))
            if row is None:
                scan['current_state_drifted'] += 1
                continue
            current = cls._snapshot_state(row)
            expected = item['expected_state']
            repair = item['repair_state']
            repair_fields = set(item['repair_fields'])
            safe = True
            for field in expected:
                allowed_values = [expected.get(field)]
                if field in repair_fields:
                    allowed_values.append(repair.get(field))
                if current.get(field) not in allowed_values:
                    safe = False
                    break
            if not safe:
                scan['current_state_drifted'] += 1
                continue
            if all(current.get(field) == repair.get(field) for field in repair_fields):
                scan['already_repaired'] += 1
                continue
            item['current_state'] = current
            candidates.append(item)
        return candidates, scan

    @classmethod
    def preview(cls, expedition_log_id: int, admin_user_id: int):
        try:
            expedition_log_id = int(expedition_log_id)
            candidates, scan = cls._build_candidates(expedition_log_id)
            snapshot = {
                'summary': {
                    'expedition_log_id': expedition_log_id,
                    'candidate_count': len(candidates),
                    'scan': scan,
                },
                'items': candidates,
            }
            token = S1AdminScanSnapshotService.store(
                cls.SNAPSHOT_OPERATION,
                admin_user_id,
                snapshot,
            )
            public_items = []
            for item in candidates:
                expected_desc = item['expected_state'].get('custom_desc') or ''
                repair_desc = item['repair_state'].get('custom_desc') or ''
                public_items.append({
                    'adventurer_id': item['adventurer_id'],
                    'adventurer_name': item['adventurer_name'],
                    'equipment_id': item['equipment_id'],
                    'equipment_name': item['equipment_name'],
                    'repair_fields': item['repair_fields'],
                    'before_description_length': len(expected_desc),
                    'before_description_hash': cls._hash(expected_desc),
                    'repair_description_length': len(repair_desc),
                    'repair_description_hash': cls._hash(repair_desc),
                    'before_type': item['expected_state'].get('type'),
                    'repair_type': item['repair_state'].get('type'),
                    'before_evolution_stage': item['expected_state'].get('evolution_stage'),
                    'repair_evolution_stage': item['repair_state'].get('evolution_stage'),
                })
            return {
                'summary': snapshot['summary'],
                'items': public_items,
                'preview_token': token,
                'execution_available': bool(token),
            }, None
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('探险装备更新污染修复 preview 失败')
            return None, f'预览失败：{exc}'

    @classmethod
    def execute(cls, admin_user_id: int, preview_token: str):
        snapshot = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION,
            admin_user_id,
            preview_token,
        )
        if snapshot is None:
            return None, '预览快照已过期，请重新预览后再执行'
        items = snapshot.get('items') or []
        if not items:
            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
            )
            return {'repaired_count': 0, 'skipped_state_drifted': 0}, None
        target_ids = sorted({int(item['equipment_id']) for item in items})
        backup = {
            'created_at': _portable_utc_now().isoformat() + 'Z',
            'expedition_log_id': snapshot.get('summary', {}).get('expedition_log_id'),
            'entries': [],
        }
        repaired = 0
        skipped = 0
        try:
            with db_manager.transaction():
                rows = (
                    db_manager.query(AdventurerS1Equipment)
                    .filter(AdventurerS1Equipment.id.in_(target_ids))
                    .order_by(AdventurerS1Equipment.id.asc())
                    .with_for_update()
                    .all()
                )
                row_by_key = {
                    (int(row.adventurer_id), int(row.id)): row
                    for row in rows
                }
                for item in items:
                    row = row_by_key.get((
                        int(item['adventurer_id']), int(item['equipment_id']),
                    ))
                    current = cls._snapshot_state(row) if row is not None else None
                    if current != item.get('current_state'):
                        skipped += 1
                        continue
                    repair_state = item['repair_state']
                    backup['entries'].append({
                        'adventurer_id': int(row.adventurer_id),
                        'equipment_id': int(row.id),
                        'before_state': current,
                        'repaired_state': repair_state,
                    })
                    for field in item.get('repair_fields') or []:
                        if field == 'custom_desc':
                            row.custom_desc = repair_state[field] or None
                        else:
                            setattr(row, field, repair_state[field])
                    repaired += 1
                if backup['entries']:
                    setting = db_manager.query(AdventurerS1Settings).filter_by(
                        key=cls.BACKUP_KEY,
                    ).with_for_update().first()
                    payload = json.dumps(backup, ensure_ascii=False)
                    if setting is None:
                        db_manager.add(AdventurerS1Settings(
                            key=cls.BACKUP_KEY,
                            value=payload,
                            description='探险装备更新污染修复前备份（供条件回滚）',
                            updated_by_user_id=admin_user_id,
                        ))
                    else:
                        setting.value = payload
                        setting.updated_by_user_id = admin_user_id
            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
            )
            return {
                'repaired_count': repaired,
                'skipped_state_drifted': skipped,
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('探险装备更新污染修复 execute 失败')
            return None, f'修复失败：{exc}'

    @classmethod
    def restore(cls):
        setting = db_manager.query(AdventurerS1Settings).filter_by(
            key=cls.BACKUP_KEY,
        ).first()
        if setting is None or not setting.value:
            return None, '没有可用的探险装备更新修复备份'
        try:
            backup = json.loads(setting.value)
        except (TypeError, json.JSONDecodeError):
            return None, '备份数据已损坏，无法回滚'
        entries = backup.get('entries') if isinstance(backup, dict) else None
        if not isinstance(entries, list) or not entries:
            return None, '备份为空，无法回滚'
        target_ids = sorted({
            cls._integer(row.get('equipment_id'))
            for row in entries
            if cls._integer(row.get('equipment_id')) > 0
        })
        if not target_ids:
            return None, '备份中没有有效装备 ID'
        restored = 0
        skipped = 0
        try:
            with db_manager.transaction():
                rows = (
                    db_manager.query(AdventurerS1Equipment)
                    .filter(AdventurerS1Equipment.id.in_(target_ids))
                    .order_by(AdventurerS1Equipment.id.asc())
                    .with_for_update()
                    .all()
                )
                locked_setting = db_manager.query(AdventurerS1Settings).filter_by(
                    key=cls.BACKUP_KEY,
                ).with_for_update().first()
                if locked_setting is None or locked_setting.value != setting.value:
                    raise ValueError('修复备份已变化，请重新发起回滚')
                row_by_key = {
                    (int(row.adventurer_id), int(row.id)): row
                    for row in rows
                }
                for entry in entries:
                    row = row_by_key.get((
                        cls._integer(entry.get('adventurer_id')),
                        cls._integer(entry.get('equipment_id')),
                    ))
                    if (
                        row is None
                        or cls._snapshot_state(row) != entry.get('repaired_state')
                    ):
                        skipped += 1
                        continue
                    before = entry.get('before_state') or {}
                    row.type = before.get('type')
                    row.custom_desc = before.get('custom_desc') or None
                    row.evolution_stage = cls._integer(before.get('evolution_stage'))
                    restored += 1
            return {
                'restored_count': restored,
                'skipped_state_drifted': skipped,
            }, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('探险装备更新污染修复 restore 失败')
            return None, f'回滚失败：{exc}'
