"""Add per-member S1 matchmaking fate-risk acknowledgements."""
from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    if 'adventurer_s1_match_tickets' not in set(inspector.get_table_names()):
        return
    existing = {
        column['name']
        for column in inspector.get_columns('adventurer_s1_match_tickets')
    }
    if 'fate_risk_acknowledgements' in existing:
        return
    column_type = 'JSONB' if db.engine.dialect.name == 'postgresql' else 'JSON'
    with db.engine.begin() as conn:
        conn.execute(text(
            'ALTER TABLE adventurer_s1_match_tickets '
            f'ADD COLUMN fate_risk_acknowledgements {column_type} NULL'
        ))


if __name__ == '__main__':
    from app import create_app
    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
