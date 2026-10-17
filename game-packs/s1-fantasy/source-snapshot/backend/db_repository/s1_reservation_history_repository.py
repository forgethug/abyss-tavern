"""Read only the current reservation owner's published NPC appearances."""

from sqlalchemy import tuple_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionLog as Log,
    AdventurerS1Reservation as Reservation,
    AdventurerS1WaitingNpcActor as Actor,
    AdventurerS1WaitingNpcRecord as Record,
)


def load_reservation_appearances(user_id, realm_code, *, page, per_page):
    latest_reservation = db_manager.query(Reservation.id).filter(
        Reservation.user_id == user_id,
        Reservation.realm_code == realm_code,
        Reservation.status == 'pending',
    ).order_by(
        Reservation.created_at.desc(), Reservation.id.desc(),
    ).limit(1).correlate(None).scalar_subquery()
    base = db_manager.query(Record).join(
        Actor, Actor.id == Record.actor_id,
    ).join(
        Reservation, Reservation.id == Actor.reservation_id,
    ).join(Log, Log.id == Record.expedition_log_id).filter(
        Reservation.id == latest_reservation,
        Reservation.user_id == user_id,
        Reservation.realm_code == realm_code,
        Reservation.status == 'pending',
        Reservation.restore_adventurer_id.is_(None),
        Actor.user_id == user_id,
        Actor.status != 'materialized',
        Log.status == 'completed',
    )
    # A single appearance can contain several excerpts. Paginate appearances,
    # not evidence rows, and keep earlier incarnations in the owner's history.
    appearances = base.with_entities(
        Record.actor_id, Record.incarnation, Record.expedition_log_id,
        Log.created_at, Log.target_dungeon, Log.hidden_dungeon,
        Log.temporary_location_name, Log.party_name,
    ).distinct()
    total = appearances.count()
    page = min(max(page, 1), max(1, (total + per_page - 1) // per_page))
    rows = appearances.order_by(
        Log.created_at.desc(), Record.expedition_log_id.desc(),
        Record.incarnation.desc(), Record.actor_id.desc(),
    ).offset((page - 1) * per_page).limit(per_page).all() if total else []
    keys = [(row.actor_id, row.incarnation, row.expedition_log_id) for row in rows]
    records = base.with_entities(
        Record.actor_id, Record.incarnation, Record.expedition_log_id,
        Record.excerpt,
    ).filter(tuple_(
        Record.actor_id, Record.incarnation, Record.expedition_log_id,
    ).in_(keys)).order_by(
        Record.event_index, Record.record_index, Record.id,
    ).all() if keys else []
    return rows, records, total, page
