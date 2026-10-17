"""Add tea house order display fields."""

from sqlalchemy import inspect, text

from extensions import db


def migrate():
    inspector = inspect(db.engine)
    if 'adventurer_s1_tea_house_seats' not in inspector.get_table_names():
        return

    columns = {
        col['name']
        for col in inspector.get_columns('adventurer_s1_tea_house_seats')
    }
    statements = []
    if 'tea_key' not in columns:
        statements.append(
            'ALTER TABLE adventurer_s1_tea_house_seats '
            'ADD COLUMN tea_key VARCHAR(50) NULL'
        )
    if 'tea_text' not in columns:
        statements.append(
            'ALTER TABLE adventurer_s1_tea_house_seats '
            'ADD COLUMN tea_text VARCHAR(200) NULL'
        )
    if 'ordered_at' not in columns:
        statements.append(
            'ALTER TABLE adventurer_s1_tea_house_seats '
            'ADD COLUMN ordered_at TIMESTAMP NULL'
        )

    for stmt in statements:
        db.session.execute(text(stmt))
    if statements:
        db.session.commit()


if __name__ == '__main__':
    migrate()
