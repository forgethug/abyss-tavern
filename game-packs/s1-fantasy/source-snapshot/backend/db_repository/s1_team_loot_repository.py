"""Bounded reads and the lot -> participants -> adventurers lock order."""
from __future__ import annotations

from sqlalchemy.orm import lazyload
from sqlalchemy import and_, func

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1, AdventurerS1Currency, AdventurerS1ExpeditionLog, AdventurerS1Item,
)
from plugins.adventurer_s1.team_loot_models import (
    AdventurerS1TeamLootLot as Lot, AdventurerS1TeamLootParticipant as Participant,
)


class S1TeamLootRepository:
    @staticmethod
    def lot(lot_id, *, lock=False):
        query = db.session.query(Lot).filter(Lot.id == lot_id)
        if lock:
            query = query.populate_existing().with_for_update()
        return query.one_or_none()

    @staticmethod
    def lock_lots(lot_ids):
        return db.session.query(Lot).filter(Lot.id.in_(lot_ids)).order_by(Lot.id).populate_existing().with_for_update().all()

    @staticmethod
    def participants(lot_ids, *, lock=False):
        query = db.session.query(Participant).filter(Participant.lot_id.in_(lot_ids)).order_by(
            Participant.lot_id, Participant.adventurer_id,
        )
        if lock:
            query = query.populate_existing().with_for_update()
        return query.all()

    @staticmethod
    def adventurers(adventurer_ids, *, lock=False):
        query = db.session.query(AdventurerS1).options(lazyload('*')).filter(
            AdventurerS1.id.in_(adventurer_ids),
        ).order_by(AdventurerS1.id)
        if lock:
            query = query.populate_existing().with_for_update()
        return {row.id: row for row in query.all()}

    @staticmethod
    def participant_profiles(adventurer_ids):
        if not adventurer_ids:
            return {}
        rows = db.session.query(
            AdventurerS1.id, AdventurerS1.owner_id, AdventurerS1.name, AdventurerS1.status,
            func.coalesce(AdventurerS1Currency.amount, AdventurerS1.gold).label('gold_balance'),
        ).outerjoin(AdventurerS1Currency, and_(
            AdventurerS1Currency.adventurer_id == AdventurerS1.id,
            AdventurerS1Currency.currency_code == 'gold',
        )).filter(AdventurerS1.id.in_(set(adventurer_ids))).all()
        return {row.id: row for row in rows}

    @staticmethod
    def source_lots(log_id):
        return db.session.query(Lot).filter(Lot.expedition_log_id == log_id).order_by(Lot.id).all()

    @staticmethod
    def catalogs(names):
        return {row.name: row for row in db.session.query(AdventurerS1Item).filter(
            AdventurerS1Item.name.in_(names),
        ).populate_existing().all()}

    @staticmethod
    def delivery_source(lot_id, name, adventurer_id, quantity):
        return db.session.query(Lot).filter_by(
            id=lot_id, item_name=name, winner_adventurer_id=adventurer_id,
            quantity=quantity, status='claim_pending',
        ).one_or_none()

    @staticmethod
    def log(log_id, *, lock=False):
        query = db.session.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == log_id)
        if lock:
            query = query.populate_existing().with_for_update()
        return query.one_or_none()

    @staticmethod
    def add_lots(lots):
        db.session.add_all(lots)
        db.session.flush()

    @staticmethod
    def add_participants(rows):
        if rows:
            db.session.execute(Participant.__table__.insert(), rows)

    @staticmethod
    def list_for_user(user_id, *, page, per_page, log_id=None, include_history=False):
        owned = db.session.query(Participant.lot_id).filter(Participant.user_id == user_id)
        query = db.session.query(Lot).filter(Lot.id.in_(owned))
        if log_id is not None:
            query = query.filter(Lot.expedition_log_id == log_id)
        if not include_history:
            query = query.filter(Lot.status.in_(('voting', 'bidding', 'rolling', 'claim_pending')))
        return query.order_by(Lot.created_at.desc(), Lot.id.desc()).paginate(
            page=page, per_page=per_page, error_out=False,
        )

    @staticmethod
    def pending_counts_for_user(user_id):
        owned = db.session.query(Participant.lot_id).filter(Participant.user_id == user_id)
        return dict(db.session.query(Lot.status, func.count(Lot.id)).filter(
            Lot.id.in_(owned),
            Lot.status.in_(('voting', 'bidding', 'rolling', 'claim_pending')),
        ).group_by(Lot.status).all())

    @staticmethod
    def has_history(user_id, log_id=None):
        query = db.session.query(Lot.id).join(Participant, Participant.lot_id == Lot.id).filter(
            Participant.user_id == user_id, Lot.status.in_(('settled', 'unclaimed', 'cancelled')),
        )
        if log_id is not None:
            query = query.filter(Lot.expedition_log_id == log_id)
        return query.limit(1).first() is not None

    @staticmethod
    def due_ids(now, limit):
        return [row[0] for row in db.session.query(Lot.id).filter(
            Lot.status.in_(('voting', 'bidding', 'rolling', 'claim_pending')),
            Lot.next_action_at <= now,
        ).order_by(Lot.next_action_at, Lot.id).limit(limit).all()]
