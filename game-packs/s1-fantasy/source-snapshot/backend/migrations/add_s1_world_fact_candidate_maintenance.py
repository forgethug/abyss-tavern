"""Add persisted S1 world-fact maintenance diagnostics to legacy tables."""

from sqlalchemy import inspect, text

from extensions import db


_MIGRATION_LOCK_KEY = 'gupiao:s1_world_fact_candidate_maintenance:v1'
_TABLE_NAME = 'adventurer_s1_world_fact_candidates'
_COLUMN_DDLS = (
    ('maintenance_issue_code', 'VARCHAR(64)'),
    ('maintenance_receipt', 'JSON'),
    ('maintenance_recovery_kind', 'VARCHAR(32)'),
    ('maintenance_recovery_attempts', 'INTEGER NOT NULL DEFAULT 0'),
)
_INDEX_DDLS = (
    (
        'idx_advs1_world_fact_candidate_issue',
        'maintenance_issue_code',
    ),
    (
        'idx_advs1_world_fact_candidate_recovery',
        'maintenance_recovery_kind',
    ),
)


def _quote(identifier: str) -> str:
    return f'"{identifier}"'


def migrate() -> None:
    """Idempotently add maintenance state before any S1 ORM read uses it."""
    with db.engine.begin() as connection:
        if db.engine.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )

        inspector = inspect(connection)
        if _TABLE_NAME not in inspector.get_table_names():
            return

        existing_columns = {
            column['name']
            for column in inspector.get_columns(_TABLE_NAME)
        }
        for column_name, column_ddl in _COLUMN_DDLS:
            if column_name in existing_columns:
                continue
            connection.execute(text(
                f'ALTER TABLE {_quote(_TABLE_NAME)} '
                f'ADD COLUMN {_quote(column_name)} {column_ddl}'
            ))
            existing_columns.add(column_name)

        for index_name, column_name in _INDEX_DDLS:
            connection.execute(text(
                f'CREATE INDEX IF NOT EXISTS {_quote(index_name)} '
                f'ON {_quote(_TABLE_NAME)} ({_quote(column_name)})'
            ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
