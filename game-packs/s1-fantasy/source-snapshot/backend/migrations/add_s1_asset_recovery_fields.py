"""Add the unified S1 skill/equipment recovery contract and instance timers."""

from sqlalchemy import inspect, text

from extensions import db


_MIGRATION_LOCK_KEY = 'gupiao:s1_asset_recovery_fields:v1'

_TABLE_COLUMNS = {
    'adventurer_s1_items': (
        ('recovery_minutes', 'INTEGER NOT NULL DEFAULT 0'),
    ),
    'adventurer_s1_equipment': (
        ('recovery_minutes', 'INTEGER NOT NULL DEFAULT 0'),
        ('recovery_started_at', 'TIMESTAMP NULL'),
        ('recovery_ends_at', 'TIMESTAMP NULL'),
    ),
    'adventurer_s1_warehouse_items': (
        ('recovery_minutes', 'INTEGER NOT NULL DEFAULT 0'),
        ('recovery_started_at', 'TIMESTAMP NULL'),
        ('recovery_ends_at', 'TIMESTAMP NULL'),
    ),
    'adventurer_s1_skills': (
        ('recovery_minutes', 'INTEGER NOT NULL DEFAULT 0'),
        ('recovery_started_at', 'TIMESTAMP NULL'),
        ('recovery_ends_at', 'TIMESTAMP NULL'),
    ),
    'adventurer_s1_market_listings': (
        ('recovery_minutes', 'INTEGER NOT NULL DEFAULT 0'),
        ('recovery_started_at', 'TIMESTAMP NULL'),
        ('recovery_ends_at', 'TIMESTAMP NULL'),
    ),
}

_INDEXES = (
    ('ix_adventurer_s1_equipment_recovery_ends_at', 'adventurer_s1_equipment'),
    ('ix_adventurer_s1_warehouse_items_recovery_ends_at', 'adventurer_s1_warehouse_items'),
    ('ix_adventurer_s1_skills_recovery_ends_at', 'adventurer_s1_skills'),
)


def migrate() -> None:
    with db.engine.begin() as connection:
        if db.engine.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table_name, definitions in _TABLE_COLUMNS.items():
            if table_name not in tables:
                continue
            existing = {
                column['name']
                for column in inspect(connection).get_columns(table_name)
            }
            for column_name, ddl in definitions:
                if column_name in existing:
                    continue
                connection.execute(text(
                    f'ALTER TABLE "{table_name}" '
                    f'ADD COLUMN "{column_name}" {ddl}'
                ))
                existing.add(column_name)

        for index_name, table_name in _INDEXES:
            if table_name not in tables:
                continue
            existing_indexes = {
                index['name']
                for index in inspect(connection).get_indexes(table_name)
            }
            if index_name in existing_indexes:
                continue
            connection.execute(text(
                f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                f'ON "{table_name}" ("recovery_ends_at")'
            ))
