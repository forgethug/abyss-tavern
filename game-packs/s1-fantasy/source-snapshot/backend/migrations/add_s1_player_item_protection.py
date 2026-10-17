"""Add the S1 player item protection flag to item instances."""

from sqlalchemy import inspect, text

from extensions import db


_MIGRATION_LOCK_KEY = 'gupiao:s1_player_item_protection:v1'
_TABLES = (
    'adventurer_s1_equipment',
    'adventurer_s1_warehouse_items',
)


def migrate() -> None:
    """Idempotently add protection columns before any S1 ORM read uses them."""
    with db.engine.begin() as connection:
        if db.engine.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )

        table_names = set(inspect(connection).get_table_names())
        for table_name in _TABLES:
            if table_name not in table_names:
                continue
            column_names = {
                column['name']
                for column in inspect(connection).get_columns(table_name)
            }
            if 'is_player_locked' in column_names:
                continue
            connection.execute(text(
                f'ALTER TABLE "{table_name}" '
                'ADD COLUMN "is_player_locked" BOOLEAN NOT NULL DEFAULT FALSE'
            ))
