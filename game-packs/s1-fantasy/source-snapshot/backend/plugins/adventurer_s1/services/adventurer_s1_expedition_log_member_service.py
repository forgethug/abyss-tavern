"""S1 探险日志参与者索引维护。"""

from __future__ import annotations

from sqlalchemy import func

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1PartyMember,
)


def _coerce_positive_int(value) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def collect_expedition_log_member_ids(full_log: dict | None, extra_ids=None) -> list[int]:
    """从日志 JSON 和额外成员列表中收集参与冒险者 id。"""
    ids: set[int] = set()
    for raw in extra_ids or []:
        adv_id = _coerce_positive_int(raw)
        if adv_id:
            ids.add(adv_id)

    if not isinstance(full_log, dict):
        return sorted(ids)

    for row in full_log.get('audit_member_snapshot') or []:
        if isinstance(row, dict):
            adv_id = _coerce_positive_int(row.get('adventurer_id'))
            if adv_id:
                ids.add(adv_id)

    for row in full_log.get('member_updates') or []:
        if isinstance(row, dict):
            adv_id = _coerce_positive_int(row.get('adventurer_id') or row.get('id'))
            if adv_id:
                ids.add(adv_id)

    return sorted(ids)


def refresh_expedition_log_members_for_log(log, extra_ids=None) -> str | None:
    """从日志 JSON + 额外成员 id 刷新参与者索引；调用方负责事务提交。"""
    if not log or not getattr(log, 'id', None):
        return None
    full_log = log.full_log if isinstance(log.full_log, dict) else {}
    member_ids = collect_expedition_log_member_ids(full_log, extra_ids)
    return sync_expedition_log_members(int(log.id), member_ids)


def preview_expedition_log_member_backfill() -> dict:
    """统计参与者索引覆盖情况（只读）。"""
    total_logs = (
        db_manager.query(func.count(AdventurerS1ExpeditionLog.id)).scalar() or 0
    )
    indexed_logs = (
        db_manager.query(func.count(func.distinct(AdventurerS1ExpeditionLogMember.expedition_log_id)))
        .scalar() or 0
    )
    member_rows = (
        db_manager.query(func.count(AdventurerS1ExpeditionLogMember.id)).scalar() or 0
    )
    return {
        'total_logs': int(total_logs),
        'indexed_logs': int(indexed_logs),
        'missing_logs_estimate': max(0, int(total_logs) - int(indexed_logs)),
        'member_rows': int(member_rows),
    }


def sync_expedition_log_members(log_id: int, member_ids) -> str | None:
    """同步单条日志的参与者索引；调用方负责事务提交。"""
    log_id = int(log_id)
    wanted = {
        adv_id for adv_id in (_coerce_positive_int(v) for v in (member_ids or []))
        if adv_id
    }
    if not wanted:
        return None

    existing = {
        row[0]
        for row in db_manager.query(AdventurerS1ExpeditionLogMember.adventurer_id)
        .filter(AdventurerS1ExpeditionLogMember.expedition_log_id == log_id)
        .all()
    }
    missing = wanted - existing
    if not missing:
        return None

    ok, err = db_manager.bulk_insert_mappings(
        AdventurerS1ExpeditionLogMember,
        [
            {'expedition_log_id': log_id, 'adventurer_id': adv_id}
            for adv_id in sorted(missing)
        ],
    )
    if not ok:
        return err or '探险日志成员索引写入失败'
    ok, err = db_manager.flush()
    if not ok:
        return err or '探险日志成员索引刷新失败'
    return None


def backfill_expedition_log_members(
    batch_size: int = 500,
    max_batches: int | None = None,
    *,
    skip_when_fully_indexed: bool = False,
) -> int:
    """从历史日志 JSON/仍存在的小队成员关系回填参与者索引。

    max_batches: 限制批次数，避免启动 seed 长时间阻塞；None 表示扫完全表。
    skip_when_fully_indexed: 启动 seed 可先检查是否每条日志至少已有一条参与者索引；
        覆盖完整时跳过重型 JSON 扫描。管理员显式回填保持全量复核语义。
    """
    if skip_when_fully_indexed:
        unindexed_log = (
            db_manager.query(AdventurerS1ExpeditionLog.id)
            .outerjoin(
                AdventurerS1ExpeditionLogMember,
                AdventurerS1ExpeditionLogMember.expedition_log_id
                == AdventurerS1ExpeditionLog.id,
            )
            .filter(AdventurerS1ExpeditionLogMember.id.is_(None))
            .first()
        )
        if unindexed_log is None:
            return 0

    inserted = 0
    last_id = 0
    batch_num = 0
    while True:
        if max_batches is not None and batch_num >= max_batches:
            break
        batch_num += 1
        rows = (
            db_manager.query(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.party_id,
                AdventurerS1ExpeditionLog.full_log,
            )
            .filter(AdventurerS1ExpeditionLog.id > last_id)
            .order_by(AdventurerS1ExpeditionLog.id.asc())
            .limit(batch_size)
            .all()
        )
        if not rows:
            break
        last_id = rows[-1][0]

        log_ids = [row[0] for row in rows]
        existing_pairs = {
            (int(row[0]), int(row[1]))
            for row in db_manager.query(
                AdventurerS1ExpeditionLogMember.expedition_log_id,
                AdventurerS1ExpeditionLogMember.adventurer_id,
            )
            .filter(AdventurerS1ExpeditionLogMember.expedition_log_id.in_(log_ids))
            .all()
        }

        party_ids = {row[1] for row in rows if row[1]}
        party_member_ids: dict[int, set[int]] = {}
        if party_ids:
            for party_id, adventurer_id in (
                db_manager.query(AdventurerS1PartyMember.party_id, AdventurerS1PartyMember.adventurer_id)
                .filter(AdventurerS1PartyMember.party_id.in_(party_ids))
                .all()
            ):
                party_member_ids.setdefault(int(party_id), set()).add(int(adventurer_id))

        mappings = []
        for log_id, party_id, full_log in rows:
            member_ids = collect_expedition_log_member_ids(
                full_log if isinstance(full_log, dict) else {},
                party_member_ids.get(int(party_id), set()) if party_id else None,
            )
            for adventurer_id in member_ids:
                pair = (int(log_id), int(adventurer_id))
                if pair in existing_pairs:
                    continue
                existing_pairs.add(pair)
                mappings.append({'expedition_log_id': pair[0], 'adventurer_id': pair[1]})

        if mappings:
            ok, err = db_manager.bulk_insert_mappings(AdventurerS1ExpeditionLogMember, mappings)
            if not ok:
                raise RuntimeError(err or '探险日志成员索引回填失败')
            ok, err = db_manager.flush()
            if not ok:
                raise RuntimeError(err or '探险日志成员索引回填刷新失败')
            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or '探险日志成员索引回填提交失败')
            inserted += len(mappings)

    return inserted
