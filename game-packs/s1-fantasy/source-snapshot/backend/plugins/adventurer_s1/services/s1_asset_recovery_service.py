"""Unified wall-clock recovery state for S1 skills and equipment.

Descriptions remain authoritative text. Recovery completion is derived lazily from
``recovery_ends_at`` and never rewrites an asset description.
"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta

from sqlalchemy import or_


def normalize_recovery_minutes(value, *, default: int = 0) -> int:
    """Return a non-negative integer duration without accepting bool/float coercion."""
    if type(value) is int and value >= 0:
        return value
    return max(0, int(default or 0))


def is_asset_recovering(asset, *, now: datetime | None = None) -> bool:
    ends_at = getattr(asset, 'recovery_ends_at', None)
    return bool(ends_at and ends_at > (now or _portable_utc_now()))


def recovery_ai_fields(asset, *, now: datetime | None = None) -> dict:
    """The only dynamic recovery signal exposed to AI is ``recovering``."""
    return {
        'recovery_minutes': normalize_recovery_minutes(
            getattr(asset, 'recovery_minutes', 0),
        ),
        'recovering': is_asset_recovering(asset, now=now),
    }


def recovery_stack_state(
    recovery_started_at,
    recovery_ends_at,
    *,
    now: datetime | None = None,
) -> tuple:
    """Return the timer state that affects stacking; completed timers are equal."""
    moment = now or _portable_utc_now()
    if recovery_ends_at and recovery_ends_at > moment:
        return recovery_started_at, recovery_ends_at
    return None, None


def filter_recovery_mergeable_rows(
    query,
    model,
    *,
    recovery_minutes=0,
    recovery_started_at=None,
    recovery_ends_at=None,
    now: datetime | None = None,
):
    """Filter rows with the same static duration and effective timer state."""
    moment = now or _portable_utc_now()
    query = query.filter(
        model.recovery_minutes == normalize_recovery_minutes(recovery_minutes),
    )
    active_started_at, active_ends_at = recovery_stack_state(
        recovery_started_at,
        recovery_ends_at,
        now=moment,
    )
    if active_ends_at is not None:
        return query.filter(
            model.recovery_started_at == active_started_at,
            model.recovery_ends_at == active_ends_at,
        )
    return query.filter(or_(
        model.recovery_ends_at.is_(None),
        model.recovery_ends_at <= moment,
    ))


def start_asset_recovery(
    asset,
    *,
    recovery_minutes: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Start one parallel timer; return False when the asset has no recovery cost.

    A still-active timer cannot be restarted or shortened. Callers are expected to
    hold a row lock before invoking this helper.
    """
    moment = now or _portable_utc_now()
    if is_asset_recovering(asset, now=moment):
        raise RuntimeError('恢复中的技能或装备不能再次触发')
    minutes = normalize_recovery_minutes(
        getattr(asset, 'recovery_minutes', 0)
        if recovery_minutes is None else recovery_minutes,
    )
    if minutes <= 0:
        return False
    asset.recovery_started_at = moment
    asset.recovery_ends_at = moment + timedelta(minutes=minutes)
    return True
