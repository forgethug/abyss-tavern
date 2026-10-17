"""S1 玩家可见内部标识污染的扫描、修复与恢复。

修复只改当前资产和当前托管状态；原始探险日志、物品流水与货币流水保持不变。
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from collections import defaultdict
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import func

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1Item,
    AdventurerS1ItemLog,
    AdventurerS1MarketListing,
    AdventurerS1MarketOffer,
    AdventurerS1MarketBuyOffer,
    AdventurerS1SecretAuctionLot,
    AdventurerS1Settings,
    AdventurerS1Skill,
    AdventurerS1Trait,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
from plugins.adventurer_s1.services.s1_visible_name_policy import (
    LEGACY_VISIBLE_NAME_ALIASES,
    is_internal_code_identifier_name,
)


_BACKUP_KEY = 'visible_name_repair_backup_v1'
_SOURCE_LOG_ID = 8994
_MISSING_AWARD_ADVENTURER_ID = 70
_MISSING_AWARD_OLD_NAME = 'recorded_crystal_ward_jamming_data'
_REPAIR_RELATED_ID = 'visible-name-v1:8994:70'
_PROTOCOL_TRAIT_NAMES = frozenset({
    'removed_traits',
    'used_skills',
    'history_story_append',
    'lore_append',
})

# 只包含当前资产或仍在托管中的资产。历史流水和已完成交易快照不做改写。
_CURRENT_NAME_TABLES = (
    (AdventurerS1Item, 'name', 'catalog', None),
    (AdventurerS1Equipment, 'name', 'equipment', None),
    (AdventurerS1WarehouseItem, 'name', 'warehouse', None),
    (AdventurerS1MarketListing, 'item_name', 'active_market_listings', ('status', 'active')),
    (AdventurerS1MarketOffer, 'offered_item_name', 'pending_market_offers', ('status', 'pending')),
    (AdventurerS1MarketBuyOffer, 'item_name', 'pending_market_buy_offers', ('status', 'pending')),
    (AdventurerS1SecretAuctionLot, 'item_name', 'open_auction_lots', ('status', 'open')),
)

_SCAN_NAME_TABLES = _CURRENT_NAME_TABLES + (
    (AdventurerS1Trait, 'name', 'traits', None),
    (AdventurerS1Skill, 'name', 'skills', None),
    (AdventurerS1ItemLog, 'item_name', 'historical_item_logs', None),
)
_HISTORICAL_SECTIONS = frozenset({
    'historical_item_logs',
    'historical_expedition_logs',
})


def _query_for(model, column_name: str, status_filter):
    query = model.query
    if status_filter:
        status_column, status_value = status_filter
        query = query.filter(getattr(model, status_column) == status_value)
    return query, getattr(model, column_name)


def _serialize_row(row) -> dict:
    payload = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            value = {'__datetime__': value.isoformat()}
        payload[column.name] = value
    return payload


def _deserialize_value(value):
    if isinstance(value, dict) and set(value) == {'__datetime__'}:
        return datetime.fromisoformat(value['__datetime__'])
    return value


class S1VisibleNameRepairService:
    """扫描并修复已确认的内部标识名称污染，可从备份恢复。"""

    @staticmethod
    def _backup_record():
        return AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()

    @classmethod
    def _backup_state(cls) -> str | None:
        rec = cls._backup_record()
        if not rec or not rec.value:
            return None
        try:
            return str((json.loads(rec.value) or {}).get('state') or '')
        except (TypeError, json.JSONDecodeError):
            return 'corrupt'

    @classmethod
    def _upgrade_applied_backup_asset_snapshots(
        cls,
        updated_by_user_id: Optional[int],
    ) -> bool:
        """为早期已执行批次补齐恢复所需资产快照；不改任何玩家资产。"""
        rec = cls._backup_record()
        if not rec or not rec.value:
            return False
        backup = json.loads(rec.value)
        granted = backup.get('granted_award') or {}
        if not granted or (
            'equipment_rows' in granted and 'warehouse_rows' in granted
        ):
            return False
        with db_manager.transaction():
            granted['equipment_rows'] = [
                _serialize_row(row)
                for row_id in granted.get('equipment_ids') or []
                if (row := db.session.get(AdventurerS1Equipment, int(row_id))) is not None
            ]
            granted['warehouse_rows'] = [
                _serialize_row(row)
                for row_id in granted.get('warehouse_ids') or []
                if (row := db.session.get(AdventurerS1WarehouseItem, int(row_id))) is not None
            ]
            backup['granted_award'] = granted
            rec.value = json.dumps(backup, ensure_ascii=False)
            rec.updated_by_user_id = updated_by_user_id
        return True

    @staticmethod
    def _scan_breakdown() -> tuple[dict, list[dict]]:
        breakdown: dict[str, int] = {}
        findings: list[dict] = []
        for model, column_name, section, status_filter in _SCAN_NAME_TABLES:
            query, column = _query_for(model, column_name, status_filter)
            rows = (
                query.with_entities(column, func.count(model.id))
                .filter(column.isnot(None))
                .group_by(column)
                .all()
            )
            polluted = [
                (str(name), int(count))
                for name, count in rows
                if is_internal_code_identifier_name(name)
            ]
            breakdown[section] = sum(count for _name, count in polluted)
            findings.extend({
                'section': section,
                'name': name,
                'count': count,
                'mapped_name': LEGACY_VISIBLE_NAME_ALIASES.get(name),
                'repairable': (
                    name in LEGACY_VISIBLE_NAME_ALIASES
                    or (section == 'traits' and name in _PROTOCOL_TRAIT_NAMES)
                ),
            } for name, count in polluted)

        log_occurrences: dict[tuple[str, str], int] = defaultdict(int)
        log_samples: dict[tuple[str, str], list[int]] = defaultdict(list)
        log_rows = (
            AdventurerS1ExpeditionLog.query
            .with_entities(AdventurerS1ExpeditionLog.id, AdventurerS1ExpeditionLog.full_log)
            .yield_per(200)
        )
        for log_id, full_log in log_rows:
            if not isinstance(full_log, dict):
                continue
            for update in full_log.get('member_updates') or []:
                if not isinstance(update, dict):
                    continue
                named_values = []
                for field_name in ('new_equipments', 'new_traits'):
                    value = update.get(field_name)
                    if isinstance(value, dict):
                        named_values.extend((field_name, name) for name in value)
                    elif isinstance(value, list):
                        named_values.extend(
                            (field_name, item.get('name'))
                            for item in value
                            if isinstance(item, dict)
                        )
                skills = update.get('new_skills')
                if isinstance(skills, list):
                    named_values.extend(
                        ('new_skills', item.get('name') if isinstance(item, dict) else item)
                        for item in skills
                    )
                for field_name, raw_name in named_values:
                    if not is_internal_code_identifier_name(raw_name):
                        continue
                    name = str(raw_name).strip()
                    key = (field_name, name)
                    log_occurrences[key] += 1
                    if len(log_samples[key]) < 20 and int(log_id) not in log_samples[key]:
                        log_samples[key].append(int(log_id))
        breakdown['historical_expedition_logs'] = sum(log_occurrences.values())
        findings.extend({
            'section': 'historical_expedition_logs',
            'field': field_name,
            'name': name,
            'count': count,
            'sample_log_ids': log_samples[(field_name, name)],
            'mapped_name': LEGACY_VISIBLE_NAME_ALIASES.get(name),
            'display_resolution': (
                'exact_alias'
                if field_name == 'new_equipments'
                and name in LEGACY_VISIBLE_NAME_ALIASES
                else 'omit_protocol_pollution'
                if field_name in {'new_traits', 'new_skills'}
                else None
            ),
            'repairable': (
                (
                    field_name == 'new_equipments'
                    and name in LEGACY_VISIBLE_NAME_ALIASES
                )
                or field_name in {'new_traits', 'new_skills'}
            ),
        } for (field_name, name), count in sorted(log_occurrences.items()))
        return breakdown, findings

    @staticmethod
    def _catalog_conflicts() -> list[dict]:
        conflicts = []
        for old_name, new_name in LEGACY_VISIBLE_NAME_ALIASES.items():
            old_row = AdventurerS1Item.query.filter_by(name=old_name).first()
            new_row = AdventurerS1Item.query.filter_by(name=new_name).first()
            if old_row and new_row:
                conflicts.append({
                    'old_name': old_name,
                    'new_name': new_name,
                    'old_id': old_row.id,
                    'new_id': new_row.id,
                })
        return conflicts

    @staticmethod
    def _find_missing_award() -> dict:
        old_name = _MISSING_AWARD_OLD_NAME
        new_name = LEGACY_VISIBLE_NAME_ALIASES[old_name]
        repaired_log = AdventurerS1ItemLog.query.filter_by(
            adventurer_id=_MISSING_AWARD_ADVENTURER_ID,
            related_id=_REPAIR_RELATED_ID,
            related_type='visible_name_repair',
        ).first()
        if repaired_log:
            return {'status': 'already_repaired', 'item_name': new_name}

        held_count = (
            AdventurerS1Equipment.query.filter(
                AdventurerS1Equipment.adventurer_id == _MISSING_AWARD_ADVENTURER_ID,
                AdventurerS1Equipment.name.in_((old_name, new_name)),
            ).count()
            + AdventurerS1WarehouseItem.query.filter(
                AdventurerS1WarehouseItem.adventurer_id == _MISSING_AWARD_ADVENTURER_ID,
                AdventurerS1WarehouseItem.name.in_((old_name, new_name)),
            ).count()
        )
        if held_count:
            return {'status': 'currently_held', 'item_name': new_name}

        log = db.session.get(AdventurerS1ExpeditionLog, _SOURCE_LOG_ID)
        full_log = log.full_log if log and isinstance(log.full_log, dict) else {}
        receipt = full_log.get('settlement_receipt') or {}
        receipt_member = next(
            (
                row for row in receipt.get('members') or []
                if isinstance(row, dict)
                and int(row.get('adventurer_id') or 0) == _MISSING_AWARD_ADVENTURER_ID
            ),
            None,
        )
        update = next(
            (
                row for row in full_log.get('member_updates') or []
                if isinstance(row, dict)
                and int(row.get('id') or 0) == _MISSING_AWARD_ADVENTURER_ID
            ),
            None,
        )
        item_data = (
            (update.get('new_equipments') or {}).get(old_name)
            if isinstance(update, dict)
            and isinstance(update.get('new_equipments'), dict)
            else None
        )
        checked = receipt_member.get('checked') or [] if receipt_member else []
        evidence_ok = (
            receipt.get('stage') == 'member_update_flush'
            and receipt.get('status') == 'passed'
            and receipt_member is not None
            and receipt_member.get('status') == 'passed'
            and f'item:{old_name}' in checked
            and isinstance(item_data, dict)
        )
        if not evidence_ok:
            return {'status': 'evidence_incomplete', 'item_name': new_name}
        return {
            'status': 'missing_confirmed',
            'item_name': new_name,
            'adventurer_id': _MISSING_AWARD_ADVENTURER_ID,
            'source_log_id': _SOURCE_LOG_ID,
            'item_data': item_data,
        }

    @classmethod
    def preview(cls) -> Tuple[Optional[dict], Optional[str]]:
        """dry-run：扫描所有当前名称表和历史证据表，不写库。"""
        try:
            breakdown, findings = cls._scan_breakdown()
            conflicts = cls._catalog_conflicts()
            missing_award = cls._find_missing_award()
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 玩家可见名称污染 preview 失败')
            return None, f'预览失败：{exc}'
        return {
            'breakdown': breakdown,
            'findings': findings,
            'total': sum(breakdown.values()),
            'current_total': sum(
                count for section, count in breakdown.items()
                if section not in _HISTORICAL_SECTIONS
            ),
            'historical_total': sum(
                count for section, count in breakdown.items()
                if section in _HISTORICAL_SECTIONS
            ),
            'catalog_conflicts': conflicts,
            'missing_award': missing_award,
            'backup_state': cls._backup_state(),
            'historical_evidence_policy': 'preserved_with_exact_display_alias',
        }, None

    @classmethod
    def repair(
        cls,
        updated_by_user_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """修复当前资产、移除协议字段名特质并补回已确认漏发物品。"""
        state = cls._backup_state()
        if state == 'applied':
            backup_upgraded = cls._upgrade_applied_backup_asset_snapshots(
                updated_by_user_id,
            )
            return {
                'already_applied': True,
                'backup_state': state,
                'backup_upgraded': backup_upgraded,
                'renamed': 0,
                'traits_removed': 0,
                'award_granted': False,
            }, None
        if state == 'corrupt':
            return None, '现有修复备份已损坏，请先人工核对'
        conflicts = cls._catalog_conflicts()
        if conflicts:
            return None, f'目标图鉴名已存在，拒绝自动合并：{conflicts}'

        backup = {
            'version': 1,
            'state': 'applied',
            'created_at': _portable_utc_now().isoformat() + 'Z',
            'renamed': {},
            'deleted_traits': [],
            'granted_award': None,
        }
        try:
            with db_manager.transaction():
                renamed_count = 0
                old_names = tuple(LEGACY_VISIBLE_NAME_ALIASES)
                for model, column_name, section, status_filter in _CURRENT_NAME_TABLES:
                    query, column = _query_for(model, column_name, status_filter)
                    rows = query.filter(column.in_(old_names)).with_for_update().all()
                    section_backup = []
                    for row in rows:
                        old_name = str(getattr(row, column_name))
                        new_name = LEGACY_VISIBLE_NAME_ALIASES[old_name]
                        section_backup.append({
                            'id': int(row.id),
                            'old_name': old_name,
                            'new_name': new_name,
                        })
                        setattr(row, column_name, new_name)
                    backup['renamed'][section] = section_backup
                    renamed_count += len(section_backup)

                traits = (
                    AdventurerS1Trait.query
                    .filter(AdventurerS1Trait.name.in_(tuple(_PROTOCOL_TRAIT_NAMES)))
                    .with_for_update()
                    .all()
                )
                backup['deleted_traits'] = [_serialize_row(row) for row in traits]
                for row in traits:
                    db.session.delete(row)

                missing_award = cls._find_missing_award()
                award_granted = False
                if missing_award.get('status') == 'missing_confirmed':
                    adv = db.session.get(
                        AdventurerS1,
                        _MISSING_AWARD_ADVENTURER_ID,
                    )
                    if adv is None:
                        raise ValueError('漏发物品目标冒险者不存在')
                    item_data = dict(missing_award['item_data'])
                    old_desc = item_data.get('desc') or item_data.get('description') or ''
                    before_equipment_ids = {
                        int(row_id) for (row_id,) in
                        AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.id)
                        .filter_by(adventurer_id=adv.id).all()
                    }
                    before_warehouse_ids = {
                        int(row_id) for (row_id,) in
                        AdventurerS1WarehouseItem.query.with_entities(AdventurerS1WarehouseItem.id)
                        .filter_by(adventurer_id=adv.id).all()
                    }
                    destination, grant_error = grant_raw_item(
                        adv,
                        LEGACY_VISIBLE_NAME_ALIASES[_MISSING_AWARD_OLD_NAME],
                        item_data,
                        source_type='admin',
                        description='日志 #8994 结算收据确认漏发，管理员审计补回',
                        register_catalog=False,
                        related_id=_REPAIR_RELATED_ID,
                        related_type='visible_name_repair',
                    )
                    if grant_error:
                        raise ValueError(f'漏发物品补回失败：{grant_error}')
                    db_manager.flush()
                    equipment_ids = [
                        int(row_id) for (row_id,) in
                        AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.id)
                        .filter_by(
                            adventurer_id=adv.id,
                            name=LEGACY_VISIBLE_NAME_ALIASES[_MISSING_AWARD_OLD_NAME],
                        ).all()
                        if int(row_id) not in before_equipment_ids
                    ]
                    warehouse_ids = [
                        int(row_id) for (row_id,) in
                        AdventurerS1WarehouseItem.query.with_entities(AdventurerS1WarehouseItem.id)
                        .filter_by(
                            adventurer_id=adv.id,
                            name=LEGACY_VISIBLE_NAME_ALIASES[_MISSING_AWARD_OLD_NAME],
                        ).all()
                        if int(row_id) not in before_warehouse_ids
                    ]
                    equipment_rows = [
                        _serialize_row(db.session.get(AdventurerS1Equipment, row_id))
                        for row_id in equipment_ids
                    ]
                    warehouse_rows = [
                        _serialize_row(db.session.get(AdventurerS1WarehouseItem, row_id))
                        for row_id in warehouse_ids
                    ]
                    grant_log = AdventurerS1ItemLog.query.filter_by(
                        adventurer_id=adv.id,
                        related_id=_REPAIR_RELATED_ID,
                        related_type='visible_name_repair',
                    ).order_by(AdventurerS1ItemLog.id.desc()).first()
                    backup['granted_award'] = {
                        'adventurer_id': adv.id,
                        'destination': destination,
                        'equipment_ids': equipment_ids,
                        'warehouse_ids': warehouse_ids,
                        'equipment_rows': equipment_rows,
                        'warehouse_rows': warehouse_rows,
                        'item_log_id': int(grant_log.id) if grant_log else None,
                        'item_name': LEGACY_VISIBLE_NAME_ALIASES[_MISSING_AWARD_OLD_NAME],
                        'description': old_desc,
                    }
                    award_granted = True

                payload = json.dumps(backup, ensure_ascii=False)
                rec = cls._backup_record()
                if rec is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=payload,
                        description='玩家可见内部标识污染修复备份（当前资产、脏特质、漏发补回）',
                        updated_by_user_id=updated_by_user_id,
                    ))
                else:
                    rec.value = payload
                    rec.description = '玩家可见内部标识污染修复备份（当前资产、脏特质、漏发补回）'
                    rec.updated_by_user_id = updated_by_user_id
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 玩家可见名称污染修复失败')
            return None, f'修复失败：{exc}'

        return {
            'already_applied': False,
            'renamed': renamed_count,
            'traits_removed': len(backup['deleted_traits']),
            'award_granted': award_granted,
            'historical_evidence_preserved': True,
        }, None

    @classmethod
    def restore(
        cls,
        updated_by_user_id: Optional[int] = None,
    ) -> Tuple[Optional[dict], Optional[str]]:
        """按最近备份恢复当前名称和脏特质，并撤销仍可精确定位的补发。"""
        rec = cls._backup_record()
        if not rec or not rec.value:
            return None, '没有可用的修复备份'
        try:
            backup = json.loads(rec.value)
        except (TypeError, json.JSONDecodeError):
            return None, '备份数据已损坏，无法还原'
        if backup.get('state') == 'restored':
            return {
                'already_restored': True,
                'renamed_restored': 0,
                'traits_restored': 0,
                'award_rows_removed': 0,
            }, None
        if backup.get('state') != 'applied':
            return None, '备份状态不可恢复'

        model_by_section = {
            section: (model, column_name)
            for model, column_name, section, _status_filter in _CURRENT_NAME_TABLES
        }
        try:
            with db_manager.transaction():
                renamed_restored = 0
                for section, entries in (backup.get('renamed') or {}).items():
                    model_info = model_by_section.get(section)
                    if not model_info:
                        continue
                    model, column_name = model_info
                    for entry in entries or []:
                        row = db.session.get(model, int(entry['id']))
                        if row is None:
                            continue
                        if getattr(row, column_name) != entry['new_name']:
                            continue
                        setattr(row, column_name, entry['old_name'])
                        renamed_restored += 1

                traits_restored = 0
                for snapshot in backup.get('deleted_traits') or []:
                    trait_id = int(snapshot['id'])
                    if db.session.get(AdventurerS1Trait, trait_id) is not None:
                        continue
                    values = {
                        key: _deserialize_value(value)
                        for key, value in snapshot.items()
                    }
                    db_manager.add(AdventurerS1Trait(**values))
                    traits_restored += 1

                award_rows_removed = 0
                granted = backup.get('granted_award') or {}
                for model, key, snapshot_key in (
                    (AdventurerS1Equipment, 'equipment_ids', 'equipment_rows'),
                    (AdventurerS1WarehouseItem, 'warehouse_ids', 'warehouse_rows'),
                ):
                    snapshots = {
                        int(snapshot['id']): snapshot
                        for snapshot in granted.get(snapshot_key) or []
                    }
                    for row_id in granted.get(key) or []:
                        row = db.session.get(model, int(row_id))
                        if row is None or row.name != granted.get('item_name'):
                            continue
                        snapshot = snapshots.get(int(row_id))
                        if snapshot and any(
                            getattr(row, field) != _deserialize_value(snapshot.get(field))
                            for field in (
                                'name', 'type', 'rarity', 'custom_desc',
                                'uses_left', 'quantity', 'evolution_stage',
                            )
                            if field in snapshot
                        ):
                            continue
                        db.session.delete(row)
                        award_rows_removed += 1
                item_log_id = granted.get('item_log_id')
                if item_log_id:
                    item_log = db.session.get(AdventurerS1ItemLog, int(item_log_id))
                    if (
                        item_log is not None
                        and item_log.related_id == _REPAIR_RELATED_ID
                        and item_log.related_type == 'visible_name_repair'
                    ):
                        db.session.delete(item_log)

                backup['state'] = 'restored'
                backup['restored_at'] = _portable_utc_now().isoformat() + 'Z'
                rec.value = json.dumps(backup, ensure_ascii=False)
                rec.updated_by_user_id = updated_by_user_id
        except Exception as exc:  # noqa: BLE001
            current_app.logger.exception('S1 玩家可见名称污染恢复失败')
            return None, f'还原失败：{exc}'

        return {
            'already_restored': False,
            'renamed_restored': renamed_restored,
            'traits_restored': traits_restored,
            'award_rows_removed': award_rows_removed,
        }, None
