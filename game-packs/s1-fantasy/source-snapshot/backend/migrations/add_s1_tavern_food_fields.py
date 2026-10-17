"""Add persistent tavern food display and settlement fields."""

from sqlalchemy import inspect, text

from extensions import db


def migrate():
    inspector = inspect(db.engine)
    table = 'adventurer_s1_tavern_seats'
    if table not in inspector.get_table_names():
        return
    columns = {col['name'] for col in inspector.get_columns(table)}
    definitions = {
        'food_key': 'VARCHAR(50) NULL',
        'food_text': 'VARCHAR(200) NULL',
        'food_started_at': 'TIMESTAMP NULL',
        'food_expires_at': 'TIMESTAMP NULL',
        'food_settled_at': 'TIMESTAMP NULL',
    }
    for name, definition in definitions.items():
        if name not in columns:
            db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
    index_names = {index['name'] for index in inspector.get_indexes(table)}
    if 'ix_adventurer_s1_tavern_seats_food_expires_at' not in index_names:
        db.session.execute(text(
            'CREATE INDEX ix_adventurer_s1_tavern_seats_food_expires_at '
            'ON adventurer_s1_tavern_seats (food_expires_at)'
        ))
    db.session.commit()


if __name__ == '__main__':
    migrate()
