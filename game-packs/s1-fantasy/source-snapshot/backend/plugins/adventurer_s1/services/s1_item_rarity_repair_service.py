"""S1 非法稀有度 ``uncommon`` 批量修复（图鉴 + 已入库实例 + 关联流水）。

历史数据：部分图鉴/实例使用了运行时四档之外的 ``uncommon``，导致申诉补发、
收购商行、交易市场等模块校验失败。本服务把已入库的 ``uncommon`` 统一改为 ``rare``
（与探险结算 ``repair_illegal_rarity(uncommon→rare)`` 口径一致）。

- :meth:`preview`  —— dry-run，统计受影响行数，不写库。
- :meth:`repair`   —— 备份后批量改图鉴 + 装备/仓库实例 + 流水/挂单等 rarity 字段。
- :meth:`restore`  —— 从最近一次备份一键还原。

备份存 :class:`AdventurerS1Settings`（key=``item_rarity_repair_backup``）。
"""
from runtime_core.time import utc_now as _portable_utc_now
import json
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1ItemLog,
    AdventurerS1LuckyWheelSpin,
    AdventurerS1MarketListing,
    AdventurerS1MarketOffer,
    AdventurerS1MarketBuyOffer,
    AdventurerS1SecretAuctionLot,
    AdventurerS1Settings,
    AdventurerS1WarehouseItem,
)

_SOURCE_RARITY = 'uncommon'
_TARGET_RARITY = 'rare'
_BACKUP_KEY = 'item_rarity_repair_backup'

# (model, column_name, backup_section_key)
_RARITY_TABLES = (
    (AdventurerS1Item, 'rarity', 'catalog'),
    (AdventurerS1Equipment, 'rarity', 'equipment'),
    (AdventurerS1WarehouseItem, 'rarity', 'warehouse'),
    (AdventurerS1ItemLog, 'item_rarity', 'item_logs'),
    (AdventurerS1MarketListing, 'rarity', 'market_listings'),
    (AdventurerS1MarketOffer, 'offered_rarity', 'market_offers'),
    (AdventurerS1MarketBuyOffer, 'rarity', 'market_buy_offers'),
    (AdventurerS1SecretAuctionLot, 'rarity', 'secret_auction_lots'),
    (AdventurerS1LuckyWheelSpin, 'item_rarity', 'lucky_wheel_spins'),
)


class S1ItemRarityRepairService:
    """已入库 ``uncommon`` → ``rare`` 批量修复，可备份还原。"""

    @staticmethod
    def _count_rows(model, column_name: str) -> int:
        col = getattr(model, column_name)
        return int(model.query.filter(col == _SOURCE_RARITY).count())

    @staticmethod
    def _fetch_ids(model, column_name: str) -> list[int]:
        col = getattr(model, column_name)
        return [int(rid) for (rid,) in model.query.with_entities(model.id).filter(col == _SOURCE_RARITY).all()]

    @classmethod
    def _has_backup(cls) -> bool:
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        return bool(rec and rec.value)

    @classmethod
    def preview(cls) -> Tuple[Optional[dict], Optional[str]]:
        """dry-run：统计各表 uncommon 行数，不写库。"""
        try:
            breakdown = {}
            total = 0
            catalog_names: list[str] = []
            for model, column_name, section in _RARITY_TABLES:
                count = cls._count_rows(model, column_name)
                breakdown[section] = count
                total += count
                if section == 'catalog' and count:
                    catalog_names = [
                        str(name)
                        for (name,) in (
                            AdventurerS1Item.query.with_entities(AdventurerS1Item.name)
                            .filter(AdventurerS1Item.rarity == _SOURCE_RARITY)
                            .order_by(AdventurerS1Item.name)
                            .all()
                        )
                    ]
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 稀有度修复 preview 失败')
            return None, f'预览失败：{e}'
        return {
            'source_rarity': _SOURCE_RARITY,
            'target_rarity': _TARGET_RARITY,
            'breakdown': breakdown,
            'total': total,
            'catalog_names': catalog_names,
            'has_backup': cls._has_backup(),
        }, None

    @classmethod
    def repair(cls, updated_by_user_id: Optional[int] = None) -> Tuple[Optional[dict], Optional[str]]:
        """把各表 uncommon 改为 rare；先备份可还原。"""
        preview, err = cls.preview()
        if err:
            return None, err
        if not preview or int(preview.get('total') or 0) <= 0:
            return {
                'repaired': 0,
                'breakdown': preview.get('breakdown') if preview else {},
                'target_rarity': _TARGET_RARITY,
            }, None

        backup = {
            'created_at': _portable_utc_now().isoformat() + 'Z',
            'source_rarity': _SOURCE_RARITY,
            'target_rarity': _TARGET_RARITY,
        }
        repaired_breakdown: dict[str, int] = {}

        try:
            with db_manager.transaction():
                for model, column_name, section in _RARITY_TABLES:
                    ids = cls._fetch_ids(model, column_name)
                    backup[section] = [{'id': rid, 'old_rarity': _SOURCE_RARITY} for rid in ids]
                    if ids:
                        col = getattr(model, column_name)
                        model.query.filter(model.id.in_(ids)).update(
                            {col: _TARGET_RARITY}, synchronize_session=False,
                        )
                    repaired_breakdown[section] = len(ids)

                payload = json.dumps(backup, ensure_ascii=False)
                rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if rec is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=payload,
                        description='uncommon→rare 稀有度修复前备份（图鉴+实例+流水，供一键还原）',
                        updated_by_user_id=updated_by_user_id,
                    ))
                else:
                    rec.value = payload
                    rec.updated_by_user_id = updated_by_user_id
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 稀有度修复写库失败')
            return None, f'修复失败：{e}'

        total = sum(repaired_breakdown.values())
        current_app.logger.info(
            'S1 稀有度修复完成：uncommon→%s 共 %s 行 %s',
            _TARGET_RARITY, total, repaired_breakdown,
        )
        return {
            'repaired': total,
            'breakdown': repaired_breakdown,
            'target_rarity': _TARGET_RARITY,
        }, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        """从最近一次备份还原 rarity。"""
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not rec or not rec.value:
            return None, '没有可用的稀有度修复备份'
        try:
            payload = json.loads(rec.value)
        except (json.JSONDecodeError, TypeError):
            return None, '备份数据已损坏，无法还原'

        old_rarity = str(payload.get('source_rarity') or _SOURCE_RARITY)
        section_map = {section: (model, column) for model, column, section in _RARITY_TABLES}

        try:
            with db_manager.transaction():
                restored = 0
                restored_breakdown: dict[str, int] = {}
                for section, (model, column_name) in section_map.items():
                    entries = payload.get(section) or []
                    ids = [
                        int(e['id'])
                        for e in entries
                        if isinstance(e, dict) and e.get('id') is not None
                    ]
                    if ids:
                        col = getattr(model, column_name)
                        model.query.filter(model.id.in_(ids)).update(
                            {col: old_rarity}, synchronize_session=False,
                        )
                    restored_breakdown[section] = len(ids)
                    restored += len(ids)
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 稀有度还原失败')
            return None, f'还原失败：{e}'

        current_app.logger.info('S1 稀有度还原完成：回退 %s 行', restored)
        return {
            'restored': restored,
            'breakdown': restored_breakdown,
            'backup_at': payload.get('created_at'),
        }, None
