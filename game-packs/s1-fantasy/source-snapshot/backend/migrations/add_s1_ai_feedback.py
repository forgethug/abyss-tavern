"""Add S1 lightweight AI feedback and safe endpoint-host provenance."""

from sqlalchemy import inspect, text

from extensions import db
from plugins.adventurer_s1.models import AdventurerS1AiFeedback


_TOKEN_USAGE_TABLE = 'adventurer_s1_token_usages'


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if not {'users', _TOKEN_USAGE_TABLE}.issubset(tables):
        return

    with db.engine.begin() as connection:
        token_columns = {
            column['name']
            for column in inspector.get_columns(_TOKEN_USAGE_TABLE)
        }
        if 'endpoint_host' not in token_columns:
            connection.execute(text(
                f'ALTER TABLE {_TOKEN_USAGE_TABLE} '
                'ADD COLUMN endpoint_host VARCHAR(253)'
            ))

        feedback_table = AdventurerS1AiFeedback.__table__
        feedback_table.create(bind=connection, checkfirst=True)
        for index in feedback_table.indexes:
            index.create(bind=connection, checkfirst=True)


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
