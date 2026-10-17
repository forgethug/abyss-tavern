"""Add player-confirmed S1 fate reset requests and generation lease fields."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if 'adventurer_s1_fate_journeys' not in tables:
        return

    existing_columns = {
        column['name']
        for column in inspector.get_columns('adventurer_s1_fate_journeys')
    }
    statements: list[str] = []
    if 'choice_generation_token' not in existing_columns:
        statements.append(
            'ALTER TABLE adventurer_s1_fate_journeys '
            'ADD COLUMN choice_generation_token VARCHAR(64) NULL'
        )
    if 'choice_generation_started_at' not in existing_columns:
        statements.append(
            'ALTER TABLE adventurer_s1_fate_journeys '
            'ADD COLUMN choice_generation_started_at TIMESTAMP NULL'
        )

    if 'adventurer_s1_fate_reset_requests' not in tables:
        id_column = 'id SERIAL PRIMARY KEY'
        json_column = 'JSONB'
        if db.engine.dialect.name != 'postgresql':
            id_column = 'id INTEGER PRIMARY KEY AUTOINCREMENT'
            json_column = 'JSON'
        statements.append(f"""
            CREATE TABLE adventurer_s1_fate_reset_requests (
                {id_column},
                journey_id INTEGER NOT NULL REFERENCES adventurer_s1_fate_journeys(id) ON DELETE CASCADE,
                adventurer_id INTEGER NOT NULL REFERENCES adventurer_s1(id) ON DELETE CASCADE,
                requested_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                reason TEXT NULL,
                journey_snapshot {json_column} NOT NULL,
                requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                responded_at TIMESTAMP NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

    index_statements = [
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_fate_journeys_choice_generation_token '
        'ON adventurer_s1_fate_journeys (choice_generation_token)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_fate_reset_requests_journey_id '
        'ON adventurer_s1_fate_reset_requests (journey_id)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_fate_reset_requests_adventurer_id '
        'ON adventurer_s1_fate_reset_requests (adventurer_id)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_fate_reset_requests_requested_by_user_id '
        'ON adventurer_s1_fate_reset_requests (requested_by_user_id)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_fate_reset_requests_status '
        'ON adventurer_s1_fate_reset_requests (status)',
        'CREATE INDEX IF NOT EXISTS idx_advs1_fate_reset_adv_status '
        'ON adventurer_s1_fate_reset_requests (adventurer_id, status)',
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
