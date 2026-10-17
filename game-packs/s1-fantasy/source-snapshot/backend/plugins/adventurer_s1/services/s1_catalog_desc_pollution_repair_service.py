"""申诉 AI 误把图鉴物 custom_desc 空值当成「空描述」补写 / 误清空进化描述的批量还原。

图鉴物品实例 custom_desc 留空时，展示会回退 adventurer_s1_items.description。
申诉 normalize 曾允许 AI 写入自拟 custom_desc，本服务按 audit 台账扫描并还原。

- :meth:`preview` —— 只读候选清单
- :meth:`execute` —— 备份后批量还原 custom_desc
- :meth:`restore` —— 从最近一次 execute 备份一键回滚
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from models.user import User
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1DataAppeal,
    AdventurerS1DataAppealAuditLog,
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1Settings,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_admin_scan_snapshot_service import S1AdminScanSnapshotService

_BACKUP_KEY = 'catalog_desc_pollution_repair_backup'
_DISMISS_KEY = 'catalog_desc_pollution_repair_dismissed'


class S1CatalogDescPollutionRepairService:
    """图鉴物 custom_desc 污染 / 误清空还原。"""

    @staticmethod
    def _text(value) -> str:
        return str(value or '').strip()

    @staticmethod
    def _load_dismissed() -> set[str]:
        rec = AdventurerS1Settings.query.filter_by(key=_DISMISS_KEY).first()
        if not rec or not rec.value:
            return set()
        try:
            payload = json.loads(rec.value)
        except (json.JSONDecodeError, TypeError):
            return set()
        keys = payload.get('issue_keys') if isinstance(payload, dict) else payload
        if not isinstance(keys, list):
            return set()
        return {str(k).strip() for k in keys if str(k).strip()}

    @staticmethod
    def _save_dismissed(keys: set[str]) -> None:
        payload = json.dumps({'issue_keys': sorted(keys)}, ensure_ascii=False)
        rec = AdventurerS1Settings.query.filter_by(key=_DISMISS_KEY).first()
        if rec is None:
            db_manager.add(AdventurerS1Settings(
                key=_DISMISS_KEY,
                value=payload,
                description='图鉴描述污染还原：已标记误报 issue_key',
            ))
        else:
            rec.value = payload

    @staticmethod
    def _has_backup() -> bool:
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        return bool(rec and rec.value)

    @staticmethod
    def _fetch_records(
        items: list[dict],
        *,
        for_update: bool = False,
    ) -> dict[tuple[str, int, int], object]:
        """Fetch affected rows in at most two queries; optionally lock them."""
        records: dict[tuple[str, int, int], object] = {}
        models = {
            'equipment': AdventurerS1Equipment,
            'warehouse': AdventurerS1WarehouseItem,
        }
        for target_type, model in models.items():
            target_ids = {
                int(item['target_id'])
                for item in items
                if item.get('target_type') == target_type and item.get('target_id') is not None
            }
            if not target_ids:
                continue
            query = db_manager.query(model).filter(model.id.in_(target_ids))
            if for_update:
                query = query.with_for_update()
            rows = query.all()
            for row in rows:
                records[(target_type, int(row.id), int(row.adventurer_id))] = row
        return records

    @staticmethod
    def _fetch_records_for_update(items: list[dict]) -> dict[tuple[str, int, int], object]:
        return S1CatalogDescPollutionRepairService._fetch_records(
            items,
            for_update=True,
        )

    @staticmethod
    def _catalog_desc_by_name(names: set[str]) -> dict[str, str]:
        if not names:
            return {}
        rows = (
            db_manager.query(AdventurerS1Item)
            .filter(AdventurerS1Item.name.in_(list(names)))
            .all()
        )
        return {
            str(row.name).strip(): S1CatalogDescPollutionRepairService._text(row.description)
            for row in rows
            if row and row.name and S1CatalogDescPollutionRepairService._text(row.description)
        }

    @classmethod
    def _build_candidates(cls, excluded: set[str]) -> tuple[list[dict], dict]:
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import (
            AdventurerS1DataAppealService,
        )

        rows = (
            db_manager.query(AdventurerS1DataAppealAuditLog, AdventurerS1DataAppeal)
            .join(
                AdventurerS1DataAppeal,
                AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id,
            )
            .filter(
                AdventurerS1DataAppeal.status == 'approved',
                AdventurerS1DataAppealAuditLog.reverted.is_(False),
                AdventurerS1DataAppealAuditLog.op_type == 'update',
                AdventurerS1DataAppealAuditLog.target_type.in_(('equipment', 'warehouse')),
            )
            .order_by(AdventurerS1DataAppealAuditLog.id.asc())
            .all()
        )
        scan = {
            'audit_rows': len(rows),
            'catalog_fill_rows': 0,
            'cleared_custom_rows': 0,
            'skipped_dismissed': 0,
            'skipped_already_ok': 0,
            'skipped_missing_record': 0,
        }
        candidates: list[dict] = []
        seen_keys: set[str] = set()
        pending_names: set[str] = set()
        clear_ids_by_appeal: dict[int, dict[str, set[int]]] = {}

        for audit, appeal in rows:
            before = audit.before_json if isinstance(audit.before_json, dict) else {}
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            before_desc = cls._text(before.get('custom_desc'))
            after_desc = cls._text(after.get('custom_desc'))
            try:
                target_id = int(audit.target_id or 0)
            except (TypeError, ValueError):
                continue
            if not target_id:
                continue
            appeal_id = int(appeal.id)
            clear_ids = clear_ids_by_appeal.get(appeal_id)
            if clear_ids is None:
                clear_ids = (
                    AdventurerS1DataAppealService
                    ._collect_custom_desc_clear_ids_from_appeal(appeal)
                )
                clear_ids_by_appeal[appeal_id] = clear_ids
            audit_item_name = cls._text(
                after.get('name') or before.get('name')
            )
            explicit_clear_name_placeholder = (
                bool(before_desc)
                and bool(audit_item_name)
                and after_desc == audit_item_name
                and target_id in clear_ids.get(audit.target_type, set())
            )
            issue_type = None
            if not before_desc and after_desc:
                issue_type = 'catalog_fill'
                scan['catalog_fill_rows'] += 1
            elif explicit_clear_name_placeholder:
                # 历史 apply 在二审明确清空 custom_desc 后又用物品名称占位；
                # 仍归入 catalog_fill，因为修复动作同样是清空并回退图鉴。
                issue_type = 'catalog_fill'
                scan['catalog_fill_rows'] += 1
            elif before_desc and not after_desc:
                issue_type = 'cleared_custom'
                scan['cleared_custom_rows'] += 1
            else:
                continue
            issue_key = f'audit:{audit.id}'
            if issue_key in excluded or issue_key in seen_keys:
                if issue_key in excluded:
                    scan['skipped_dismissed'] += 1
                continue
            seen_keys.add(issue_key)
            candidates.append({
                'issue_key': issue_key,
                'issue_type': issue_type,
                'audit_log_id': audit.id,
                'appeal_id': appeal.id,
                'target_type': audit.target_type,
                'target_id': target_id,
                'adventurer_id': appeal.adventurer_id,
                'item_name': audit_item_name,
                'before_custom_desc': before_desc or None,
                'after_custom_desc': after_desc or None,
                'current_custom_desc': None,
                'repair_custom_desc': None,
                'reason': '',
            })

        current_records = cls._fetch_records(candidates)
        existing_candidates: list[dict] = []
        for item in candidates:
            record = current_records.get((
                item['target_type'],
                int(item['target_id']),
                int(item['adventurer_id']),
            ))
            if record is None:
                scan['skipped_missing_record'] += 1
                continue
            item_name = cls._text(record.name)
            if not item_name:
                continue
            item['item_name'] = item_name
            item['current_custom_desc'] = cls._text(record.custom_desc) or None
            pending_names.add(item_name)
            existing_candidates.append(item)
        candidates = existing_candidates

        catalog_map = cls._catalog_desc_by_name(pending_names)
        adv_ids = {c['adventurer_id'] for c in candidates}
        user_map: dict[int, tuple] = {}
        if adv_ids:
            adv_rows = (
                db_manager.query(AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id)
                .filter(AdventurerS1.id.in_(list(adv_ids)))
                .all()
            )
            user_ids = {int(r[2]) for r in adv_rows if r[2] is not None}
            users = {}
            if user_ids:
                users = {
                    u.id: u
                    for u in db_manager.query(User).filter(User.id.in_(list(user_ids))).all()
                }
            for adv_id, adv_name, user_id in adv_rows:
                user = users.get(int(user_id)) if user_id is not None else None
                user_map[int(adv_id)] = (adv_name, user)

        filtered: list[dict] = []
        for item in candidates:
            name = item['item_name']
            catalog_desc = catalog_map.get(name, '')
            current = cls._text(item.get('current_custom_desc'))
            issue_type = item['issue_type']

            if issue_type == 'catalog_fill':
                if not catalog_desc:
                    scan['skipped_already_ok'] += 1
                    continue
                after_desc = cls._text(item.get('after_custom_desc'))
                if not current or current != after_desc:
                    scan['skipped_already_ok'] += 1
                    continue
                item['catalog_desc'] = catalog_desc
                item['repair_custom_desc'] = None
                if cls._text(item.get('before_custom_desc')):
                    item['reason'] = (
                        '二审明确清空 custom_desc 后被名称占位；'
                        '清空以回退图鉴权威描述'
                    )
                else:
                    item['reason'] = (
                        '图鉴物 custom_desc 为空时展示回退图鉴；'
                        '申诉 AI 误补自拟描述'
                    )
            else:
                before_desc = cls._text(item.get('before_custom_desc'))
                if not before_desc:
                    scan['skipped_already_ok'] += 1
                    continue
                if current == before_desc:
                    scan['skipped_already_ok'] += 1
                    continue
                if current and current != before_desc:
                    scan['skipped_already_ok'] += 1
                    continue
                item['catalog_desc'] = catalog_desc or None
                item['repair_custom_desc'] = before_desc
                item['reason'] = '申诉 AI 误清空原有 custom_desc（进化/定制文案）'

            adv_name, user = user_map.get(int(item['adventurer_id']), ('', None))
            item['adventurer_name'] = adv_name or ''
            item['username'] = getattr(user, 'username', None) if user else None
            item['nickname'] = getattr(user, 'nickname', None) if user else None
            filtered.append(item)

        return filtered, scan

    @classmethod
    def preview(
        cls,
        limit: int = 100,
        admin_user_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        try:
            limit = max(1, min(int(limit or 100), 500))
        except (TypeError, ValueError):
            return None, 'limit 无效'
        try:
            excluded = cls._load_dismissed()
            candidates, scan = cls._build_candidates(excluded)
            shown = candidates[:limit]
            result = {
                'summary': {
                    'total_candidates': len(candidates),
                    'shown_count': len(shown),
                    'catalog_fill_count': sum(1 for c in candidates if c['issue_type'] == 'catalog_fill'),
                    'cleared_custom_count': sum(1 for c in candidates if c['issue_type'] == 'cleared_custom'),
                    'scan': scan,
                    'has_backup': cls._has_backup(),
                },
                'items': shown,
            }
            if admin_user_id is not None:
                result['snapshot_token'] = S1AdminScanSnapshotService.store(
                    'catalog_desc_pollution', admin_user_id, result,
                )
            return result, None
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('图鉴描述污染还原 preview 失败')
            return None, f'预览失败：{exc}'

    @classmethod
    def execute(
        cls,
        admin_user_id: Optional[int],
        limit: int = 100,
        note: str = '',
        preview_token: str = '',
    ) -> Tuple[Optional[dict], Optional[str]]:
        preview = None
        if preview_token and admin_user_id is not None:
            preview = S1AdminScanSnapshotService.load(
                'catalog_desc_pollution', admin_user_id, preview_token,
            )
            if preview is None:
                return None, '预览快照已过期，请重新预览后再执行'
        if preview is None:
            preview, err = cls.preview(limit=limit)
            if err:
                return None, err
        items = (preview or {}).get('items') or []
        if preview_token:
            dismissed = cls._load_dismissed()
            items = [item for item in items if item.get('issue_key') not in dismissed]
        if not items:
            if admin_user_id is not None:
                S1AdminScanSnapshotService.delete(
                    'catalog_desc_pollution', admin_user_id, preview_token,
                )
            return {
                'repaired': 0,
                'breakdown': {'catalog_fill': 0, 'cleared_custom': 0},
                'note': note or '',
            }, None

        backup = {
            'created_at': _portable_utc_now().isoformat() + 'Z',
            'note': (note or '').strip(),
            'entries': [],
        }
        breakdown = {'catalog_fill': 0, 'cleared_custom': 0}

        try:
            with db_manager.transaction():
                record_map = cls._fetch_records_for_update(items)
                for item in items:
                    record = record_map.get((
                        str(item['target_type']),
                        int(item['target_id']),
                        int(item['adventurer_id']),
                    ))
                    if not record:
                        continue
                    expected_desc = item.get('current_custom_desc')
                    if record.custom_desc != expected_desc:
                        continue
                    old_desc = record.custom_desc
                    new_desc = item.get('repair_custom_desc')
                    backup['entries'].append({
                        'issue_key': item['issue_key'],
                        'audit_log_id': item['audit_log_id'],
                        'target_type': item['target_type'],
                        'target_id': item['target_id'],
                        'adventurer_id': item['adventurer_id'],
                        'old_custom_desc': old_desc,
                    })
                    record.custom_desc = new_desc
                    breakdown[item['issue_type']] = breakdown.get(item['issue_type'], 0) + 1

                payload = json.dumps(backup, ensure_ascii=False)
                rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if rec is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=payload,
                        description='图鉴描述污染还原前备份（custom_desc，供一键回滚）',
                        updated_by_user_id=admin_user_id,
                    ))
                else:
                    rec.value = payload
                    rec.updated_by_user_id = admin_user_id
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('图鉴描述污染还原 execute 失败')
            return None, f'还原失败：{exc}'

        total = sum(breakdown.values())
        current_app.logger.info(
            '图鉴描述污染还原完成：共 %s 条 %s',
            total, breakdown,
        )
        if admin_user_id is not None:
            S1AdminScanSnapshotService.delete(
                'catalog_desc_pollution', admin_user_id, preview_token,
            )
        return {
            'repaired': total,
            'breakdown': breakdown,
            'note': note or '',
        }, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not rec or not rec.value:
            return None, '没有可用的图鉴描述污染还原备份'
        try:
            payload = json.loads(rec.value)
        except (json.JSONDecodeError, TypeError):
            return None, '备份数据已损坏，无法回滚'

        entries = payload.get('entries') or []
        if not entries:
            return None, '备份为空，无法回滚'

        try:
            with db_manager.transaction():
                restored = 0
                for entry in entries:
                    record = cls._fetch_record(
                        str(entry.get('target_type') or ''),
                        int(entry.get('target_id') or 0),
                        int(entry.get('adventurer_id') or 0),
                    )
                    if not record:
                        continue
                    record.custom_desc = entry.get('old_custom_desc')
                    restored += 1
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('图鉴描述污染还原 restore 失败')
            return None, f'回滚失败：{exc}'

        return {'restored': restored}, None

    @classmethod
    def dismiss(
        cls,
        admin_user_id: Optional[int],
        issue_key: str,
        note: str = '',
    ) -> Tuple[Optional[dict], Optional[str]]:
        key = cls._text(issue_key)
        if not key:
            return None, 'issue_key 不能为空'
        dismissed = cls._load_dismissed()
        dismissed.add(key)
        try:
            with db_manager.transaction():
                cls._save_dismissed(dismissed)
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('图鉴描述污染 dismiss 失败 admin=%s key=%s', admin_user_id, key)
            return None, f'标记误报失败：{exc}'
        return {'issue_key': key, 'note': (note or '').strip()}, None
