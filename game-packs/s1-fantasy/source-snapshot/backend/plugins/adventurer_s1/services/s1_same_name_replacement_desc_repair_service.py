"""Repair descriptions lost by same-name expedition replacements."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime, timedelta

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1Settings,
    AdventurerS1Skill,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import (
    S1AdminScanSnapshotService,
)


class S1SameNameReplacementDescRepairService:
    """Mechanically restore frozen descriptions lost by same-name settlement writes."""

    SNAPSHOT_OPERATION = 'same_name_replacement_desc_repair'
    BACKUP_KEY = 'same_name_replacement_desc_repair_backup'
    SETTLEMENT_GRACE = timedelta(seconds=30)
    PLAYER_HISTORY_LIMIT = 50

    @staticmethod
    def _text(value) -> str:
        return str(value or '').strip()

    @classmethod
    def _consumed_names(cls, update: dict) -> set[str]:
        raw = update.get('consumed_equipments')
        rows = raw if isinstance(raw, list) else [raw]
        names = set()
        for row in rows[:100]:
            if isinstance(row, str):
                name = cls._text(row)
            elif isinstance(row, dict):
                name = cls._text(row.get('name'))
            else:
                name = ''
            if name:
                names.add(name)
        return names

    @classmethod
    def _new_skill_entries(cls, raw) -> list[dict]:
        rows = raw if isinstance(raw, list) else []
        entries = []
        for row in rows[:100]:
            if isinstance(row, str):
                name = cls._text(row)
                desc = ''
            elif isinstance(row, dict):
                name = cls._text(row.get('name'))
                desc = cls._text(row.get('description') or row.get('desc'))
            else:
                continue
            if name:
                entries.append({'name': name, 'description': desc})
        return entries

    @staticmethod
    def _target_model(target_type: str):
        if target_type == 'equipment':
            return AdventurerS1Equipment
        if target_type == 'warehouse':
            return AdventurerS1WarehouseItem
        if target_type == 'skill':
            return AdventurerS1Skill
        return None

    @staticmethod
    def _value_field(target_type: str) -> str:
        return 'description' if target_type == 'skill' else 'custom_desc'

    @classmethod
    def _declarations(cls, log: AdventurerS1ExpeditionLog) -> tuple[list[dict], int]:
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        snapshots = {}
        for snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            try:
                snapshots[int(snapshot.get('adventurer_id'))] = snapshot
            except (TypeError, ValueError):
                continue

        declarations = []
        ambiguous = 0
        for update in full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            try:
                adventurer_id = int(update.get('id'))
            except (TypeError, ValueError):
                continue
            new_items = update.get('new_equipments')
            if not isinstance(new_items, dict):
                continue
            consumed_names = cls._consumed_names(update)
            snapshot = snapshots.get(adventurer_id) or {}
            equipment_by_name = {}
            for item in snapshot.get('equipment') or []:
                if not isinstance(item, dict):
                    continue
                name = cls._text(item.get('name'))
                if name:
                    equipment_by_name.setdefault(name, []).append(item)

            for name, metadata in new_items.items():
                name = cls._text(name)
                if not name or name not in consumed_names:
                    continue
                metadata = metadata if isinstance(metadata, dict) else {}
                old_rows = equipment_by_name.get(name) or []
                old_descriptions = {
                    cls._text(row.get('description') or row.get('desc'))
                    for row in old_rows
                    if cls._text(row.get('description') or row.get('desc'))
                }
                if len(old_rows) != 1 or len(old_descriptions) != 1:
                    ambiguous += 1
                    continue
                declarations.append({
                    'resource_type': 'item',
                    'expedition_log_id': int(log.id),
                    'adventurer_id': adventurer_id,
                    'adventurer_name': cls._text(snapshot.get('adventurer_name')),
                    'item_name': name,
                    'frozen_description': next(iter(old_descriptions)),
                    'settled_description': cls._text(
                        metadata.get('desc') or metadata.get('description')
                    ),
                    'settled_at': log.updated_at or log.created_at,
                })

        for update in full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            try:
                adventurer_id = int(update.get('id'))
            except (TypeError, ValueError):
                continue
            snapshot = snapshots.get(adventurer_id) or {}
            raw_removed_skills = update.get('removed_skills')
            removed_skill_rows = (
                raw_removed_skills if isinstance(raw_removed_skills, list)
                else [raw_removed_skills]
            )
            removed_skills = {
                cls._text(row.get('name')) if isinstance(row, dict) else cls._text(row)
                for row in removed_skill_rows
                if row is not None
            }
            skills_by_name: dict[str, list[dict]] = {}
            for skill in snapshot.get('skills') or []:
                if not isinstance(skill, dict):
                    continue
                name = cls._text(skill.get('name'))
                if name:
                    skills_by_name.setdefault(name, []).append(skill)
            for skill in cls._new_skill_entries(update.get('new_skills')):
                name = skill['name']
                if name in removed_skills:
                    continue
                old_rows = skills_by_name.get(name) or []
                old_descriptions = {
                    cls._text(row.get('description') or row.get('desc'))
                    for row in old_rows
                    if cls._text(row.get('description') or row.get('desc'))
                }
                if len(old_rows) != 1 or len(old_descriptions) != 1:
                    if old_rows:
                        ambiguous += 1
                    continue
                declarations.append({
                    'resource_type': 'skill',
                    'expedition_log_id': int(log.id),
                    'adventurer_id': adventurer_id,
                    'adventurer_name': cls._text(snapshot.get('adventurer_name')),
                    'item_name': name,
                    'frozen_description': next(iter(old_descriptions)),
                    'settled_description': skill['description'],
                    'settled_at': log.updated_at or log.created_at,
                })
        return declarations, ambiguous

    @classmethod
    def _resolve_declarations(
        cls,
        declarations: list[dict],
        ambiguous: int,
    ) -> tuple[list[dict], int]:
        if not declarations:
            return [], ambiguous

        adv_ids = sorted({row['adventurer_id'] for row in declarations})
        names = sorted({row['item_name'] for row in declarations})
        rows_by_key = {}
        for target_type, model in (
            ('equipment', AdventurerS1Equipment),
            ('warehouse', AdventurerS1WarehouseItem),
        ):
            rows = db_manager.query(model).filter(
                model.adventurer_id.in_(adv_ids), model.name.in_(names),
            ).all()
            for row in rows:
                rows_by_key.setdefault((int(row.adventurer_id), str(row.name)), []).append(
                    (target_type, row)
                )
        skill_rows_by_key = {}
        for row in db_manager.query(AdventurerS1Skill).filter(
            AdventurerS1Skill.adventurer_id.in_(adv_ids),
            AdventurerS1Skill.name.in_(names),
        ).all():
            skill_rows_by_key.setdefault(
                (int(row.adventurer_id), str(row.name)), []
            ).append(row)

        candidates = []
        for declaration in declarations:
            if declaration.get('resource_type') == 'skill':
                matching_skills = skill_rows_by_key.get(
                    (declaration['adventurer_id'], declaration['item_name']), []
                )
                if len(matching_skills) != 1:
                    ambiguous += 1
                    continue
                row = matching_skills[0]
                current_desc = cls._text(row.description)
                frozen_desc = declaration['frozen_description']
                if frozen_desc in current_desc:
                    continue
                repair_desc = frozen_desc
                if current_desc and current_desc != '技能效果待探索':
                    repair_desc = f'{frozen_desc}\n{current_desc}'
                candidates.append({
                    **{k: v for k, v in declaration.items() if k != 'settled_at'},
                    'target_type': 'skill',
                    'target_id': int(row.id),
                    'expected_description': row.description,
                    'repair_description': repair_desc,
                    'settled_at': declaration['settled_at'].isoformat(),
                })
                continue
            settled_at = declaration['settled_at']
            matching = []
            for target_type, row in rows_by_key.get(
                (declaration['adventurer_id'], declaration['item_name']), []
            ):
                if not row.created_at or abs(row.created_at - settled_at) > cls.SETTLEMENT_GRACE:
                    continue
                if cls._text(row.custom_desc) != declaration['settled_description']:
                    continue
                matching.append((target_type, row))
            if len(matching) != 1:
                ambiguous += 1
                continue
            target_type, row = matching[0]
            if declaration['frozen_description'] in cls._text(row.custom_desc):
                continue
            candidates.append({
                **{k: v for k, v in declaration.items() if k != 'settled_at'},
                'target_type': target_type,
                'target_id': int(row.id),
                'expected_custom_desc': row.custom_desc,
                'repair_custom_desc': declaration['frozen_description'],
                'settled_at': settled_at.isoformat(),
            })
        return candidates, ambiguous

    @classmethod
    def _candidates(cls, expedition_log_id: int) -> tuple[list[dict], int]:
        log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(
            id=expedition_log_id,
        ).first()
        if not log:
            raise ValueError('探险日志不存在')
        if log.status != 'completed':
            raise ValueError('只能修复已完成的探险日志')
        declarations, ambiguous = cls._declarations(log)
        return cls._resolve_declarations(declarations, ambiguous)

    @classmethod
    def list_player_candidates(cls, adventurer_id: int) -> list[dict]:
        """批量列出玩家可机械还原的历史事故；不在读路径写库。"""
        logs = (
            db_manager.query(AdventurerS1ExpeditionLog)
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id
                == AdventurerS1ExpeditionLog.id,
            )
            .filter(
                AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id),
                AdventurerS1ExpeditionLog.status == 'completed',
            )
            .order_by(AdventurerS1ExpeditionLog.created_at.desc())
            .limit(cls.PLAYER_HISTORY_LIMIT)
            .all()
        )
        if not logs:
            return []

        log_by_id = {int(log.id): log for log in logs}
        declarations: list[dict] = []
        ambiguous = 0
        for log in logs:
            rows, row_ambiguous = cls._declarations(log)
            declarations.extend(
                row for row in rows
                if int(row.get('adventurer_id') or 0) == int(adventurer_id)
            )
            ambiguous += row_ambiguous

        candidates, _ = cls._resolve_declarations(declarations, ambiguous)
        from utils.time_utils import to_utc_iso

        return [
            {
                'expedition_log_id': candidate['expedition_log_id'],
                'target_type': candidate['target_type'],
                'target_id': candidate['target_id'],
                'item_name': candidate['item_name'],
                'frozen_description': candidate['frozen_description'],
                'target_dungeon': log_by_id[candidate['expedition_log_id']].target_dungeon,
                'party_name': log_by_id[candidate['expedition_log_id']].party_name,
                'log_created_at': to_utc_iso(
                    log_by_id[candidate['expedition_log_id']].created_at
                ) if log_by_id[candidate['expedition_log_id']].created_at else None,
            }
            for candidate in candidates
            if candidate['expedition_log_id'] in log_by_id
        ]

    @classmethod
    def get_player_candidate(
        cls,
        adventurer_id: int,
        expedition_log_id: int,
        target_type: str,
        target_id: int,
    ) -> dict | None:
        """按玩家、日志和当前实例精确定位一条可还原记录。"""
        log = (
            db_manager.query(AdventurerS1ExpeditionLog)
            .join(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id
                == AdventurerS1ExpeditionLog.id,
            )
            .filter(
                AdventurerS1ExpeditionLog.id == int(expedition_log_id),
                AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id),
            )
            .first()
        )
        if not log:
            return None
        candidates, _ = cls._candidates(int(expedition_log_id))
        candidate = next((
            candidate for candidate in candidates
            if int(candidate.get('adventurer_id') or 0) == int(adventurer_id)
            and candidate.get('target_type') == target_type
            and int(candidate.get('target_id') or 0) == int(target_id)
        ), None)
        if not candidate:
            return None
        from utils.time_utils import to_utc_iso

        return {
            **candidate,
            'target_dungeon': log.target_dungeon,
            'party_name': log.party_name,
            'log_created_at': to_utc_iso(log.created_at) if log.created_at else None,
        }

    @classmethod
    def preview(cls, expedition_log_id: int, admin_user_id: int):
        try:
            expedition_log_id = int(expedition_log_id)
            candidates, ambiguous = cls._candidates(expedition_log_id)
            result = {
                'summary': {
                    'expedition_log_id': expedition_log_id,
                    'candidate_count': len(candidates),
                    'ambiguous_count': ambiguous,
                    'has_backup': bool(db_manager.query(AdventurerS1Settings).filter_by(
                        key=cls.BACKUP_KEY,
                    ).first()),
                },
                'items': candidates,
            }
            result['preview_token'] = S1AdminScanSnapshotService.store(
                cls.SNAPSHOT_OPERATION, admin_user_id, result,
                allow_signed_fallback=True,
            )
            return result, None
        except (TypeError, ValueError) as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('同名替换装备描述修复 preview 失败')
            return None, f'预览失败：{exc}'

    @classmethod
    def execute(cls, admin_user_id: int, preview_token: str):
        preview = S1AdminScanSnapshotService.load(
            cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
            allow_signed_fallback=True,
        )
        if preview is None:
            return None, '预览快照已过期，请重新预览后再执行'
        items = preview.get('items') or []
        if not items:
            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
            )
            return {'repaired_count': 0}, None
        backup = {'created_at': _portable_utc_now().isoformat() + 'Z', 'entries': []}
        repaired = 0
        try:
            with db_manager.transaction():
                for item in items:
                    target_type = str(item.get('target_type') or '')
                    model = cls._target_model(target_type)
                    if model is None:
                        continue
                    value_field = cls._value_field(target_type)
                    row = db_manager.query(model).filter_by(
                        id=int(item['target_id']),
                        adventurer_id=int(item['adventurer_id']),
                    ).with_for_update().first()
                    expected_value = item.get(
                        'expected_description' if target_type == 'skill'
                        else 'expected_custom_desc'
                    )
                    repair_value = item.get(
                        'repair_description' if target_type == 'skill'
                        else 'repair_custom_desc'
                    )
                    if not row or getattr(row, value_field) != expected_value:
                        continue
                    backup['entries'].append({
                        'target_type': target_type,
                        'target_id': int(row.id),
                        'adventurer_id': int(row.adventurer_id),
                        'value_field': value_field,
                        'old_value': getattr(row, value_field),
                        'repaired_value': repair_value,
                    })
                    setattr(row, value_field, repair_value)
                    repaired += 1
                if backup['entries']:
                    setting = db_manager.query(AdventurerS1Settings).filter_by(
                        key=cls.BACKUP_KEY,
                    ).first()
                    payload = json.dumps(backup, ensure_ascii=False)
                    if setting is None:
                        db_manager.add(AdventurerS1Settings(
                            key=cls.BACKUP_KEY,
                            value=payload,
                            description='同名替换资源描述修复前备份（供一键回滚）',
                            updated_by_user_id=admin_user_id,
                        ))
                    else:
                        setting.value = payload
                        setting.updated_by_user_id = admin_user_id
            S1AdminScanSnapshotService.delete(
                cls.SNAPSHOT_OPERATION, admin_user_id, preview_token,
            )
            return {'repaired_count': repaired}, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('同名替换装备描述修复 execute 失败')
            return None, f'修复失败：{exc}'

    @classmethod
    def restore(cls):
        setting = db_manager.query(AdventurerS1Settings).filter_by(
            key=cls.BACKUP_KEY,
        ).first()
        if not setting or not setting.value:
            return None, '没有可用的同名替换装备描述修复备份'
        try:
            backup = json.loads(setting.value)
        except (TypeError, json.JSONDecodeError):
            return None, '修复备份已损坏，无法回滚'
        restored = 0
        try:
            with db_manager.transaction():
                for item in backup.get('entries') or []:
                    target_type = str(item.get('target_type') or '')
                    model = cls._target_model(target_type)
                    if model is None:
                        continue
                    value_field = item.get('value_field') or cls._value_field(target_type)
                    row = db_manager.query(model).filter_by(
                        id=int(item['target_id']),
                        adventurer_id=int(item['adventurer_id']),
                    ).with_for_update().first()
                    repaired_value = item.get('repaired_value', item.get('repaired_custom_desc'))
                    old_value = item.get('old_value', item.get('old_custom_desc'))
                    if not row or getattr(row, value_field) != repaired_value:
                        continue
                    setattr(row, value_field, old_value)
                    restored += 1
                setting.value = None
            return {'restored_count': restored}, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('同名替换装备描述修复 restore 失败')
            return None, f'回滚失败：{exc}'
