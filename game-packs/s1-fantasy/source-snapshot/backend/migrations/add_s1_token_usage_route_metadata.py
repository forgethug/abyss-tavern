"""Add non-sensitive AI route and failover metadata to S1 token usage."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_token_usages'
_COLUMN_DDLS = {
    'api_key_slot': 'VARCHAR(24)',
    'api_endpoint_slot': 'VARCHAR(24)',
    'transport_attempt': 'INTEGER',
    'failover_reason': 'VARCHAR(160)',
    'failover_scope': 'VARCHAR(40)',
    'failover_attempt_count': 'INTEGER',
}


def migrate() -> None:
    inspector = inspect(db.engine)
    if _TABLE_NAME not in inspector.get_table_names():
        return

    with db.engine.begin() as conn:
        existing = {
            column['name']
            for column in inspector.get_columns(_TABLE_NAME)
        }
        for column_name, column_ddl in _COLUMN_DDLS.items():
            if column_name in existing:
                continue
            conn.execute(text(
                f'ALTER TABLE {_TABLE_NAME} '
                f'ADD COLUMN {column_name} {column_ddl}'
            ))
            existing.add(column_name)


def rollback() -> None:
    """Columns are retained because SQLite cannot safely drop them in place."""


if __name__ == '__main__':
    from app import app

    with app.app_context():
        migrate()
        print('S1 token usage route metadata migration complete')
