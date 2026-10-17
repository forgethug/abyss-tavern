"""S1 命途关键节点陪同贡献台账。"""
from __future__ import annotations

from collections.abc import Iterable

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1FateCompanionCredit,
    AdventurerS1FateEvent,
    AdventurerS1FateJourney,
    AdventurerS1FateResetRequest,
)


_BACKFILL_BATCH_SIZE = 500


def record_fate_companion_credits(
    members: Iterable[AdventurerS1],
    *,
    expedition_log_id: int,
    fate_adventurer_id: int,
) -> int:
    """在命途结算事务内，为本次真实关键节点推进写入每位队友的一次贡献。"""
    focus_id = int(fate_adventurer_id)
    companion_ids = {
        int(member.id)
        for member in members
        if getattr(member, 'id', None) is not None and int(member.id) != focus_id
    }
    if not companion_ids:
        return 0
    existing_ids = {
        int(row[0])
        for row in AdventurerS1FateCompanionCredit.query.with_entities(
            AdventurerS1FateCompanionCredit.companion_adventurer_id,
        ).filter(
            AdventurerS1FateCompanionCredit.expedition_log_id == int(expedition_log_id),
            AdventurerS1FateCompanionCredit.companion_adventurer_id.in_(companion_ids),
        ).all()
    }
    additions = [
        AdventurerS1FateCompanionCredit(
            expedition_log_id=int(expedition_log_id),
            fate_adventurer_id=focus_id,
            companion_adventurer_id=companion_id,
        )
        for companion_id in sorted(companion_ids - existing_ids)
    ]
    if additions:
        ok, err = db_manager.add_all(additions, flush=False)
        if not ok:
            raise RuntimeError(err or '记录命途陪同贡献失败')
    return len(additions)


def _accepted_reset_advanced_logs() -> dict[int, int]:
    result: dict[int, int] = {}
    rows = AdventurerS1FateResetRequest.query.with_entities(
        AdventurerS1FateResetRequest.adventurer_id,
        AdventurerS1FateResetRequest.journey_snapshot,
    ).filter(
        AdventurerS1FateResetRequest.status == 'accepted',
    ).all()
    for adventurer_id, snapshot in rows:
        events = snapshot.get('events') if isinstance(snapshot, dict) else None
        if not isinstance(events, list):
            continue
        for event in events:
            if (
                not isinstance(event, dict)
                or event.get('event_type') != 'key'
                or event.get('result_status') != 'advanced'
            ):
                continue
            try:
                log_id = int(event.get('expedition_log_id') or 0)
            except (TypeError, ValueError):
                continue
            if log_id > 0:
                result.setdefault(log_id, int(adventurer_id))
    return result


def backfill_fate_companion_credits() -> int:
    """从现存命途事件和已接受重置快照幂等回填历史贡献。"""
    advanced_logs = {
        int(log_id): int(fate_adventurer_id)
        for log_id, fate_adventurer_id in (
            AdventurerS1FateEvent.query
            .join(
                AdventurerS1FateJourney,
                AdventurerS1FateJourney.id == AdventurerS1FateEvent.journey_id,
            )
            .with_entities(
                AdventurerS1FateEvent.expedition_log_id,
                AdventurerS1FateJourney.adventurer_id,
            )
            .filter(
                AdventurerS1FateEvent.event_type == 'key',
                AdventurerS1FateEvent.result_status == 'advanced',
                AdventurerS1FateEvent.expedition_log_id.isnot(None),
            )
            .all()
        )
    }
    for log_id, fate_adventurer_id in _accepted_reset_advanced_logs().items():
        advanced_logs.setdefault(log_id, fate_adventurer_id)

    added = 0
    log_ids = sorted(advanced_logs)
    for offset in range(0, len(log_ids), _BACKFILL_BATCH_SIZE):
        batch_ids = log_ids[offset:offset + _BACKFILL_BATCH_SIZE]
        existing = {
            (int(log_id), int(companion_id))
            for log_id, companion_id in (
                AdventurerS1FateCompanionCredit.query.with_entities(
                    AdventurerS1FateCompanionCredit.expedition_log_id,
                    AdventurerS1FateCompanionCredit.companion_adventurer_id,
                ).filter(
                    AdventurerS1FateCompanionCredit.expedition_log_id.in_(batch_ids),
                ).all()
            )
        }
        member_rows = (
            AdventurerS1ExpeditionLogMember.query
            .join(
                AdventurerS1,
                AdventurerS1.id == AdventurerS1ExpeditionLogMember.adventurer_id,
            )
            .with_entities(
                AdventurerS1ExpeditionLogMember.expedition_log_id,
                AdventurerS1ExpeditionLogMember.adventurer_id,
            )
            .filter(
                AdventurerS1ExpeditionLogMember.expedition_log_id.in_(batch_ids),
            )
            .all()
        )
        additions = []
        for log_id, companion_id in member_rows:
            log_id = int(log_id)
            companion_id = int(companion_id)
            fate_adventurer_id = advanced_logs[log_id]
            key = (log_id, companion_id)
            if companion_id == fate_adventurer_id or key in existing:
                continue
            existing.add(key)
            additions.append(AdventurerS1FateCompanionCredit(
                expedition_log_id=log_id,
                fate_adventurer_id=fate_adventurer_id,
                companion_adventurer_id=companion_id,
            ))
        if additions:
            ok, err = db_manager.add_all(additions, flush=False)
            if not ok:
                raise RuntimeError(err or '回填命途陪同贡献失败')
            added += len(additions)
    if added:
        ok, err = db_manager.commit()
        if not ok:
            raise RuntimeError(err or '提交命途陪同贡献回填失败')
    return added
