"""Add an independent semantic-terminal retry counter to S1 description cache."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_description_ai_cache'
_COLUMN_NAME = 'terminal_retry_count'


def migrate(*, engine=None) -> None:
    bind = engine or db.engine
    inspector = inspect(bind)
    if _TABLE_NAME not in inspector.get_table_names():
        return
    existing = {
        column['name']
        for column in inspector.get_columns(_TABLE_NAME)
    }
    with bind.begin() as conn:
        if _COLUMN_NAME not in existing:
            conn.execute(text(
                f'ALTER TABLE {_TABLE_NAME} '
                f'ADD COLUMN {_COLUMN_NAME} INTEGER NOT NULL DEFAULT 0'
            ))
        conn.execute(text(
            f'UPDATE {_TABLE_NAME} SET {_COLUMN_NAME} = 0 '
            f'WHERE {_COLUMN_NAME} IS NULL'
        ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
