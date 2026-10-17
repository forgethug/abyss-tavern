"""S1 常规副本改名维护工具。

只做精确旧名替换，不改历史剧情正文，避免把玩家可见叙事文本误改坏。
"""
from runtime_core.time import utc_now as _portable_utc_now
import json
from datetime import datetime
from typing import Any, Optional, Tuple

from flask import current_app
from sqlalchemy import Text, cast, or_
from sqlalchemy.orm.attributes import flag_modified

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1AppealFactSheet,
    AdventurerS1Commission,
    AdventurerS1DataAppeal,
    AdventurerS1DmHandoffState,
    AdventurerS1ExpeditionLog,
    AdventurerS1HiddenBossState,
    AdventurerS1MatchTicket,
    AdventurerS1NarrativeLease,
    AdventurerS1Party,
    AdventurerS1Settings,
    AdventurerS1WorldFactCandidate,
)


S1_DUNGEON_RENAME_MAP = {
    '炼狱魔城': '黑日王庭',
    '龙皇陵': '陨落星都',
}

_AUDIT_SETTING_KEY = 'dungeon_rename_20260705'

_SCALAR_TARGETS = (
    ('parties.target_dungeon', AdventurerS1Party, AdventurerS1Party.target_dungeon, False),
    ('match_tickets.preferred_dungeon', AdventurerS1MatchTicket, AdventurerS1MatchTicket.preferred_dungeon, False),
    ('world_fact_candidates.target_dungeon', AdventurerS1WorldFactCandidate, AdventurerS1WorldFactCandidate.target_dungeon, False),
    ('dm_handoff_states.target_dungeon', AdventurerS1DmHandoffState, AdventurerS1DmHandoffState.target_dungeon, True),
    ('expedition_logs.target_dungeon', AdventurerS1ExpeditionLog, AdventurerS1ExpeditionLog.target_dungeon, False),
    ('hidden_boss_states.dungeon', AdventurerS1HiddenBossState, AdventurerS1HiddenBossState.dungeon, True),
    ('narrative_leases.target_dungeon', AdventurerS1NarrativeLease, AdventurerS1NarrativeLease.target_dungeon, False),
)

_JSON_TARGETS = (
    ('match_tickets.preferred_dungeons', AdventurerS1MatchTicket, AdventurerS1MatchTicket.preferred_dungeons),
    ('commissions.target_dungeons', AdventurerS1Commission, AdventurerS1Commission.target_dungeons),
    ('data_appeals.expedition_evidence', AdventurerS1DataAppeal, AdventurerS1DataAppeal.expedition_evidence),
    ('appeal_fact_sheets.fact_sheet', AdventurerS1AppealFactSheet, AdventurerS1AppealFactSheet.fact_sheet),
)


def _replace_exact_json_values(value: Any) -> tuple[Any, int]:
    """递归替换 JSON 里的精确副本名字符串，返回新值和替换次数。"""
    if isinstance(value, str):
        new_value = S1_DUNGEON_RENAME_MAP.get(value)
        return (new_value, 1) if new_value else (value, 0)
    if isinstance(value, list):
        changed = 0
        result = []
        for item in value:
            new_item, item_changed = _replace_exact_json_values(item)
            result.append(new_item)
            changed += item_changed
        return result, changed
    if isinstance(value, dict):
        changed = 0
        result = {}
        for key, item in value.items():
            new_item, item_changed = _replace_exact_json_values(item)
            result[key] = new_item
            changed += item_changed
        return result, changed
    return value, 0


def _json_text_match(column, old_name: str):
    escaped = json.dumps(old_name, ensure_ascii=True).strip('"')
    return or_(
        cast(column, Text).like(f'%{old_name}%'),
        cast(column, Text).like(f'%{escaped}%'),
    )


class S1DungeonRenameService:
    """管理员手动执行的 S1 副本名历史数据迁移。"""

    @staticmethod
    def _count_scalar(model, column) -> dict:
        counts = {}
        for old_name in S1_DUNGEON_RENAME_MAP:
            counts[old_name] = model.query.filter(column == old_name).count()
        return counts

    @staticmethod
    def _count_json(model, column) -> tuple[dict, int, int]:
        counts = {old_name: 0 for old_name in S1_DUNGEON_RENAME_MAP}
        candidate_ids = set()
        for old_name in S1_DUNGEON_RENAME_MAP:
            for (row_id,) in model.query.with_entities(model.id).filter(_json_text_match(column, old_name)).all():
                candidate_ids.add(row_id)
        if not candidate_ids:
            return counts, 0, 0
        replaced_total = 0
        changed_row_count = 0
        for row in model.query.filter(model.id.in_(candidate_ids)).all():
            _new_value, changed = _replace_exact_json_values(getattr(row, column.key))
            if changed:
                changed_row_count += 1
                replaced_total += changed
                for old_name in S1_DUNGEON_RENAME_MAP:
                    # 预览按原始 JSON 精确值估算，和执行替换次数保持同一语义。
                    _unused, old_changed = _replace_exact_json_values(_filter_json_value(getattr(row, column.key), old_name))
                    counts[old_name] += old_changed
        return counts, replaced_total, changed_row_count

    @classmethod
    def preview(cls) -> Tuple[Optional[dict], Optional[str]]:
        """预览各表命中数量，不写库。"""
        try:
            rows = []
            total_rows = 0
            total_replacements = 0
            conflicts = []
            for label, model, column, unique in _SCALAR_TARGETS:
                counts = cls._count_scalar(model, column)
                row_total = sum(counts.values())
                total_rows += row_total
                total_replacements += row_total
                conflict_items = []
                if unique:
                    for old_name, new_name in S1_DUNGEON_RENAME_MAP.items():
                        if counts.get(old_name, 0) and model.query.filter(column == new_name).first() is not None:
                            conflict_items.append({'old_name': old_name, 'new_name': new_name})
                            conflicts.append({'target': label, 'old_name': old_name, 'new_name': new_name})
                rows.append({
                    'target': label,
                    'kind': 'scalar',
                    'row_count': row_total,
                    'replacement_count': row_total,
                    'by_old_name': counts,
                    'conflicts': conflict_items,
                })
            for label, model, column in _JSON_TARGETS:
                counts, replacement_total, row_total = cls._count_json(model, column)
                rows.append({
                    'target': label,
                    'kind': 'json',
                    'row_count': row_total,
                    'replacement_count': replacement_total,
                    'by_old_name': counts,
                    'conflicts': [],
                })
                total_rows += row_total
                total_replacements += replacement_total
            return {
                'mappings': dict(S1_DUNGEON_RENAME_MAP),
                'targets': rows,
                'total_rows': total_rows,
                'total_replacements': total_replacements,
                'conflicts': conflicts,
                'has_conflicts': bool(conflicts),
            }, None
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 副本名迁移 preview 失败')
            return None, f'预览失败：{e}'

    @classmethod
    def execute(cls, *, admin_user_id: Optional[int] = None) -> Tuple[Optional[dict], Optional[str]]:
        """执行精确改名迁移；唯一键冲突的旧行会跳过并在结果中报告。"""
        preview, err = cls.preview()
        if err:
            return None, err
        try:
            with db_manager.transaction():
                scalar_updated = 0
                json_rows_updated = 0
                json_replacements = 0
                skipped_conflicts = []
                for label, model, column, unique in _SCALAR_TARGETS:
                    for old_name, new_name in S1_DUNGEON_RENAME_MAP.items():
                        if unique and model.query.filter(column == new_name).first() is not None:
                            old_count = model.query.filter(column == old_name).count()
                            if old_count:
                                skipped_conflicts.append({
                                    'target': label,
                                    'old_name': old_name,
                                    'new_name': new_name,
                                    'skipped_rows': old_count,
                                })
                            continue
                        scalar_updated += model.query.filter(column == old_name).update(
                            {column: new_name}, synchronize_session=False,
                        )
                for _label, model, column in _JSON_TARGETS:
                    candidate_ids = set()
                    for old_name in S1_DUNGEON_RENAME_MAP:
                        for (row_id,) in model.query.with_entities(model.id).filter(
                            _json_text_match(column, old_name)
                        ).all():
                            candidate_ids.add(row_id)
                    if not candidate_ids:
                        continue
                    for row in model.query.filter(model.id.in_(candidate_ids)).all():
                        old_value = getattr(row, column.key)
                        new_value, changed = _replace_exact_json_values(old_value)
                        if not changed:
                            continue
                        setattr(row, column.key, new_value)
                        flag_modified(row, column.key)
                        json_rows_updated += 1
                        json_replacements += changed
                audit_payload = {
                    'executed_at': _portable_utc_now().isoformat() + 'Z',
                    'admin_user_id': admin_user_id,
                    'mappings': dict(S1_DUNGEON_RENAME_MAP),
                    'preview': preview,
                    'result': {
                        'scalar_updated': scalar_updated,
                        'json_rows_updated': json_rows_updated,
                        'json_replacements': json_replacements,
                        'skipped_conflicts': skipped_conflicts,
                    },
                }
                rec = AdventurerS1Settings.query.filter_by(key=_AUDIT_SETTING_KEY).first()
                if rec is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_AUDIT_SETTING_KEY,
                        value=json.dumps(audit_payload, ensure_ascii=False),
                        description='S1 高阶常规副本改名迁移审计记录',
                        updated_by_user_id=admin_user_id,
                    ))
                else:
                    rec.value = json.dumps(audit_payload, ensure_ascii=False)
                    rec.updated_by_user_id = admin_user_id
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 副本名迁移执行失败')
            return None, f'迁移失败：{e}'
        return {
            'mappings': dict(S1_DUNGEON_RENAME_MAP),
            'scalar_updated': scalar_updated,
            'json_rows_updated': json_rows_updated,
            'json_replacements': json_replacements,
            'total_replacements': scalar_updated + json_replacements,
            'skipped_conflicts': skipped_conflicts,
        }, None


def _filter_json_value(value: Any, target_name: str) -> Any:
    """只保留目标旧名，用于按旧名统计精确 JSON 值命中次数。"""
    if isinstance(value, str):
        return value if value == target_name else None
    if isinstance(value, list):
        return [_filter_json_value(item, target_name) for item in value]
    if isinstance(value, dict):
        return {key: _filter_json_value(item, target_name) for key, item in value.items()}
    return None
