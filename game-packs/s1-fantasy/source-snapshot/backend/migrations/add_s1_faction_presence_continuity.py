"""Add the soft, per-profile-thread S1 faction-presence clock."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    table_name = 'adventurer_s1_faction_profile_threads'
    inspector = inspect(db.engine)
    if table_name not in inspector.get_table_names():
        return

    columns = {column['name'] for column in inspector.get_columns(table_name)}
    indexes = {index['name'] for index in inspector.get_indexes(table_name)}
    statements: list[str] = []
    column_definitions = {
        'presence_debt': 'INTEGER NOT NULL DEFAULT 0',
        'presence_cooldown_runs': 'INTEGER NOT NULL DEFAULT 0',
        'last_presence_log_id': 'INTEGER',
    }
    for column_name, definition in column_definitions.items():
        if column_name not in columns:
            statements.append(
                f'ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}'
            )

    index_definitions = {
        'ix_adventurer_s1_faction_profile_threads_presence_debt': (
            'presence_debt'
        ),
        'ix_adventurer_s1_faction_profile_threads_last_presence_log_id': (
            'last_presence_log_id'
        ),
        'idx_advs1_faction_presence_due': (
            'presence_debt, presence_cooldown_runs'
        ),
    }
    for index_name, columns_sql in index_definitions.items():
        if index_name not in indexes:
            statements.append(
                f'CREATE INDEX {index_name} ON {table_name} ({columns_sql})'
            )

    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
