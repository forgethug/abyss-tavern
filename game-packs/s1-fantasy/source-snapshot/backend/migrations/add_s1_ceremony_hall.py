"""Add S1 Star Lantern Hall birthday profiles, receipts, and social visits."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if 'users' not in tables:
        return

    statements: list[str] = []
    if 'adventurer_s1_ceremony_profiles' not in tables:
        statements.append("""
            CREATE TABLE adventurer_s1_ceremony_profiles (
                user_id INTEGER PRIMARY KEY,
                birthday_month INTEGER NOT NULL,
                birthday_day INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT ck_advs1_ceremony_birthday_month
                    CHECK (birthday_month BETWEEN 1 AND 12),
                CONSTRAINT ck_advs1_ceremony_birthday_day
                    CHECK (birthday_day BETWEEN 1 AND 31)
            )
        """)

    if 'adventurer_s1_ceremony_attendances' not in tables:
        id_column = (
            'id SERIAL PRIMARY KEY'
            if db.engine.dialect.name == 'postgresql'
            else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
        )
        statements.append(f"""
            CREATE TABLE adventurer_s1_ceremony_attendances (
                {id_column},
                user_id INTEGER NOT NULL,
                ceremony_type VARCHAR(40) NOT NULL,
                occurrence_year INTEGER NOT NULL,
                birthday_month INTEGER NOT NULL,
                birthday_day INTEGER NOT NULL,
                wish_key VARCHAR(32) NOT NULL,
                celebrant_name VARCHAR(64) NOT NULL,
                host_snapshot JSON,
                guest_snapshot JSON,
                feast_snapshot JSON,
                tavern_blessing TEXT,
                visit_token VARCHAR(32),
                visitors_open BOOLEAN NOT NULL DEFAULT FALSE,
                visitors_opened_at TIMESTAMP,
                gift_claimed_at TIMESTAMP,
                gift_destination VARCHAR(20),
                gift_adventurer_id INTEGER,
                attended_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_ceremony_attendance_year
                    UNIQUE (user_id, ceremony_type, occurrence_year)
            )
        """)
    else:
        attendance_columns = {
            column['name']
            for column in inspector.get_columns(
                'adventurer_s1_ceremony_attendances'
            )
        }
        attendance_column_definitions = {
            'host_snapshot': 'JSON',
            'guest_snapshot': 'JSON',
            'feast_snapshot': 'JSON',
            'tavern_blessing': 'TEXT',
            'visit_token': 'VARCHAR(32)',
            'visitors_open': 'BOOLEAN NOT NULL DEFAULT FALSE',
            'visitors_opened_at': 'TIMESTAMP',
            'gift_claimed_at': 'TIMESTAMP',
            'gift_destination': 'VARCHAR(20)',
            'gift_adventurer_id': 'INTEGER',
        }
        for column_name, column_type in attendance_column_definitions.items():
            if column_name not in attendance_columns:
                statements.append(
                    'ALTER TABLE adventurer_s1_ceremony_attendances '
                    f'ADD COLUMN {column_name} {column_type}'
                )

    if 'adventurer_s1_ceremony_visits' not in tables:
        id_column = (
            'id SERIAL PRIMARY KEY'
            if db.engine.dialect.name == 'postgresql'
            else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
        )
        statements.append(f"""
            CREATE TABLE adventurer_s1_ceremony_visits (
                {id_column},
                attendance_id INTEGER NOT NULL,
                visitor_user_id INTEGER NOT NULL,
                visitor_name VARCHAR(64) NOT NULL,
                action_key VARCHAR(32) NOT NULL,
                message VARCHAR(160),
                request_token VARCHAR(32),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_advs1_ceremony_visit_attendance
                    FOREIGN KEY (attendance_id)
                    REFERENCES adventurer_s1_ceremony_attendances (id)
                    ON DELETE CASCADE
            )
        """)
    else:
        visit_columns = {
            column['name']
            for column in inspector.get_columns('adventurer_s1_ceremony_visits')
        }
        if 'request_token' not in visit_columns:
            statements.append(
                'ALTER TABLE adventurer_s1_ceremony_visits '
                'ADD COLUMN request_token VARCHAR(32)'
            )

    index_statements = [
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_ceremony_attendances_user_id '
        'ON adventurer_s1_ceremony_attendances (user_id)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_ceremony_attendances_ceremony_type '
        'ON adventurer_s1_ceremony_attendances (ceremony_type)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_ceremony_attendances_occurrence_year '
        'ON adventurer_s1_ceremony_attendances (occurrence_year)',
        'CREATE INDEX IF NOT EXISTS idx_advs1_ceremony_user_attended '
        'ON adventurer_s1_ceremony_attendances (user_id, attended_at)',
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_advs1_ceremony_visit_token '
        'ON adventurer_s1_ceremony_attendances (visit_token)',
        'CREATE INDEX IF NOT EXISTS idx_advs1_ceremony_open_tables '
        'ON adventurer_s1_ceremony_attendances '
        '(ceremony_type, visitors_open, attended_at)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_ceremony_visits_attendance_id '
        'ON adventurer_s1_ceremony_visits (attendance_id)',
        'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_ceremony_visits_visitor_user_id '
        'ON adventurer_s1_ceremony_visits (visitor_user_id)',
        'CREATE INDEX IF NOT EXISTS idx_advs1_ceremony_visit_table_time '
        'ON adventurer_s1_ceremony_visits (attendance_id, created_at, id)',
        'CREATE INDEX IF NOT EXISTS idx_advs1_ceremony_visit_user_table_time '
        'ON adventurer_s1_ceremony_visits '
        '(visitor_user_id, attendance_id, created_at)',
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_advs1_ceremony_visit_request '
        'ON adventurer_s1_ceremony_visits '
        '(attendance_id, visitor_user_id, request_token)',
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
