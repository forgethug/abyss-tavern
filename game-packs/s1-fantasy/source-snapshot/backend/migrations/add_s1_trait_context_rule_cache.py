"""Add independently reviewed trait-context fields to the S1 cache."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_description_ai_cache'
_COLUMN_DEFINITIONS = {
    'trait_context_rule': 'JSON',
    'trait_context_audit_status': (
        "VARCHAR(24) NOT NULL DEFAULT 'not_sampled'"
    ),
}


def migrate(*, engine=None) -> None:
    bind = engine or db.engine
    inspector = inspect(bind)
    if _TABLE_NAME not in inspector.get_table_names():
        return
    existing = {
        column['name']
        for column in inspector.get_columns(_TABLE_NAME)
    }
    missing = [
        (name, definition)
        for name, definition in _COLUMN_DEFINITIONS.items()
        if name not in existing
    ]
    if not missing:
        return
    with bind.begin() as conn:
        for name, definition in missing:
            conn.execute(text(
                f'ALTER TABLE {_TABLE_NAME} ADD COLUMN {name} {definition}'
            ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
