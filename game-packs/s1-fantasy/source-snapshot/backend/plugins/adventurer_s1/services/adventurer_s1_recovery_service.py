"""Unified S1 lazy recovery settlement helpers.

Model ``get_current_*`` methods are read-only live calculators. Functions in this
module are the only service-level entry points that write the calculated HP or
stamina back to an adventurer row.
"""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime
from typing import Optional

from flask import current_app


def s1_max_stamina() -> int:
    return int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))


def s1_stamina_recovery_per_minute() -> int:
    return int(current_app.config.get('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1))


def s1_hp_recovery_minutes_per_point() -> int:
    return int(current_app.config.get('ADVENTURER_S1_DORM_HP_RECOVERY_MINUTES', 10))


def resolve_tavern_seated_for_recovery(adv, tavern_seated: Optional[bool] = None) -> bool:
    """Resolve whether the independent tavern recovery source is active.

    Callers that already batch-loaded seat ids should pass an explicit boolean;
    single-adventurer business checks use the narrow resting/idle lookup below.
    """
    # A seat flag is only evidence of location, never permission to recover
    # while the adventurer is already busy.  This also makes stale seat/cache
    # state harmless during dispatch races.
    if getattr(adv, 'status', None) not in ('resting', 'idle'):
        return False
    if tavern_seated is not None:
        return bool(tavern_seated)
    if getattr(adv, 'id', None) is None:
        return False
    from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
        AdventurerS1TavernSeatService,
    )
    return adv.id in AdventurerS1TavernSeatService.get_seated_adventurer_ids([adv.id])


def get_live_stamina(
    adv,
    *,
    max_stamina: Optional[int] = None,
    recovery_per_minute: Optional[int] = None,
    now: Optional[datetime] = None,
    tavern_seated: Optional[bool] = None,
) -> int:
    """Read live stamina from the active recovery source.

    Tavern recovery is an independent, stamina-only source: it uses the base
    configured rate regardless of dorm level and also applies to an ``idle``
    adventurer.  Other states keep using the model's dorm/building calculator.
    """
    stamina_max = s1_max_stamina() if max_stamina is None else int(max_stamina)
    rate = (
        s1_stamina_recovery_per_minute()
        if recovery_per_minute is None else int(recovery_per_minute)
    )
    recovery_now = now or _portable_utc_now()
    if resolve_tavern_seated_for_recovery(adv, tavern_seated):
        base = max(0, min(int(getattr(adv, 'stamina', 0) or 0), stamina_max))
        updated_at = getattr(adv, 'stamina_updated_at', None)
        if not updated_at:
            return base
        elapsed_minutes = max(0, int((recovery_now - updated_at).total_seconds() / 60))
        return max(0, min(base + elapsed_minutes * max(0, rate), stamina_max))
    return int(adv.get_current_stamina(
        max_stamina=stamina_max,
        recovery_per_minute=rate,
        now=recovery_now,
        tavern_seated=False,
    ))


def get_live_hp(
    adv,
    *,
    recovery_minutes_per_point: Optional[int] = None,
    now: Optional[datetime] = None,
    tavern_seated: Optional[bool] = None,
) -> int:
    """Read current HP; tavern seating freezes HP because the tavern only restores stamina."""
    if resolve_tavern_seated_for_recovery(adv, tavern_seated):
        max_hp = max(1, int(getattr(adv, 'max_hp', 100) or 100))
        return max(0, min(int(getattr(adv, 'hp', 0) or 0), max_hp))
    return int(adv.get_current_hp(
        recovery_minutes_per_point=(
            s1_hp_recovery_minutes_per_point()
            if recovery_minutes_per_point is None else int(recovery_minutes_per_point)
        ),
        now=now,
        tavern_seated=False,
    ))


def flush_stamina_to_db(
    adv,
    now: Optional[datetime] = None,
    recovery_per_minute: Optional[int] = None,
    *,
    tavern_seated: Optional[bool] = None,
) -> int:
    """Settle lazy stamina recovery onto ``adv.stamina`` and refresh its timer."""
    settle_now = now or _portable_utc_now()
    rate = s1_stamina_recovery_per_minute() if recovery_per_minute is None else int(recovery_per_minute)
    current = get_live_stamina(
        adv,
        max_stamina=s1_max_stamina(),
        recovery_per_minute=rate,
        now=settle_now,
        tavern_seated=tavern_seated,
    )
    adv.stamina = current
    adv.stamina_updated_at = settle_now
    return current


def flush_hp_to_db(
    adv,
    now: Optional[datetime] = None,
    recovery_minutes_per_point: Optional[int] = None,
    *,
    tavern_seated: Optional[bool] = None,
) -> int:
    """Settle lazy HP recovery onto ``adv.hp`` and refresh its timer."""
    settle_now = now or _portable_utc_now()
    minutes = (
        s1_hp_recovery_minutes_per_point()
        if recovery_minutes_per_point is None
        else int(recovery_minutes_per_point)
    )
    current = get_live_hp(
        adv,
        recovery_minutes_per_point=minutes,
        now=settle_now,
        tavern_seated=tavern_seated,
    )
    adv.hp = current
    adv.hp_updated_at = settle_now
    return current


def flush_recovery_to_db(
    adv,
    now: Optional[datetime] = None,
    *,
    tavern_seated: Optional[bool] = None,
) -> tuple[int, int]:
    """Settle both stamina and HP using one timestamp."""
    settle_now = now or _portable_utc_now()
    resolved_tavern_seated = resolve_tavern_seated_for_recovery(adv, tavern_seated)
    stamina = flush_stamina_to_db(adv, now=settle_now, tavern_seated=resolved_tavern_seated)
    hp = flush_hp_to_db(adv, now=settle_now, tavern_seated=resolved_tavern_seated)
    return stamina, hp
