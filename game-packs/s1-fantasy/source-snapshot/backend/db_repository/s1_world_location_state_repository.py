"""Exact, bounded location authority reads before review or after a conflict."""

from sqlalchemy import or_, select

from extensions import db
from plugins.adventurer_s1.models import AdventurerS1WorldLocationState


def list_proposed_location_states(
    target_dungeon: str, *, location_ids: set[int], location_names: set[str],
) -> list[dict]:
    """Only resolve identities actually present in the validated proposal."""
    if not target_dungeon or not (location_ids or location_names):
        return []
    state = AdventurerS1WorldLocationState
    statement = (
        select(
            state.id, state.canonical_name, state.revision,
            state.current_state, state.public_risk_notice,
        )
        .filter(
            state.target_dungeon == target_dungeon,
            state.current_state != '',
            or_(
                state.id.in_(location_ids),
                state.canonical_name.in_(location_names),
            ),
        )
        .order_by(state.id)
    )
    # Review preparation reads committed authority, never pending settlement
    # writes. Do not leave a scoped transaction open for rendering/checkpoints.
    with db.engine.connect() as connection:
        connection = connection.execution_options(isolation_level='AUTOCOMMIT')
        rows = connection.execute(statement).all()
    return [{
        'location_id': row.id,
        'location_name': row.canonical_name,
        'revision': row.revision,
        'current_state': row.current_state,
        'public_risk_notice': row.public_risk_notice,
    } for row in rows]
