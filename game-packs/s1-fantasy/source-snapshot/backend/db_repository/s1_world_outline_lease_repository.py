"""Database ownership checks for S1 world-outline maintenance."""
from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime, timedelta

from db_repository import db_manager


def maintenance_claim_exists(now: datetime, *, expired: bool = False):
    """Correlated predicate; legacy dungeon tokens retain their full lease."""
    from plugins.adventurer_s1.models import (
        AdventurerS1WorldFactCandidate as Candidate,
        AdventurerS1WorldOutlineDungeonState as State,
    )

    return State.query.filter(
        State.target_dungeon == Candidate.target_dungeon,
        State.maintenance_claim_token.isnot(None),
        State.maintenance_claim_token != '',
        State.maintenance_claim_expires_at.isnot(None),
        (
            State.maintenance_claim_expires_at <= now
            if expired else State.maintenance_claim_expires_at > now
        ),
    ).exists()


def renew_maintenance_claim(
    *, dungeon: str, candidate_ids: list[int], candidate_token: str,
    dungeon_token: str, outline_revision: int, lease_seconds: int,
) -> bool:
    """Renew both ownership layers in one short transaction, state before rows."""
    from plugins.adventurer_s1.models import (
        AdventurerS1WorldFactCandidate as Candidate,
        AdventurerS1WorldOutlineDungeonState as State,
    )

    with db_manager.transaction():
        state = (
            State.query.filter_by(target_dungeon=dungeon)
            .with_for_update().populate_existing().first()
        )
        now = _portable_utc_now()
        if not (
            state
            and state.maintenance_claim_token == dungeon_token
            and int(state.outline_revision or 0) == outline_revision
            and state.maintenance_claim_expires_at
            and state.maintenance_claim_expires_at > now
        ):
            return False
        rows = (
            Candidate.query.filter(
                Candidate.id.in_(candidate_ids),
                Candidate.target_dungeon == dungeon,
                Candidate.status == 'processing',
                Candidate.outline_error == candidate_token,
            ).with_for_update().populate_existing().all()
            if candidate_ids else []
        )
        if len(rows) != len(candidate_ids):
            return False
        state.maintenance_claim_expires_at = now + timedelta(seconds=lease_seconds)
        state.updated_at = now
        for row in rows:
            row.updated_at = now
    return True
