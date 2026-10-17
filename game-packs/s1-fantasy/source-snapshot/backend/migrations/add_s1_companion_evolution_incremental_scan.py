"""Add persisted incremental scan tables for S1 companion evolution integrity."""

from sqlalchemy import inspect

from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1CompanionEvolutionScanEvent,
    AdventurerS1CompanionEvolutionScanState,
)


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if 'adventurer_s1' not in tables:
        return

    with db.engine.begin() as connection:
        for model in (
            AdventurerS1CompanionEvolutionScanState,
            AdventurerS1CompanionEvolutionScanEvent,
        ):
            table = model.__table__
            table.create(bind=connection, checkfirst=True)
            for index in table.indexes:
                index.create(bind=connection, checkfirst=True)


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
