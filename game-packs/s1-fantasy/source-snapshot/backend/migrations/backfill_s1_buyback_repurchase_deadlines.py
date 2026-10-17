"""Give pre-deadline S1 buyback sales one deployment-time repurchase window."""
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1CurrencyLog


def _clamp_hours(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 24
    return max(1.0, min(168.0, parsed))


def migrate(*, now: datetime | None = None, hours=24) -> dict:
    """Freeze one common deadline for every old sale that had no timer.

    The nullable predicate makes repeated starts idempotent: once a deadline is
    written, later restarts cannot extend it.
    """
    started_at = now or _portable_utc_now()
    deadline = started_at + timedelta(hours=_clamp_hours(hours))
    with db_manager.transaction():
        updated = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.source_type == 'buyback_sell',
                AdventurerS1CurrencyLog.change_amount > 0,
                AdventurerS1CurrencyLog.business_expires_at.is_(None),
            )
            .update(
                {AdventurerS1CurrencyLog.business_expires_at: deadline},
                synchronize_session=False,
            )
        )
    return {
        'updated': int(updated or 0),
        'started_at': started_at,
        'deadline': deadline,
    }
