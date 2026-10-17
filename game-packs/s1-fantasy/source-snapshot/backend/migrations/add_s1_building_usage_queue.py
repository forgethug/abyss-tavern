"""Add the S1 generic async-building FIFO queue table."""

from sqlalchemy import inspect, text

from extensions import db


TABLE_NAME = 'adventurer_s1_building_usage_queue_entries'


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if not {
        'adventurer_s1',
        'adventurer_s1_building_instances',
    }.issubset(tables):
        return

    statements: list[str] = []
    if TABLE_NAME not in tables:
        id_column = 'id SERIAL PRIMARY KEY'
        if db.engine.dialect.name != 'postgresql':
            id_column = 'id INTEGER PRIMARY KEY AUTOINCREMENT'
        statements.append(f"""
            CREATE TABLE {TABLE_NAME} (
                {id_column},
                building_instance_id INTEGER NOT NULL
                    REFERENCES adventurer_s1_building_instances(id) ON DELETE CASCADE,
                adventurer_id INTEGER NOT NULL
                    REFERENCES adventurer_s1(id) ON DELETE CASCADE,
                queued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

    index_statements = [
        f'CREATE INDEX IF NOT EXISTS '
        f'ix_advs1_building_usage_queue_building '
        f'ON {TABLE_NAME} (building_instance_id)',
        f'CREATE UNIQUE INDEX IF NOT EXISTS '
        f'ix_adventurer_s1_building_usage_queue_entries_adventurer_id '
        f'ON {TABLE_NAME} (adventurer_id)',
        f'CREATE INDEX IF NOT EXISTS '
        f'ix_adventurer_s1_building_usage_queue_entries_queued_at '
        f'ON {TABLE_NAME} (queued_at)',
        f'CREATE INDEX IF NOT EXISTS ix_advs1_building_usage_queue_fifo '
        f'ON {TABLE_NAME} (building_instance_id, queued_at, id)',
    ]

    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        for statement in index_statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
