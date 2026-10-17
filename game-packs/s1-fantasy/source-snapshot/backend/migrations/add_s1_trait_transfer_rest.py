"""Preserve natural recovery progress across exact-instance transfers."""

from sqlalchemy import inspect, text

from extensions import db


_MIGRATION_LOCK_KEY = 'gupiao:s1_trait_transfer_rest:v1'
_TABLE_NAME = 'adventurer_s1_traits'
_COLUMN_NAME = 'recovery_rest_baseline_persistent'


def migrate() -> None:
    with db.engine.begin() as connection:
        if db.engine.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )
        inspector = inspect(connection)
        if _TABLE_NAME not in set(inspector.get_table_names()):
            return
        existing = {
            column['name']
            for column in inspect(connection).get_columns(_TABLE_NAME)
        }
        if _COLUMN_NAME in existing:
            return
        connection.execute(text(
            f'ALTER TABLE "{_TABLE_NAME}" '
            f'ADD COLUMN "{_COLUMN_NAME}" BOOLEAN NOT NULL DEFAULT FALSE'
        ))
