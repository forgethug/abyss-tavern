"""Bounded read helpers for the S1 expedition-log deduplication preview."""

from extensions import db
from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog


def list_preview_logs(
    *,
    before_log_id: int | None,
    limit: int,
    cold_only: bool,
) -> tuple[list[dict], bool]:
    """Load one stable newest-first page; the preview service performs no writes."""
    query = db.session.query(
        AdventurerS1ExpeditionLog.id,
        AdventurerS1ExpeditionLog.status,
        AdventurerS1ExpeditionLog.created_at,
        AdventurerS1ExpeditionLog.updated_at,
        AdventurerS1ExpeditionLog.cold_compressed_at,
        AdventurerS1ExpeditionLog.full_log,
    ).filter(
        AdventurerS1ExpeditionLog.status.in_(("completed", "failed")),
    )
    if before_log_id is not None:
        query = query.filter(AdventurerS1ExpeditionLog.id < int(before_log_id))
    if cold_only:
        query = query.filter(AdventurerS1ExpeditionLog.cold_compressed_at.isnot(None))

    rows = (
        query.order_by(AdventurerS1ExpeditionLog.id.desc())
        .limit(int(limit) + 1)
        .all()
    )
    has_more = len(rows) > int(limit)
    return [
        {
            "id": int(row.id),
            "status": str(row.status),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "cold_compressed_at": row.cold_compressed_at,
            "full_log": row.full_log,
        }
        for row in rows[: int(limit)]
    ], has_more
