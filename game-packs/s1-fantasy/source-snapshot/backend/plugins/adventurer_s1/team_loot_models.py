"""Persist expedition-owned loot independently of the temporary party lifetime."""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime

from extensions import db


class AdventurerS1TeamLootLot(db.Model):
    __tablename__ = 'adventurer_s1_team_loot_lots'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    expedition_log_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_expedition_logs.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    source_key = db.Column(db.String(64), nullable=False)
    source_event_index = db.Column(db.Integer, nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    item_data = db.Column(db.JSON, nullable=False)
    evidence_event_indexes = db.Column(db.JSON, nullable=False)
    provenance = db.Column(db.JSON, nullable=False)
    party_name = db.Column(db.String(100), nullable=True)
    target_dungeon = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='voting')
    mode = db.Column(db.String(12), nullable=True)
    deadline_at = db.Column(db.DateTime, nullable=False)
    next_action_at = db.Column(db.DateTime, nullable=False)
    contender_ids = db.Column(db.JSON, nullable=False, default=list)
    roll_rounds = db.Column(db.JSON, nullable=False, default=list)
    winner_adventurer_id = db.Column(db.Integer, nullable=True)
    winning_cents = db.Column(db.BigInteger, nullable=False, default=0)
    receipt = db.Column(db.JSON, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, onupdate=_portable_utc_now)
    __table_args__ = (
        db.UniqueConstraint('expedition_log_id', 'source_key', name='uq_s1_team_loot_source'),
        db.CheckConstraint('quantity > 0 AND winning_cents >= 0', name='ck_s1_team_loot_amounts'),
        db.Index('ix_s1_team_loot_due', 'status', 'next_action_at', 'id'),
    )


class AdventurerS1TeamLootParticipant(db.Model):
    __tablename__ = 'adventurer_s1_team_loot_participants'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lot_id = db.Column(
        db.Integer, db.ForeignKey('adventurer_s1_team_loot_lots.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    adventurer_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    adventurer_name = db.Column(db.String(100), nullable=False)
    vote = db.Column(db.String(12), nullable=True)
    bid_cents = db.Column(db.BigInteger, nullable=False, default=0)
    held_cents = db.Column(db.BigInteger, nullable=False, default=0)
    __table_args__ = (
        db.UniqueConstraint('lot_id', 'adventurer_id', name='uq_s1_team_loot_participant'),
        db.CheckConstraint('bid_cents >= 0 AND held_cents >= 0', name='ck_s1_team_loot_escrow'),
        db.Index('ix_s1_team_loot_owner', 'user_id', 'lot_id'),
    )
