"""深渊酒馆 S1 传记代号修复服务。

历史 bug（``_restore_codenames_in_result`` 漏处理 ``history_story_append``）导致部分
冒险者 ``history_story`` 残留探险代号未还原成真名。本服务用每次探险
日志自带的 ``audit_member_snapshot``（记录了该次 ``{代号 -> adventurer_id}``）做**精确**
还原：代号按队伍成员顺序分配，snapshot 与之同序，因此多人队伍里的代号能对应到当次
正确的队友（而非传记主人自己）。

- :meth:`preview`  —— dry-run，统计可精确还原 / 仍残留的数量，不写库。
- :meth:`repair`   —— 先把受影响冒险者的 ``history_story`` 原文备份到 settings，再逐段
  精确替换。对应不上现存日志的段落保持原样残留（方案：仅精确重建）。
- :meth:`restore`  —— 从最近一次备份一键回写，撤销修复。

备份存放在 :class:`AdventurerS1Settings`（key=``codename_repair_backup``，value 为
JSON）——不新建表，便于「小幅度临时备份 + 出问题再还原」。
"""
from runtime_core.time import utc_now as _portable_utc_now
import json
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Optional, Tuple

from flask import current_app
from sqlalchemy import String, cast, or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1Settings,
)
from plugins.adventurer_s1.services.adventurer_s1_prompt_service import (
    RESIDUAL_CODENAME_PATTERN,
    build_codename_to_real_from_snapshot,
)

_CODENAME_RE = RESIDUAL_CODENAME_PATTERN
_AFFECTED_FILTER = (
    r'(?:【M[0-9]+】|冒险者[甲乙丙丁戊己庚辛壬癸]|[^A-Za-z【]M[0-9]+([^0-9]|$))'
)  # PG 正则（~）用
_BACKUP_KEY = 'codename_repair_backup'


class AdventurerS1CodenameRepairService:
    """传记残留代号的精确还原 + 备份 / 还原。"""

    @staticmethod
    def _load_affected_adventurers():
        """返回 history_story 仍含探险代号的不可变扫描快照。"""
        rows = (
            AdventurerS1.query
            .with_entities(AdventurerS1.id, AdventurerS1.name, AdventurerS1.history_story)
            .filter(AdventurerS1.history_story.op('~')(_AFFECTED_FILTER))
            .all()
        )
        db_manager.rollback()
        return [
            SimpleNamespace(id=int(row.id), name=str(row.name or ''), history_story=row.history_story)
            for row in rows
        ]

    @staticmethod
    def _build_expedition_log_replacement_query(*, affected_adv_ids: set[int]):
        """只拉可能含 biography append + 残留代号的 completed 日志。"""
        full_log_text = cast(AdventurerS1ExpeditionLog.full_log, String)
        q = (
            AdventurerS1ExpeditionLog.query
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.full_log,
            )
            .filter(
                AdventurerS1ExpeditionLog.status == 'completed',
                full_log_text.contains('"history_story_append"'),
                full_log_text.op('~')(_AFFECTED_FILTER),
            )
        )
        if affected_adv_ids and len(affected_adv_ids) <= 100:
            id_filters = []
            for adv_id in affected_adv_ids:
                id_filters.append(full_log_text.contains(f'"id": {adv_id}'))
                id_filters.append(full_log_text.contains(f'"id":{adv_id}'))
            q = q.filter(or_(*id_filters))
        return q

    @staticmethod
    def _build_replacements_by_adv(affected_adv_ids: set[int] | None = None) -> dict:
        """扫描相关 completed 日志，按冒险者构建 ``{adv_id: [(dirty, clean), ...]}`` 替换对。

        每条日志独立用自身 ``audit_member_snapshot`` 还原代号；落库的
        ``history_story_append`` 与冒险者 ``history_story`` 里的段落逐字一致，故可用
        ``dirty`` 子串精确定位替换，避免不同探险的同名代号互相串味。
        """
        if affected_adv_ids is not None and not affected_adv_ids:
            return {}

        from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
            restore_names_in_narrative,
        )

        id_to_name = dict(
            AdventurerS1.query
            .with_entities(AdventurerS1.id, AdventurerS1.name)
            .all()
        )
        repl: dict[int, list] = {}
        seen: dict[int, set] = {}  # 去重：同一冒险者同一段 dirty 文本只记一次
        only_adv_ids = affected_adv_ids

        q = AdventurerS1CodenameRepairService._build_expedition_log_replacement_query(
            affected_adv_ids=only_adv_ids or set(),
        ).order_by(AdventurerS1ExpeditionLog.id.asc())

        def iter_log_rows():
            after_id = 0
            while True:
                rows = q.filter(AdventurerS1ExpeditionLog.id > after_id).limit(100).all()
                db_manager.rollback()
                if not rows:
                    return
                yield from rows
                after_id = int(rows[-1][0])

        for _log_id, full_log in iter_log_rows():
            if not isinstance(full_log, dict):
                continue
            snap = full_log.get('audit_member_snapshot')
            updates = full_log.get('member_updates')
            if not isinstance(snap, list) or not isinstance(updates, list):
                continue
            codename_to_real = build_codename_to_real_from_snapshot(snap, id_to_name)
            if not codename_to_real:
                continue
            for mu in updates:
                if not isinstance(mu, dict):
                    continue
                hsa = mu.get('history_story_append')
                adv_id = mu.get('id')
                if not isinstance(hsa, str) or not isinstance(adv_id, int):
                    continue
                if only_adv_ids is not None and adv_id not in only_adv_ids:
                    continue
                if not _CODENAME_RE.search(hsa):
                    continue
                clean = restore_names_in_narrative(hsa, codename_to_real)
                if clean == hsa:
                    continue
                key_set = seen.setdefault(adv_id, set())
                if hsa in key_set:
                    continue
                key_set.add(hsa)
                repl.setdefault(adv_id, []).append((hsa, clean))
        return repl

    @classmethod
    def _compute(cls):
        """核心计算（不写库）。

        返回 ``(advs, updates, residual_ids)``：
        - ``advs``：当前含代号的冒险者 ORM 列表；
        - ``updates``：``[(adv, original, new_story, seg_count)]`` 实际有改动的；
        - ``residual_ids``：替换后仍残留代号（对应不上日志）的冒险者 id 列表。
        """
        advs = cls._load_affected_adventurers()
        affected_adv_ids = {adv.id for adv in advs}
        repl = cls._build_replacements_by_adv(affected_adv_ids)
        updates = []
        residual = []
        for adv in advs:
            original = adv.history_story or ''
            new_story = original
            seg = 0
            for dirty, clean in repl.get(adv.id, []):
                if dirty in new_story:
                    new_story = new_story.replace(dirty, clean)
                    seg += 1
            if new_story != original:
                updates.append((adv, original, new_story, seg))
            if _CODENAME_RE.search(new_story):
                residual.append(adv.id)
        return advs, updates, residual

    @classmethod
    def preview(cls) -> Tuple[Optional[dict], Optional[str]]:
        """dry-run：返回将修复 / 仍残留的统计，不写库。"""
        try:
            advs, updates, residual = cls._compute()
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 传记代号修复 preview 失败')
            return None, f'预览失败：{e}'
        return {
            'affected_total': len(advs),
            'will_fix': len(updates),
            'residual_after': len(residual),
            'has_backup': cls._has_backup(),
            'samples': [
                {'id': adv.id, 'name': adv.name, 'segments': seg}
                for adv, _o, _n, seg in updates[:20]
            ],
        }, None

    @classmethod
    def repair(cls, updated_by_user_id: Optional[int] = None) -> Tuple[Optional[dict], Optional[str]]:
        """精确还原传记代号：先备份原文到 settings，再逐段替换写库。"""
        try:
            advs, updates, residual = cls._compute()
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 传记代号修复计算失败')
            return None, f'修复失败：{e}'

        if not updates:
            return {
                'will_fix': 0,
                'fixed': 0,
                'affected_total': len(advs),
                'residual_after': len(residual),
                'backup_count': 0,
            }, None

        applied_updates = []
        try:
            with db_manager.transaction():
                update_by_id = {
                    int(adv.id): (original, new_story, seg)
                    for adv, original, new_story, seg in updates
                }
                locked_adventurers = (
                    AdventurerS1.query
                    .filter(AdventurerS1.id.in_(list(update_by_id)))
                    .with_for_update()
                    .all()
                )
                for adv in locked_adventurers:
                    original, new_story, seg = update_by_id[int(adv.id)]
                    if (adv.history_story or '') != original:
                        continue
                    adv.history_story = new_story
                    applied_updates.append((int(adv.id), original, seg))

                if not applied_updates:
                    return {
                        'will_fix': len(updates),
                        'fixed': 0,
                        'affected_total': len(advs),
                        'residual_after': len(residual),
                        'backup_count': 0,
                    }, None

                backup_payload = json.dumps({
                    'created_at': _portable_utc_now().isoformat(),
                    'count': len(applied_updates),
                    'items': [
                        {'id': adv_id, 'before': original}
                        for adv_id, original, _seg in applied_updates
                    ],
                }, ensure_ascii=False)
                rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if rec is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=backup_payload,
                        description='传记代号修复前备份（history_story 原文，供一键还原）',
                        updated_by_user_id=updated_by_user_id,
                    ))
                else:
                    rec.value = backup_payload
                    rec.updated_by_user_id = updated_by_user_id
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 传记代号修复写库失败')
            return None, f'修复失败：{e}'

        current_app.logger.info(
            'S1 传记代号修复完成：修复 %s 名，备份 %s 条，仍残留 %s 名',
            len(applied_updates), len(applied_updates), len(residual),
        )
        return {
            'will_fix': len(updates),
            'fixed': len(applied_updates),
            'affected_total': len(advs),
            'residual_after': len(residual),
            'backup_count': len(applied_updates),
        }, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        """从最近一次修复备份一键回写 history_story。"""
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not rec or not rec.value:
            return None, '没有可用的修复备份'
        try:
            payload = json.loads(rec.value)
            items = payload.get('items') or []
        except (json.JSONDecodeError, TypeError):
            return None, '备份数据已损坏，无法还原'
        before_map = {
            int(it['id']): it['before']
            for it in items
            if isinstance(it, dict) and 'id' in it and 'before' in it
        }
        if not before_map:
            return None, '备份为空，无需还原'
        try:
            with db_manager.transaction():
                advs = (
                    AdventurerS1.query
                    .filter(AdventurerS1.id.in_(list(before_map.keys())))
                    .all()
                )
                restored = 0
                for adv in advs:
                    val = before_map.get(adv.id)
                    if val is not None and adv.history_story != val:
                        adv.history_story = val
                        restored += 1
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 传记代号还原失败')
            return None, f'还原失败：{e}'
        current_app.logger.info('S1 传记代号还原完成：回写 %s 名（备份 %s 条）',
                                restored, len(before_map))
        return {
            'restored': restored,
            'backup_count': len(before_map),
            'backup_at': payload.get('created_at'),
        }, None

    @staticmethod
    def _has_backup() -> bool:
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        return bool(rec and rec.value)
