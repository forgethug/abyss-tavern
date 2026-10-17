"""Compress and restore finalized S1 cold payloads without changing their API shape."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import logging
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.orm.attributes import flag_modified

from db_repository import db_manager
from plugins.adventurer_s1.cold_storage import mark_cold
from plugins.adventurer_s1.models import (
    AdventurerS1AppealFactSheet,
    AdventurerS1DataAppeal,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionObserverReview,
)


logger = logging.getLogger(__name__)


class S1ColdStorageService:
    """Batch lifecycle for old, finalized S1 text and JSON payloads."""

    _SPECS = (
        (
            AdventurerS1ExpeditionLog,
            ("full_log",),
            lambda q: q.filter(AdventurerS1ExpeditionLog.status.in_(("completed", "failed"))),
        ),
        (
            AdventurerS1DataAppeal,
            (
                "snapshot_before",
                "expedition_evidence",
                "ai_raw_response",
                "proposed_patch",
                "review_ai_raw_response",
                "review_supplement_patch",
                "player_review_requests",
            ),
            lambda q: q.filter(AdventurerS1DataAppeal.status.in_(("approved", "rejected", "reverted"))),
        ),
        (
            AdventurerS1ExpeditionObserverReview,
            ("result", "raw_response"),
            lambda q: q.filter(AdventurerS1ExpeditionObserverReview.status.in_(("completed", "failed"))),
        ),
        (
            AdventurerS1AppealFactSheet,
            ("fact_sheet", "raw_response"),
            lambda q: q,
        ),
    )

    @staticmethod
    def _retention_days() -> int:
        raw = current_app.config.get("ADVENTURER_S1_COLD_STORAGE_AFTER_DAYS", 7)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 7

    @staticmethod
    def _batch_size() -> int:
        raw = current_app.config.get("ADVENTURER_S1_COLD_STORAGE_BATCH_SIZE", 200)
        try:
            return min(1000, max(1, int(raw)))
        except (TypeError, ValueError):
            return 200

    @staticmethod
    def _max_batches_per_run() -> int:
        raw = current_app.config.get("ADVENTURER_S1_COLD_STORAGE_MAX_BATCHES_PER_RUN", 5)
        try:
            return min(20, max(1, int(raw)))
        except (TypeError, ValueError):
            return 5

    @classmethod
    def _compress_one_round(cls, *, cutoff: datetime, batch_size: int) -> dict[str, int]:
        """Stage one bounded batch per table, locking rows without blocking another worker."""
        counts: dict[str, int] = {}

        for model, fields, scope_query in cls._SPECS:
            query = model.query.filter(
                model.cold_compressed_at.is_(None),
                model.created_at < cutoff,
            ).order_by(model.id.asc())
            rows = scope_query(query).with_for_update(skip_locked=True).limit(batch_size).all()
            compressed_at = _portable_utc_now()
            for row in rows:
                for field in fields:
                    value = getattr(row, field)
                    if value is not None:
                        setattr(row, field, mark_cold(value))
                row.cold_compressed_at = compressed_at
            counts[model.__tablename__] = len(rows)

        return counts

    @classmethod
    def compress_due_payloads(cls) -> dict[str, int]:
        """Compress bounded catch-up batches, committing each round independently."""
        cutoff = _portable_utc_now() - timedelta(days=cls._retention_days())
        batch_size = cls._batch_size()
        counts = {model.__tablename__: 0 for model, _fields, _scope_query in cls._SPECS}

        for _batch_number in range(cls._max_batches_per_run()):
            round_counts = cls._compress_one_round(cutoff=cutoff, batch_size=batch_size)
            if not any(round_counts.values()):
                break

            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(f"S1 cold payload compression commit failed: {err}")
            for table_name, count in round_counts.items():
                counts[table_name] += count

        logger.info("S1 cold payload compression completed: %s", counts)
        return counts

    @classmethod
    def restore_payloads(cls, *, batch_size: int | None = None) -> dict[str, int]:
        """Rewrite compressed values to their ordinary JSON/TEXT representation in bounded batches."""
        limit = min(1000, max(1, int(batch_size or cls._batch_size())))
        counts: dict[str, int] = {}

        for model, fields, _scope_query in cls._SPECS:
            rows = (
                model.query.filter(model.cold_compressed_at.isnot(None))
                .order_by(model.id.asc())
                .limit(limit)
                .all()
            )
            for row in rows:
                for field in fields:
                    if getattr(row, field) is not None:
                        flag_modified(row, field)
                row.cold_compressed_at = None
            counts[model.__tablename__] = len(rows)

        ok, err = db_manager.commit()
        if not ok:
            raise RuntimeError(f"S1 cold payload restore commit failed: {err}")
        logger.info("S1 cold payload restore completed: %s", counts)
        return counts


def compress_due_s1_cold_payloads_job() -> None:
    """Scheduler entrypoint; failures are logged and retried on the next interval."""
    try:
        S1ColdStorageService.compress_due_payloads()
    except Exception:
        logger.exception("S1 cold payload compression job failed")
    finally:
        db_manager.remove()
