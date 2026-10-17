"""Add visible reasoning character counts to S1 token usage records."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_token_usages'
_COLUMN_NAME = 'visible_reasoning_chars'


def migrate() -> None:
    inspector = inspect(db.engine)
    if _TABLE_NAME not in inspector.get_table_names():
        return

    existing = {
        column['name']
        for column in inspector.get_columns(_TABLE_NAME)
    }
    if _COLUMN_NAME in existing:
        return

    with db.engine.begin() as conn:
        conn.execute(text(
            f'ALTER TABLE {_TABLE_NAME} '
            f'ADD COLUMN {_COLUMN_NAME} INTEGER'
        ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
