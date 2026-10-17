"""探险日志 summary / events 残留代号修复。

AI 有时把 ``【M1】`` 缩写成 bare ``M1``，或历史日志仍含 ``冒险者甲``。
映射**必须**来自当次 ``audit_member_snapshot`` 顺序，不能按 party_member.created_at 乱序。
"""
from runtime_core.time import utc_now as _portable_utc_now
import json
from datetime import datetime
from typing import Optional

from flask import current_app
from sqlalchemy.orm.attributes import flag_modified

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1ExpeditionLog
from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
    _restore_codenames_in_result,
    restore_names_in_narrative,
)
from plugins.adventurer_s1.services.adventurer_s1_prompt_service import (
    RESIDUAL_CODENAME_PATTERN,
    build_codename_to_real_from_snapshot,
)

# PostgreSQL ~ 预筛（result_summary）；full_log 仍在 Python 侧复检
_EXPEDITION_SUMMARY_FILTER = (
    r'(?:【M[0-9]+】|冒险者[甲乙丙丁戊己庚辛壬癸]|[^A-Za-z【]M[0-9]+([^0-9]|$))'
)
_STARTUP_CHECKPOINT_KEY = 'advs1_expedition_codename_repair_checkpoint_v1'


class AdventurerS1ExpeditionCodenameRepairService:
    """completed 探险日志内玩家可见叙事的代号 → 真名还原。"""

    @staticmethod
    def _text_has_residual(text: str) -> bool:
        return bool(RESIDUAL_CODENAME_PATTERN.search(text or ''))

    @classmethod
    def _log_needs_repair(cls, log: AdventurerS1ExpeditionLog) -> bool:
        fl = log.full_log if isinstance(log.full_log, dict) else {}
        blob = (log.result_summary or '') + json.dumps(fl, ensure_ascii=False)
        return cls._text_has_residual(blob)

    @classmethod
    def repair_log(cls, log: AdventurerS1ExpeditionLog, id_to_name: dict[int, str]) -> bool:
        fl = dict(log.full_log or {})
        snap = fl.get('audit_member_snapshot')
        if not isinstance(snap, list) or not snap:
            return False
        mapping = build_codename_to_real_from_snapshot(snap, id_to_name)
        if not mapping:
            return False
        if not cls._log_needs_repair(log):
            return False

        payload = {
            'summary': fl.get('summary') or log.result_summary or '',
            'events': fl.get('events') or [],
            'member_updates': fl.get('member_updates') or [],
        }
        before = json.dumps(payload, ensure_ascii=False)
        restored = _restore_codenames_in_result(dict(payload), mapping)
        after = json.dumps(restored, ensure_ascii=False)

        new_summary = restored.get('summary') or ''
        new_result_summary = restore_names_in_narrative(
            log.result_summary or '', mapping,
        )
        if not new_result_summary and new_summary:
            new_result_summary = new_summary

        if before == after and new_result_summary == (log.result_summary or ''):
            return False

        fl['summary'] = restored.get('summary')
        fl['events'] = restored.get('events')
        fl['member_updates'] = restored.get('member_updates')
        log.full_log = fl
        if new_summary:
            log.result_summary = new_summary
        elif new_result_summary != (log.result_summary or ''):
            log.result_summary = new_result_summary
        flag_modified(log, 'full_log')
        return True

    @classmethod
    def repair_all(
        cls,
        *,
        party_ids: Optional[list[int]] = None,
        log_ids: Optional[list[int]] = None,
        updated_after: Optional[datetime] = None,
        updated_through: Optional[datetime] = None,
        raise_on_error: bool = False,
    ) -> int:
        """扫描并修复残留代号；返回修复条数。"""
        id_to_name = dict(
            AdventurerS1.query
            .with_entities(AdventurerS1.id, AdventurerS1.name)
            .all()
        )
        db_manager.rollback()
        q = AdventurerS1ExpeditionLog.query.filter(
            AdventurerS1ExpeditionLog.status == 'completed',
        )
        if party_ids:
            q = q.filter(AdventurerS1ExpeditionLog.party_id.in_(party_ids))
        if log_ids:
            q = q.filter(AdventurerS1ExpeditionLog.id.in_(log_ids))
        elif not party_ids:
            if updated_after is not None:
                q = q.filter(AdventurerS1ExpeditionLog.updated_at > updated_after)
            if updated_through is not None:
                q = q.filter(AdventurerS1ExpeditionLog.updated_at <= updated_through)

            from sqlalchemy import String, cast

            full_log_text = cast(AdventurerS1ExpeditionLog.full_log, String)
            q = q.filter(
                db.or_(
                    AdventurerS1ExpeditionLog.result_summary.op('~')(
                        _EXPEDITION_SUMMARY_FILTER,
                    ),
                    full_log_text.op('~')(_EXPEDITION_SUMMARY_FILTER),
                ),
            )

        fixed = 0
        last_id = 0
        try:
            while True:
                page_ids = [
                    int(row[0])
                    for row in (
                        q.with_entities(AdventurerS1ExpeditionLog.id)
                        .filter(AdventurerS1ExpeditionLog.id > last_id)
                        .order_by(AdventurerS1ExpeditionLog.id.asc())
                        .limit(50)
                        .all()
                    )
                ]
                db_manager.rollback()
                if not page_ids:
                    break
                last_id = page_ids[-1]
                with db_manager.transaction():
                    logs = (
                        AdventurerS1ExpeditionLog.query
                        .filter(AdventurerS1ExpeditionLog.id.in_(page_ids))
                        .order_by(AdventurerS1ExpeditionLog.id.asc())
                        .with_for_update()
                        .all()
                    )
                    for log in logs:
                        if log_ids is None and party_ids is None and not cls._log_needs_repair(log):
                            continue
                        if cls.repair_log(log, id_to_name):
                            fixed += 1
        except Exception:  # noqa: BLE001
            if raise_on_error:
                raise
            current_app.logger.exception('S1 探险代号修复失败')
            return fixed

        if fixed:
            current_app.logger.info('S1 探险日志代号修复完成：%s 条', fixed)
        return fixed

    @classmethod
    def repair_startup_incremental(cls) -> int:
        """按持久检查点扫描启动后新增或变更的日志；首次运行仍完整复核历史数据。"""
        from models.system_config import SystemConfig

        checkpoint_row = (
            db_manager.query(SystemConfig)
            .filter_by(config_key=_STARTUP_CHECKPOINT_KEY)
            .first()
        )
        updated_after = None
        if checkpoint_row and checkpoint_row.config_value:
            try:
                updated_after = datetime.fromisoformat(checkpoint_row.config_value)
            except (TypeError, ValueError):
                current_app.logger.warning(
                    'S1 探险代号修复检查点无效，将重新完整扫描: %r',
                    checkpoint_row.config_value,
                )

        scan_through = _portable_utc_now()
        fixed = cls.repair_all(
            updated_after=updated_after,
            updated_through=scan_through,
            raise_on_error=True,
        )

        checkpoint_value = scan_through.isoformat(timespec='microseconds')
        if checkpoint_row is None:
            checkpoint_row = SystemConfig(
                config_key=_STARTUP_CHECKPOINT_KEY,
                config_value=checkpoint_value,
                description='S1 探险日志残留代号启动增量修复检查点（UTC）',
            )
            ok, err = db_manager.add(checkpoint_row)
            if not ok:
                raise RuntimeError(err or 'S1 探险代号修复检查点创建失败')
        else:
            checkpoint_row.config_value = checkpoint_value

        ok, err = db_manager.commit()
        if not ok:
            raise RuntimeError(err or 'S1 探险代号修复检查点提交失败')
        return fixed
