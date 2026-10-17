"""S1 误标 consumable 的「可复用工具」修复。

历史 bug：可反复查阅/长期使用的工具（地图、星图、钥匙、罗盘、典籍等）被错误归类为
``consumable``。探险结算 ``apply_update_to_s1_adventurer`` 里，凡 ``type=='consumable'``
的物品一旦被主 AI 写进 ``used_equipments``（剧情"使用/翻阅"），就会被 ``_consume_once``
当一次性整删——导致玩家"没描述消耗、结算没去除、出本后却没了"。

消耗判定读的是**实例 type**（``e.type``），不是图鉴 type，所以只改图鉴不保护存量实例；
本服务把**图鉴条目 + 其全部随身/仓库实例**的 ``type`` 一起改为 ``document``（非 consumable，
不再被 used 路径自动消耗）。

- :meth:`preview`  —— dry-run，列出候选图鉴条目与受影响实例数，不写库。
- :meth:`repair`   —— 先把旧 type 备份到 settings，再批量改图鉴 + 实例 type。
- :meth:`restore`  —— 从最近一次备份回退 type，撤销修复。

备份存 :class:`AdventurerS1Settings`（key=``item_type_repair_backup``）——不新建表。
关键词保守选取，避开真消耗品（卷轴/火把/药水）；repair 前 preview 供管理员核对名单。
"""
from runtime_core.time import utc_now as _portable_utc_now
import json
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1Settings,
    AdventurerS1WarehouseItem,
)

# 高置信度「可复用工具」关键词：被误标 consumable 时，探险"使用"会被整删。
# 谨慎选词，避开真消耗品（卷轴/火把/药水/箭矢等不在内）。
_REUSABLE_TOOL_KEYWORDS = (
    '地图', '星图', '坐标图', '路线图', '航海图', '海图', '航线图',
    '罗盘', '指南针', '钥匙', '典籍', '秘籍', '手稿', '图鉴', '手册', '卷宗',
)
_TARGET_TYPE = 'document'
_BACKUP_KEY = 'item_type_repair_backup'


class S1ItemTypeRepairService:
    """误标 consumable 的可复用工具：图鉴 + 实例 type 批量改 document，可备份还原。"""

    @staticmethod
    def _matched_catalog_items():
        """type='consumable' 且名字含可复用工具关键词的图鉴条目（ORM 列表）。"""
        conds = [AdventurerS1Item.name.like(f'%{kw}%') for kw in _REUSABLE_TOOL_KEYWORDS]
        return (
            AdventurerS1Item.query
            .filter(AdventurerS1Item.type == 'consumable')
            .filter(or_(*conds))
            .order_by(AdventurerS1Item.name)
            .all()
        )

    @staticmethod
    def _count_instances(names: list) -> dict:
        """批量统计候选物品当前 type=consumable 的随身+仓库实例数（避免 N+1）。"""
        counts = {n: 0 for n in names}
        if not names:
            return counts
        for model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
            rows = (
                model.query.with_entities(model.name)
                .filter(model.name.in_(names), model.type == 'consumable')
                .all()
            )
            for (nm,) in rows:
                counts[nm] = counts.get(nm, 0) + 1
        return counts

    @staticmethod
    def _has_backup() -> bool:
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        return bool(rec and rec.value)

    @classmethod
    def preview(cls) -> Tuple[Optional[dict], Optional[str]]:
        """dry-run：列出候选图鉴条目与受影响实例数，不写库。"""
        try:
            items = cls._matched_catalog_items()
            names = [it.name for it in items]
            inst_counts = cls._count_instances(names)
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 物品类型修复 preview 失败')
            return None, f'预览失败：{e}'
        candidates = [{
            'id': it.id,
            'name': it.name,
            'rarity': it.rarity,
            'current_type': it.type,
            'instance_count': inst_counts.get(it.name, 0),
        } for it in items]
        return {
            'candidates': candidates,
            'total': len(candidates),
            'instance_total': sum(inst_counts.values()),
            'target_type': _TARGET_TYPE,
            'has_backup': cls._has_backup(),
        }, None

    @classmethod
    def repair(cls, updated_by_user_id: Optional[int] = None) -> Tuple[Optional[dict], Optional[str]]:
        """把候选图鉴条目及其全部 consumable 实例 type 改为 document；先备份可还原。"""
        try:
            items = cls._matched_catalog_items()
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 物品类型修复扫描失败')
            return None, f'修复失败：{e}'
        if not items:
            return {'repaired': 0, 'instances_updated': 0, 'names': []}, None

        names = [it.name for it in items]
        backup = {
            'created_at': _portable_utc_now().isoformat() + 'Z',
            'target_type': _TARGET_TYPE,
            'catalog': [{'id': it.id, 'old_type': it.type} for it in items],
            'equipment': [],
            'warehouse': [],
        }
        try:
            with db_manager.transaction():
                inst_updated = 0
                for model, key in (
                    (AdventurerS1Equipment, 'equipment'),
                    (AdventurerS1WarehouseItem, 'warehouse'),
                ):
                    rows = (
                        model.query.with_entities(model.id, model.type)
                        .filter(model.name.in_(names), model.type == 'consumable')
                        .all()
                    )
                    ids = [rid for rid, _t in rows]
                    backup[key] = [{'id': rid, 'old_type': t} for rid, t in rows]
                    if ids:
                        model.query.filter(model.id.in_(ids)).update(
                            {model.type: _TARGET_TYPE}, synchronize_session=False,
                        )
                        inst_updated += len(ids)
                for it in items:
                    it.type = _TARGET_TYPE
                payload = json.dumps(backup, ensure_ascii=False)
                rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if rec is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=payload,
                        description='可复用工具误标 consumable 修复前备份（图鉴+实例 type，供一键还原）',
                        updated_by_user_id=updated_by_user_id,
                    ))
                else:
                    rec.value = payload
                    rec.updated_by_user_id = updated_by_user_id
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 物品类型修复写库失败')
            return None, f'修复失败：{e}'

        current_app.logger.info(
            'S1 物品类型修复完成：图鉴 %s 条、实例 %s 件 改为 %s',
            len(items), inst_updated, _TARGET_TYPE,
        )
        return {
            'repaired': len(items),
            'instances_updated': inst_updated,
            'names': names,
        }, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        """从最近一次备份回退图鉴 + 实例 type。"""
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not rec or not rec.value:
            return None, '没有可用的修复备份'
        try:
            payload = json.loads(rec.value)
        except (json.JSONDecodeError, TypeError):
            return None, '备份数据已损坏，无法还原'

        sections = (
            (AdventurerS1Item, payload.get('catalog') or []),
            (AdventurerS1Equipment, payload.get('equipment') or []),
            (AdventurerS1WarehouseItem, payload.get('warehouse') or []),
        )
        try:
            with db_manager.transaction():
                restored = 0
                for model, entries in sections:
                    by_old: dict = {}
                    for e in entries:
                        if isinstance(e, dict) and 'id' in e and 'old_type' in e:
                            by_old.setdefault(e['old_type'], []).append(int(e['id']))
                    for old_type, ids in by_old.items():
                        if ids:
                            model.query.filter(model.id.in_(ids)).update(
                                {model.type: old_type}, synchronize_session=False,
                            )
                            restored += len(ids)
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 物品类型还原失败')
            return None, f'还原失败：{e}'

        current_app.logger.info('S1 物品类型还原完成：回退 %s 行', restored)
        return {
            'restored': restored,
            'backup_at': payload.get('created_at'),
        }, None
