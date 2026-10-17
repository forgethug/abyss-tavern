"""Create the realm-local physical team-loot ledger; no historical asset writes."""
from sqlalchemy import inspect

from extensions import db
from plugins.adventurer_s1.team_loot_models import (
    AdventurerS1TeamLootLot, AdventurerS1TeamLootParticipant,
)


def migrate():
    if 'adventurer_s1_expedition_logs' not in inspect(db.engine).get_table_names():
        return
    with db.engine.begin() as connection:
        AdventurerS1TeamLootLot.__table__.create(connection, checkfirst=True)
        AdventurerS1TeamLootParticipant.__table__.create(connection, checkfirst=True)
