"""Add versioned authoritative capability profiles to S1 world actors."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    if 'adventurer_s1_world_actors' not in set(inspector.get_table_names()):
        return
    columns = {column['name'] for column in inspector.get_columns('adventurer_s1_world_actors')}
    statements = []
    if 'capability_profile' not in columns:
        statements.append('ALTER TABLE adventurer_s1_world_actors ADD COLUMN capability_profile JSON')
    if 'capability_revision' not in columns:
        statements.append(
            'ALTER TABLE adventurer_s1_world_actors ADD COLUMN capability_revision INTEGER NOT NULL DEFAULT 0'
        )
    if 'capability_source_log_id' not in columns:
        statements.extend([
            'ALTER TABLE adventurer_s1_world_actors ADD COLUMN capability_source_log_id INTEGER',
            'CREATE INDEX ix_adventurer_s1_world_actors_capability_source_log_id '
            'ON adventurer_s1_world_actors (capability_source_log_id)',
        ])
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
