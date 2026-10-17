"""Add comments for S1 expedition paragraphs and member updates."""

from sqlalchemy import inspect

from extensions import db
from plugins.adventurer_s1.models import AdventurerS1ExpeditionComment


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    required_tables = {'users', 'adventurer_s1_expedition_logs'}
    if not required_tables.issubset(tables):
        return

    comment_table = AdventurerS1ExpeditionComment.__table__
    with db.engine.begin() as connection:
        comment_table.create(bind=connection, checkfirst=True)
        for index in comment_table.indexes:
            index.create(bind=connection, checkfirst=True)


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
