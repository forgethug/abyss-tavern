"""Add tea house stamina settlement cursor."""

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
    if 'stamina_settled_at' not in columns:
        db.session.execute(text(
            'ALTER TABLE adventurer_s1_tea_house_seats '
            'ADD COLUMN stamina_settled_at TIMESTAMP NULL'
        ))
        db.session.commit()


if __name__ == '__main__':
    migrate()
