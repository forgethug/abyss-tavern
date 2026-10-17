"""转职 / 进阶后漏发职业技能的管理员批量补发（职业通用技能卷轴）。

检测逻辑：
- 从已完成探险日志的 member_updates 收集转职 / 进阶事件（new_class 变更）；
- 或从探险结算收集已消耗「职业通用技能卷轴」且未获得技能的冒险者；
- 当前有职业、且转职 / 卷轴消耗当次没有新增技能；
- 未持有「职业通用技能卷轴」；
- 未通过已批准申诉补发过同类职业技能或卷轴；
- 未被本服务补发过（备份表无记录）。

补发：图鉴物品「职业通用技能卷轴」入仓库；备份 warehouse_item_id 可一键撤销。
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import String, cast, or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1DataAppeal,
    AdventurerS1DataAppealAuditLog,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1Settings,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_class_identity_service import (
    CLASS_IDENTITY_SKILL_SCROLL_NAME,
    S1ClassIdentityService,
)

_BACKUP_KEY = 'class_skill_scroll_repair_backup'
# adventurer_s1_item_logs.source_type 上限 30 字符
_ITEM_LOG_SOURCE = 'admin_class_scroll_repair'
# SQLite / ensure_ascii JSON 会把中文存成 \uXXXX，预筛需兼容两种形态
_SCROLL_NAME_ESCAPED = json.dumps(
    CLASS_IDENTITY_SKILL_SCROLL_NAME, ensure_ascii=True,
).strip('"')


class S1ClassSkillScrollRepairService:
    """转职 / 进阶漏技能 → 补发职业通用技能卷轴。"""

    @staticmethod
    def _load_backup() -> dict:
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not rec or not rec.value:
            return {'entries': []}
        try:
            data = json.loads(rec.value)
        except (json.JSONDecodeError, TypeError):
            return {'entries': []}
        if not isinstance(data, dict) or not isinstance(data.get('entries'), list):
            return {'entries': []}
        return data

    @staticmethod
    def _backed_up_adv_ids() -> set[int]:
        return {
            int(e['adventurer_id'])
            for e in S1ClassSkillScrollRepairService._load_backup().get('entries') or []
            if e.get('adventurer_id') is not None
        }

    @staticmethod
    def _backup_repair_reasons(entry: dict) -> set[str]:
        reasons = entry.get('repair_reasons')
        if isinstance(reasons, list):
            return {str(r) for r in reasons if r}
        # 旧备份没有 reason 字段，只代表原来的“转职/进阶漏技能”补发。
        return {'class_change_missing_skill'}

    @classmethod
    def _backed_up_class_adv_ids(cls) -> set[int]:
        out: set[int] = set()
        for entry in cls._load_backup().get('entries') or []:
            if 'class_change_missing_skill' not in cls._backup_repair_reasons(entry):
                continue
            try:
                out.add(int(entry.get('adventurer_id')))
            except (TypeError, ValueError):
                continue
        return out

    @classmethod
    def _backed_up_scroll_keys(cls) -> set[tuple[int, int]]:
        out: set[tuple[int, int]] = set()
        for entry in cls._load_backup().get('entries') or []:
            if 'scroll_consumed_without_skill' not in cls._backup_repair_reasons(entry):
                continue
            scroll = entry.get('recent_scroll_consumption') if isinstance(entry.get('recent_scroll_consumption'), dict) else {}
            try:
                adv_id = int(entry.get('adventurer_id'))
                log_id = int(scroll.get('expedition_log_id') or 0)
            except (TypeError, ValueError):
                continue
            if log_id > 0:
                out.add((adv_id, log_id))
        return out

    @staticmethod
    def _skill_names_from_update(update: dict) -> list[str]:
        return [name for name, _desc in S1ClassIdentityService._iter_update_skills(update)]

    @staticmethod
    def _payload_mentions_class_scroll(raw) -> bool:
        if raw is None:
            return False
        if isinstance(raw, str):
            return CLASS_IDENTITY_SKILL_SCROLL_NAME in raw
        if isinstance(raw, dict):
            for key in ('name', 'item_name'):
                if CLASS_IDENTITY_SKILL_SCROLL_NAME in str(raw.get(key) or ''):
                    return True
            return any(
                CLASS_IDENTITY_SKILL_SCROLL_NAME in str(k)
                or S1ClassSkillScrollRepairService._payload_mentions_class_scroll(v)
                for k, v in raw.items()
            )
        if isinstance(raw, list):
            return any(S1ClassSkillScrollRepairService._payload_mentions_class_scroll(v) for v in raw)
        return False

    @classmethod
    def _build_expedition_log_scan_query(cls, *, only_adv_ids: set[int] | None = None):
        """只拉可能含转职/卷轴消耗的 completed 日志，避免全表两次扫 full_log。"""
        full_log_text = cast(AdventurerS1ExpeditionLog.full_log, String)
        scroll_name_filters = [
            full_log_text.contains(CLASS_IDENTITY_SKILL_SCROLL_NAME),
            full_log_text.contains(_SCROLL_NAME_ESCAPED),
        ]
        q = (
            AdventurerS1ExpeditionLog.query
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.full_log,
                AdventurerS1ExpeditionLog.created_at,
            )
            .filter(
                AdventurerS1ExpeditionLog.status == 'completed',
                or_(
                    full_log_text.contains('"new_class"'),
                    *scroll_name_filters,
                ),
            )
            .order_by(AdventurerS1ExpeditionLog.id.asc())
        )
        if only_adv_ids:
            id_filters = []
            for adv_id in only_adv_ids:
                id_filters.append(full_log_text.contains(f'"id": {adv_id}'))
                id_filters.append(full_log_text.contains(f'"id":{adv_id}'))
            q = q.filter(or_(*id_filters))
        return q

    @classmethod
    def _scan_expedition_signals(
        cls,
        *,
        only_adv_ids: set[int] | None = None,
    ) -> tuple[dict[int, list[dict]], dict[int, dict], dict]:
        """单次遍历：转职/进阶事件 + 卷轴消耗；返回 (class_changes, scroll_consumptions, scan_stats)。"""
        changes: dict[int, list[dict]] = {}
        scroll_consumptions: dict[int, dict] = {}
        tracked_class: dict[int, str | None] = {}
        logs_scanned = 0

        q = cls._build_expedition_log_scan_query(only_adv_ids=only_adv_ids)

        def iter_log_rows():
            after_id = 0
            while True:
                rows = q.filter(AdventurerS1ExpeditionLog.id > after_id).limit(100).all()
                db_manager.rollback()
                if not rows:
                    return
                yield from rows
                after_id = int(rows[-1][0])

        for log_id, full_log, created_at in iter_log_rows():
            logs_scanned += 1
            if not isinstance(full_log, dict):
                continue
            updates = full_log.get('member_updates')
            if not isinstance(updates, list):
                continue
            for mu in updates:
                if not isinstance(mu, dict):
                    continue
                try:
                    adv_id = int(mu.get('id'))
                except (TypeError, ValueError):
                    continue
                if only_adv_ids is not None and adv_id not in only_adv_ids:
                    continue

                new_class = S1ClassIdentityService._normalize_class(mu.get('new_class'))
                if new_class:
                    old_class = tracked_class.get(adv_id)
                    norm_old = (
                        S1ClassIdentityService._normalize_class(old_class)
                        if old_class else None
                    )
                    if norm_old != new_class:
                        entry = {
                            'log_id': log_id,
                            'old_class': norm_old,
                            'new_class': new_class,
                            'new_skill_names': cls._skill_names_from_update(mu),
                            'created_at': (
                                created_at.isoformat() + 'Z' if created_at else None
                            ),
                        }
                        entry['has_new_skill'] = bool(entry['new_skill_names'])
                        changes.setdefault(adv_id, []).append(entry)
                        tracked_class[adv_id] = new_class

                if (
                    cls._payload_mentions_class_scroll(mu.get('consumed_equipments'))
                    or cls._payload_mentions_class_scroll(mu.get('used_equipments'))
                ):
                    skill_names = cls._skill_names_from_update(mu)
                    entry = {
                        'expedition_log_id': log_id,
                        'new_skill_names': skill_names,
                        'has_new_skill': bool(skill_names),
                        'at': created_at.isoformat() + 'Z' if created_at else None,
                    }
                    previous = scroll_consumptions.get(adv_id)
                    if (
                        previous is None
                        or not entry['has_new_skill']
                        or previous.get('has_new_skill')
                    ):
                        scroll_consumptions[adv_id] = entry

        return changes, scroll_consumptions, {'expedition_logs_scanned': logs_scanned}

    @staticmethod
    def _batch_adv_ids_holding_scroll(adv_ids: set[int]) -> set[int]:
        """批量查询哪些冒险者随身/仓库已持有职业通用技能卷轴（2 次 SQL）。"""
        if not adv_ids:
            return set()
        ids = list(adv_ids)
        held: set[int] = set()
        wh_rows = (
            AdventurerS1WarehouseItem.query
            .with_entities(AdventurerS1WarehouseItem.adventurer_id)
            .filter(
                AdventurerS1WarehouseItem.adventurer_id.in_(ids),
                AdventurerS1WarehouseItem.name == CLASS_IDENTITY_SKILL_SCROLL_NAME,
            )
            .distinct()
            .all()
        )
        held.update(row[0] for row in wh_rows)
        eq_rows = (
            AdventurerS1Equipment.query
            .with_entities(AdventurerS1Equipment.adventurer_id)
            .filter(
                AdventurerS1Equipment.adventurer_id.in_(ids),
                AdventurerS1Equipment.name == CLASS_IDENTITY_SKILL_SCROLL_NAME,
            )
            .distinct()
            .all()
        )
        held.update(row[0] for row in eq_rows)
        return held

    @classmethod
    def _load_appeal_compensation(cls, adv_ids: set[int]) -> dict[int, dict]:
        """adv_id -> 已批准且未还原申诉补偿信息。"""
        if not adv_ids:
            return {}
        rows = (
            db_manager.query(
                AdventurerS1DataAppealAuditLog.adventurer_id,
                AdventurerS1DataAppeal.expedition_log_id,
                AdventurerS1DataAppeal.expedition_evidence,
                AdventurerS1DataAppealAuditLog.op_type,
                AdventurerS1DataAppealAuditLog.after_json,
            )
            .join(
                AdventurerS1DataAppeal,
                AdventurerS1DataAppeal.id == AdventurerS1DataAppealAuditLog.appeal_id,
            )
            .filter(
                AdventurerS1DataAppealAuditLog.adventurer_id.in_(list(adv_ids)),
                AdventurerS1DataAppealAuditLog.reverted.is_(False),
                AdventurerS1DataAppeal.status == 'approved',
                AdventurerS1DataAppealAuditLog.op_type.in_(('create_skill', 'create_item')),
            )
            .all()
        )
        out: dict[int, dict] = {}
        for adv_id, expedition_log_id, expedition_evidence, op_type, after_json in rows:
            bucket = out.setdefault(adv_id, {
                'has_scroll': False,
                'skill_names': [],
                'compensated_log_ids': set(),
            })
            after = after_json if isinstance(after_json, dict) else {}
            name = str(after.get('name') or '').strip()
            if not name:
                continue
            is_compensation = False
            if op_type == 'create_item' and name == CLASS_IDENTITY_SKILL_SCROLL_NAME:
                bucket['has_scroll'] = True
                is_compensation = True
            elif op_type == 'create_skill':
                bucket['skill_names'].append(name)
                is_compensation = True
            if is_compensation:
                effective_log_id = cls._appeal_expedition_log_id(
                    expedition_log_id,
                    expedition_evidence,
                )
                if effective_log_id:
                    bucket['compensated_log_ids'].add(effective_log_id)
        return out

    @staticmethod
    def _appeal_expedition_log_id(
        expedition_log_id,
        expedition_evidence,
    ) -> int | None:
        """兼容旧申诉：反范式列缺失时，从申诉证据恢复关联探险日志。"""
        for raw_log_id in (
            expedition_log_id,
            (
                expedition_evidence.get('expedition_log_id')
                if isinstance(expedition_evidence, dict) else None
            ),
        ):
            try:
                log_id = int(raw_log_id or 0)
            except (TypeError, ValueError):
                continue
            if log_id > 0:
                return log_id
        return None

    @classmethod
    def _appeal_has_class_skill(cls, appeal_info: dict) -> bool:
        if appeal_info.get('has_scroll'):
            return True
        return bool(appeal_info.get('skill_names'))

    @staticmethod
    def _appeal_has_scroll_consumption_compensation(appeal_info: dict, expedition_log_id: int | None) -> bool:
        if not expedition_log_id:
            return False
        return int(expedition_log_id) in (appeal_info.get('compensated_log_ids') or set())

    @classmethod
    def _compute_candidates(
        cls,
        *,
        only_adv_ids: set[int] | None = None,
    ) -> tuple[list[dict], dict]:
        """返回 (candidates, stats)。"""
        class_changes, scroll_consumptions, scan_stats = cls._scan_expedition_signals(
            only_adv_ids=only_adv_ids,
        )
        scroll_failed_count = sum(
            1 for row in scroll_consumptions.values() if not row.get('has_new_skill')
        )
        if not class_changes and not scroll_consumptions:
            return [], {
                'class_change_adventurers': 0,
                'scroll_consumed_adventurers': 0,
                'scroll_failed_adventurers': 0,
                'candidates': 0,
                **scan_stats,
            }

        candidate_adv_ids = set(class_changes.keys()) | set(scroll_consumptions.keys())
        if only_adv_ids is not None:
            candidate_adv_ids &= only_adv_ids
        if not candidate_adv_ids:
            return [], {
                'class_change_adventurers': len(class_changes),
                'scroll_consumed_adventurers': len(scroll_consumptions),
                'scroll_failed_adventurers': scroll_failed_count,
                'candidates': 0,
                **scan_stats,
            }

        adv_rows = (
            AdventurerS1.query
            .with_entities(
                AdventurerS1.id,
                AdventurerS1.name,
                AdventurerS1.character_class,
                AdventurerS1.level,
                AdventurerS1.status,
                AdventurerS1.owner_id,
            )
            .filter(AdventurerS1.id.in_(list(candidate_adv_ids)))
            .all()
        )
        adv_map = {r.id: r for r in adv_rows}

        scroll_adv_ids = cls._batch_adv_ids_holding_scroll(candidate_adv_ids)

        appeal_map = cls._load_appeal_compensation(candidate_adv_ids)
        backed_up_class_adv_ids = cls._backed_up_class_adv_ids()
        backed_up_scroll_keys = cls._backed_up_scroll_keys()

        candidates: list[dict] = []
        skipped = {
            'no_current_class': 0,
            'has_class_skill': 0,
            'has_scroll': 0,
            'appeal_compensated': 0,
            'already_repaired': 0,
            'class_change_had_skill': 0,
            'scroll_consumption_had_skill': 0,
        }

        for adv_id in sorted(candidate_adv_ids):
            adv = adv_map.get(adv_id)
            if adv is None:
                continue
            current_class = S1ClassIdentityService._normalize_class(adv.character_class)
            if not current_class:
                skipped['no_current_class'] += 1
                continue

            events = class_changes.get(adv_id) or []
            last = events[-1] if events else None
            last_old = last.get('old_class') if last else None
            scroll_consumption = scroll_consumptions.get(adv_id)
            has_missing_class_change_skill = bool(last and not last.get('has_new_skill'))
            has_failed_scroll_learning = bool(scroll_consumption and not scroll_consumption.get('has_new_skill'))
            if not has_missing_class_change_skill and not has_failed_scroll_learning:
                if last and last.get('has_new_skill'):
                    skipped['class_change_had_skill'] += 1
                if scroll_consumption and scroll_consumption.get('has_new_skill'):
                    skipped['scroll_consumption_had_skill'] += 1
                continue

            appeal_info = appeal_map.get(adv_id) or {}
            class_change_candidate = has_missing_class_change_skill
            scroll_failed_candidate = has_failed_scroll_learning

            if class_change_candidate and adv_id in scroll_adv_ids:
                class_change_candidate = False
                skipped['has_scroll'] += 1

            if class_change_candidate and cls._appeal_has_class_skill(appeal_info):
                class_change_candidate = False
                skipped['appeal_compensated'] += 1

            scroll_log_id = None
            if scroll_consumption:
                try:
                    scroll_log_id = int(scroll_consumption.get('expedition_log_id') or 0) or None
                except (TypeError, ValueError):
                    scroll_log_id = None
            if scroll_failed_candidate and cls._appeal_has_scroll_consumption_compensation(appeal_info, scroll_log_id):
                scroll_failed_candidate = False
                skipped['appeal_compensated'] += 1

            if class_change_candidate and adv_id in backed_up_class_adv_ids:
                class_change_candidate = False
                skipped['already_repaired'] += 1

            if scroll_failed_candidate and scroll_log_id and (adv_id, scroll_log_id) in backed_up_scroll_keys:
                scroll_failed_candidate = False
                skipped['already_repaired'] += 1

            if not class_change_candidate and not scroll_failed_candidate:
                continue

            candidates.append({
                'id': adv.id,
                'name': adv.name,
                'level': adv.level,
                'status': adv.status,
                'owner_id': adv.owner_id,
                'current_class': current_class,
                'last_class_change': ({
                    'log_id': last.get('log_id'),
                    'old_class': last_old,
                    'new_class': last.get('new_class'),
                    'new_skill_names': last.get('new_skill_names') or [],
                    'at': last.get('created_at'),
                } if last else None),
                'recent_scroll_consumption': scroll_consumption,
                'repair_reasons': [
                    reason for reason, enabled in (
                        ('class_change_missing_skill', class_change_candidate),
                        ('scroll_consumed_without_skill', scroll_failed_candidate),
                    ) if enabled
                ],
                'class_change_count': len(events),
                'grant_item': CLASS_IDENTITY_SKILL_SCROLL_NAME,
            })

        stats = {
            'class_change_adventurers': len(class_changes),
            'scroll_consumed_adventurers': len(scroll_consumptions),
            'scroll_failed_adventurers': scroll_failed_count,
            'scanned': len(candidate_adv_ids),
            'candidates': len(candidates),
            **scan_stats,
            **skipped,
            'has_backup': bool(backed_up_class_adv_ids or backed_up_scroll_keys),
            'already_repaired_count': len(backed_up_class_adv_ids) + len(backed_up_scroll_keys),
        }
        return candidates, stats

    @classmethod
    def preview(
        cls,
        adventurer_id: int | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """只读预览漏发职业技能、可补卷轴的冒险者清单。"""
        only_ids = {int(adventurer_id)} if adventurer_id else None
        try:
            candidates, stats = cls._compute_candidates(only_adv_ids=only_ids)
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 转职技能卷轴补发 preview 失败')
            return None, f'预览失败：{e}'
        return {
            **stats,
            'items': candidates,
            'scroll_name': CLASS_IDENTITY_SKILL_SCROLL_NAME,
        }, None

    @classmethod
    def repair(
        cls,
        *,
        adventurer_ids: list[int] | None = None,
        updated_by_user_id: int | None = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """批量补发职业通用技能卷轴（入仓库），备份可还原。"""
        only_ids = set(int(i) for i in (adventurer_ids or []) if i) or None
        try:
            candidates, stats = cls._compute_candidates(only_adv_ids=only_ids)
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 转职技能卷轴补发计算失败')
            return None, f'修复失败：{e}'

        if not candidates:
            return {
                'granted': 0,
                'skipped': 0,
                'errors': [],
                **stats,
            }, None

        from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item

        data = cls._load_backup()
        entries = list(data.get('entries') or [])
        granted = 0
        skipped = 0
        errors: list[dict] = []
        new_entries: list[dict] = []

        try:
            with db_manager.transaction():
                for item in candidates:
                    adv_id = item['id']
                    adv = (
                        AdventurerS1.query
                        .with_for_update()
                        .filter_by(id=adv_id)
                        .first()
                    )
                    if adv is None:
                        errors.append({'id': adv_id, 'error': '冒险者不存在'})
                        continue

                    current_class = S1ClassIdentityService._normalize_class(
                        adv.character_class,
                    )
                    if not current_class:
                        skipped += 1
                        continue

                    _, err = grant_catalog_item(
                        adv,
                        CLASS_IDENTITY_SKILL_SCROLL_NAME,
                        quantity=1,
                        prefer_warehouse=True,
                        source_type=_ITEM_LOG_SOURCE,
                        description='管理员补发：转职/进阶漏发职业技能卷轴',
                    )
                    if err:
                        errors.append({'id': adv_id, 'name': adv.name, 'error': err})
                        continue

                    wh_item = (
                        AdventurerS1WarehouseItem.query
                        .filter_by(
                            adventurer_id=adv.id,
                            name=CLASS_IDENTITY_SKILL_SCROLL_NAME,
                        )
                        .order_by(AdventurerS1WarehouseItem.id.desc())
                        .first()
                    )
                    now_iso = _portable_utc_now().isoformat() + 'Z'
                    entry = {
                        'adventurer_id': adv.id,
                        'name': adv.name,
                        'warehouse_item_id': wh_item.id if wh_item else None,
                        'current_class': current_class,
                        'last_class_change': item.get('last_class_change'),
                        'recent_scroll_consumption': item.get('recent_scroll_consumption'),
                        'repair_reasons': item.get('repair_reasons') or ['class_change_missing_skill'],
                        'at': now_iso,
                    }
                    new_entries.append(entry)
                    granted += 1

                if new_entries:
                    entries.extend(new_entries)
                    payload = json.dumps({'entries': entries}, ensure_ascii=False)
                    rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                    if rec is None:
                        db_manager.add(AdventurerS1Settings(
                            key=_BACKUP_KEY,
                            value=payload,
                            description='转职漏技能补发卷轴备份（warehouse_item_id，供一键撤销）',
                            updated_by_user_id=updated_by_user_id,
                        ))
                    else:
                        rec.value = payload
                        rec.updated_by_user_id = updated_by_user_id
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 转职技能卷轴补发写库失败')
            return None, f'修复失败：{e}'

        current_app.logger.info(
            'S1 转职技能卷轴补发完成：成功 %s，跳过 %s，失败 %s',
            granted, skipped, len(errors),
        )
        return {
            'granted': granted,
            'skipped': skipped,
            'errors': errors,
            **stats,
            'candidates': len(candidates),
        }, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        """撤销本服务补发的卷轴（删除备份记录的仓库物品行）。"""
        data = cls._load_backup()
        entries = data.get('entries') or []
        if not entries:
            return None, '没有可用的转职技能卷轴补发备份'

        removed = 0
        skipped = 0
        missing = 0
        try:
            with db_manager.transaction():
                for entry in entries:
                    wh_id = entry.get('warehouse_item_id')
                    if wh_id is None:
                        missing += 1
                        continue
                    row = (
                        AdventurerS1WarehouseItem.query
                        .with_for_update()
                        .filter_by(id=int(wh_id))
                        .first()
                    )
                    if row is None:
                        missing += 1
                        continue
                    if row.name != CLASS_IDENTITY_SKILL_SCROLL_NAME:
                        skipped += 1
                        continue
                    db_manager.delete_instance(row)
                    removed += 1
                rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if rec is not None:
                    rec.value = json.dumps({'entries': []}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 转职技能卷轴补发还原失败')
            return None, f'还原失败：{e}'

        current_app.logger.info(
            'S1 转职技能卷轴补发还原：删除 %s 件，缺失 %s，跳过 %s',
            removed, missing, skipped,
        )
        return {
            'removed': removed,
            'missing': missing,
            'skipped': skipped,
            'total_backup': len(entries),
        }, None
