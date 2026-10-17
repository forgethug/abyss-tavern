"""Full failed-call evidence, loaded separately from the token-usage list."""
from sqlalchemy import inspect, text
from extensions import db

_TABLE_NAME = 'adventurer_s1_token_usages'


def migrate():
    table = _TABLE_NAME
    inspector = inspect(db.engine)
    if table not in inspector.get_table_names():
        return
    existing = {column['name'] for column in inspector.get_columns(table)}
    with db.engine.begin() as connection:
        for name, ddl in {
            'has_failure_detail': 'BOOLEAN NOT NULL DEFAULT FALSE',
            'failure_detail': 'TEXT',
        }.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
