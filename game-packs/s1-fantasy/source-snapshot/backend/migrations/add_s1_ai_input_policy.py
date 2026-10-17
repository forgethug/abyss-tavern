"""Add an internal, content-bound AI input policy to the existing cache."""
from sqlalchemy import inspect, text
from extensions import db

def migrate(*, engine=None):
    bind = engine or db.engine
    table = 'adventurer_s1_description_ai_cache'
    with bind.begin() as connection:
        if bind.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                {'key': 's1_ai_input_policy_v1'},
            )
        inspector = inspect(connection)
        if table not in inspector.get_table_names():
            return
        if 'ai_input_policy' not in {c['name'] for c in inspector.get_columns(table)}:
            connection.execute(text(
                'ALTER TABLE adventurer_s1_description_ai_cache ADD COLUMN ai_input_policy JSON'
            ))
