"""Locked S1 skill/trait ownership changes inside the caller's transaction."""
from db_repository.manager import db_manager
from sqlalchemy import and_, or_
from plugins.adventurer_s1.models import (
    AdventurerS1, AdventurerS1Skill, AdventurerS1Trait, AdventurerS1Equipment,
    AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog,
)


def lock_transfer_state(member_ids, *, lock_rows=True):
    ids = sorted(set(member_ids))
    query = db_manager.query(AdventurerS1).filter(
        AdventurerS1.id.in_(ids),
    ).order_by(AdventurerS1.id).populate_existing()
    members = (query.with_for_update() if lock_rows else query).all()
    assets = {}
    for kind, model in (('skill', AdventurerS1Skill), ('trait', AdventurerS1Trait), ('equipment', AdventurerS1Equipment)):
        query = db_manager.query(model).filter(
            model.adventurer_id.in_(ids),
        ).order_by(model.id).populate_existing()
        assets[kind] = (query.with_for_update() if lock_rows else query).all()
    return {row.id: row for row in members}, assets


def _audit_member_ids(rows):
    ids = set()
    for adventurer_id, op_type, receipt in rows:
        ids.add(int(adventurer_id))
        if op_type == 'transfer_asset' and isinstance(receipt, dict):
            for field in ('from_adventurer_id', 'to_adventurer_id'):
                value = receipt.get(field)
                if type(value) is int and value > 0:
                    ids.add(value)
    return ids


def appeal_affected_member_ids(appeal_id):
    audit = AdventurerS1DataAppealAuditLog
    rows = db_manager.query(audit.adventurer_id, audit.op_type, audit.after_json).filter(
        audit.appeal_id == appeal_id, audit.reverted.is_(False),
    ).all()
    return sorted(_audit_member_ids(rows))


def pending_revert_member_ids(member_ids):
    ids = set(member_ids)
    if not ids:
        return []
    audit = AdventurerS1DataAppealAuditLog
    rows = db_manager.query(audit.adventurer_id, audit.op_type, audit.after_json).join(
        AdventurerS1DataAppeal, AdventurerS1DataAppeal.id == audit.appeal_id,
    ).filter(
        AdventurerS1DataAppeal.cross_character_revert_status == 'pending',
        audit.reverted.is_(False),
        or_(audit.adventurer_id.in_(ids), and_(audit.op_type == 'transfer_asset', or_(
            audit.after_json['from_adventurer_id'].as_integer().in_(ids),
            audit.after_json['to_adventurer_id'].as_integer().in_(ids),
        ))),
    ).all()
    return sorted(_audit_member_ids(rows) & ids)
