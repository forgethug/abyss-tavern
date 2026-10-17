"""Add S1 solo matchmaking dungeon pool."""
from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    if 'adventurer_s1_match_tickets' not in set(inspector.get_table_names()):
        return
    existing = {col['name'] for col in inspector.get_columns('adventurer_s1_match_tickets')}
    if 'preferred_dungeons' in existing:
        return
    column_type = 'JSONB' if db.engine.dialect.name == 'postgresql' else 'JSON'
    with db.engine.begin() as conn:
        conn.execute(text(
            f'ALTER TABLE adventurer_s1_match_tickets ADD COLUMN preferred_dungeons {column_type} NULL'
        ))


if __name__ == '__main__':
    from app import create_app
    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
