"""Add the structured trait-context review receipt to the S1 cache."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_description_ai_cache'
_COLUMN_NAME = 'trait_context_audit_receipt'


def migrate(*, engine=None) -> None:
    bind = engine or db.engine
    inspector = inspect(bind)
    if _TABLE_NAME not in inspector.get_table_names():
        return
    existing = {
        column['name']
        for column in inspector.get_columns(_TABLE_NAME)
    }
    if _COLUMN_NAME in existing:
        return
    with bind.begin() as conn:
        conn.execute(text(
            f'ALTER TABLE {_TABLE_NAME} ADD COLUMN {_COLUMN_NAME} JSON'
        ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
