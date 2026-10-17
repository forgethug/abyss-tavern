"""Repair expedition skills recorded in settlement but missing from the skill table."""
from __future__ import annotations

import json
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer.services.adventurer_parser_service import _normalize_new_skills_payload
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1Settings,
    AdventurerS1Skill,
)

_BACKUP_KEY = 'expedition_missing_skill_repair_backup'


class S1ExpeditionSkillRepairService:
    """Preview, repair, and restore mechanically provable expedition skill omissions."""

    @staticmethod
    def _load_backup() -> dict:
        row = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not row or not row.value:
            return {'entries': []}
        try:
            data = json.loads(row.value)
        except (TypeError, ValueError):
            return {'entries': []}
        return data if isinstance(data, dict) else {'entries': []}

    @staticmethod
    def _member_updates(full_log: object) -> list[dict]:
        if not isinstance(full_log, dict):
            return []
        updates = full_log.get('member_updates')
        return [item for item in updates if isinstance(item, dict)] if isinstance(updates, list) else []

    @classmethod
    def preview(
        cls,
        *,
        expedition_log_id: int | None = None,
        adventurer_id: int | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        query = AdventurerS1ExpeditionLog.query.with_entities(
            AdventurerS1ExpeditionLog.id,
            AdventurerS1ExpeditionLog.full_log,
        ).filter_by(status='completed')
        if expedition_log_id:
            query = query.filter(AdventurerS1ExpeditionLog.id == expedition_log_id)
        query = query.order_by(AdventurerS1ExpeditionLog.id.asc())

        promises: dict[tuple[int, str], dict] = {}
        scanned_logs = 0

        def iter_log_rows():
            after_id = 0
            while True:
                rows = query.filter(AdventurerS1ExpeditionLog.id > after_id).limit(100).all()
                db_manager.rollback()
                if not rows:
                    return
                yield from rows
                after_id = int(rows[-1][0])

        for log_id, full_log in iter_log_rows():
            scanned_logs += 1
            for update in cls._member_updates(full_log):
                try:
                    member_id = int(update.get('id'))
                except (TypeError, ValueError):
                    continue
                if adventurer_id and member_id != adventurer_id:
                    continue
                for skill in _normalize_new_skills_payload(
                    update.get('new_skills'),
                    split_semantic_name_desc=False,
                ):
                    name = str(skill.get('name') or '').strip()
                    if not name:
                        continue
                    key = (member_id, name)
                    entry = promises.setdefault(key, {
                        'adventurer_id': member_id,
                        'skill_name': name,
                        'description': str(skill.get('desc') or '').strip() or '技能效果待探索',
                        'expedition_log_ids': [],
                    })
                    entry['expedition_log_ids'].append(int(log_id))
                    if str(skill.get('desc') or '').strip():
                        entry['description'] = str(skill['desc']).strip()

        if not promises:
            return {'scanned_logs': scanned_logs, 'candidate_count': 0, 'items': []}, None

        adv_ids = sorted({key[0] for key in promises})
        names = sorted({key[1] for key in promises})
        existing = {
            (int(row.adventurer_id), str(row.name))
            for row in AdventurerS1Skill.query.filter(
                AdventurerS1Skill.adventurer_id.in_(adv_ids),
                AdventurerS1Skill.name.in_(names),
            ).all()
        }
        adv_names = dict(
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(adv_ids)).all()
        )
        backed_up = {
            (int(item.get('adventurer_id')), str(item.get('skill_name') or ''))
            for item in cls._load_backup().get('entries') or []
            if item.get('adventurer_id') is not None
        }

        items = []
        for key, entry in promises.items():
            if key in existing:
                continue
            row = dict(entry)
            row['adventurer_name'] = adv_names.get(key[0], f'#{key[0]}')
            row['already_repaired'] = key in backed_up
            items.append(row)
        items.sort(key=lambda item: (item['adventurer_id'], item['skill_name']))
        return {
            'scanned_logs': scanned_logs,
            'candidate_count': len(items),
            'items': items,
        }, None

    @classmethod
    def repair(
        cls,
        *,
        expedition_log_id: int | None = None,
        adventurer_id: int | None = None,
        updated_by_user_id: int | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        preview, err = cls.preview(
            expedition_log_id=expedition_log_id,
            adventurer_id=adventurer_id,
        )
        if err:
            return None, err
        candidates = preview.get('items') or []
        if not candidates:
            return {'created': 0, 'skipped': 0, 'items': []}, None

        created_rows = []
        skipped = 0
        try:
            with db_manager.transaction():
                backup = cls._load_backup()
                entries = list(backup.get('entries') or [])
                max_equipped = int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5))
                for candidate in candidates:
                    adv_id = int(candidate['adventurer_id'])
                    name = str(candidate['skill_name'])
                    adv = AdventurerS1.query.filter_by(id=adv_id).with_for_update().first()
                    if adv is None:
                        skipped += 1
                        continue
                    existing = AdventurerS1Skill.query.filter_by(adventurer_id=adv_id, name=name).first()
                    if existing is not None:
                        skipped += 1
                        continue
                    equipped_count = AdventurerS1Skill.query.filter_by(
                        adventurer_id=adv_id, equipped=True,
                    ).count()
                    skill = AdventurerS1Skill(
                        adventurer_id=adv_id,
                        name=name,
                        description=str(candidate.get('description') or '技能效果待探索'),
                        equipped=equipped_count < max_equipped,
                    )
                    db_manager.add(skill)
                    db_manager.flush()
                    record = {
                        'skill_id': int(skill.id),
                        'adventurer_id': adv_id,
                        'adventurer_name': adv.name,
                        'skill_name': name,
                        'expedition_log_ids': list(candidate.get('expedition_log_ids') or []),
                    }
                    entries.append(record)
                    created_rows.append(record)

                payload = json.dumps({'entries': entries}, ensure_ascii=False)
                setting = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if setting is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=payload,
                        description='探险结算已承诺但技能表漏发的补发备份',
                        updated_by_user_id=updated_by_user_id,
                    ))
                else:
                    setting.value = payload
                    setting.updated_by_user_id = updated_by_user_id
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 探险技能漏发修复失败')
            return None, f'修复失败：{exc}'
        return {'created': len(created_rows), 'skipped': skipped, 'items': created_rows}, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        entries = cls._load_backup().get('entries') or []
        if not entries:
            return None, '没有可还原的探险技能补发记录'
        removed = 0
        skipped = 0
        try:
            with db_manager.transaction():
                for entry in entries:
                    skill = AdventurerS1Skill.query.filter_by(id=entry.get('skill_id')).first()
                    if (
                        skill is None
                        or int(skill.adventurer_id) != int(entry.get('adventurer_id'))
                        or skill.name != entry.get('skill_name')
                    ):
                        skipped += 1
                        continue
                    db_manager.delete_instance(skill)
                    removed += 1
                setting = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if setting is not None:
                    setting.value = json.dumps({'entries': []}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 探险技能漏发还原失败')
            return None, f'还原失败：{exc}'
        return {'removed': removed, 'skipped': skipped}, None
