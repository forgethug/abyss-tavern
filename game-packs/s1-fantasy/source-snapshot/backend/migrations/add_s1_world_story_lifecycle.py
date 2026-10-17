"""Add versioning, rotation and closure lifecycle fields to S1 world story threads."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    table_name = 'adventurer_s1_world_story_threads'
    if table_name not in inspector.get_table_names():
        return

    columns = {column['name'] for column in inspector.get_columns(table_name)}
    indexes = {index['name'] for index in inspector.get_indexes(table_name)}
    statements = []
    column_definitions = {
        'revision': "INTEGER NOT NULL DEFAULT 1",
        'injection_count': "INTEGER NOT NULL DEFAULT 0",
        'missed_injection_runs': "INTEGER NOT NULL DEFAULT 0",
        'closure_debt': "INTEGER NOT NULL DEFAULT 0",
        'last_injected_log_id': "INTEGER",
        'last_progress_log_id': "INTEGER",
    }
    for column_name, definition in column_definitions.items():
        if column_name not in columns:
            statements.append(
                f'ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}'
            )

    index_definitions = {
        'ix_adventurer_s1_world_story_threads_closure_debt': 'closure_debt',
        'ix_adventurer_s1_world_story_threads_last_injected_log_id': 'last_injected_log_id',
        'ix_adventurer_s1_world_story_threads_last_progress_log_id': 'last_progress_log_id',
    }
    for index_name, column_name in index_definitions.items():
        if index_name not in indexes:
            statements.append(
                f'CREATE INDEX {index_name} ON {table_name} ({column_name})'
            )

    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
