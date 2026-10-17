"""Add S1 match ticket solo-fill switch."""
from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    if 'adventurer_s1_match_tickets' not in set(inspector.get_table_names()):
        return

    existing = {
        col['name']
        for col in inspector.get_columns('adventurer_s1_match_tickets')
    }
    if 'allow_solo_fill' in existing:
        return

    with db.engine.begin() as conn:
        conn.execute(text(
            'ALTER TABLE adventurer_s1_match_tickets '
            'ADD COLUMN allow_solo_fill BOOLEAN NOT NULL DEFAULT FALSE'
        ))


if __name__ == '__main__':
    from app import create_app
    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
