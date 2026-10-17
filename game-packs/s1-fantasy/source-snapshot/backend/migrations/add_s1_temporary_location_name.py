"""Add the published destination label without loading full_log in list APIs."""
from sqlalchemy import inspect, text
from extensions import db


def migrate() -> None:
    table = 'adventurer_s1_expedition_logs'
    columns = {column['name'] for column in inspect(db.engine).get_columns(table)}
    if 'temporary_location_name' not in columns:
        with db.engine.begin() as connection:
            connection.execute(text(
                f'ALTER TABLE {table} ADD COLUMN temporary_location_name VARCHAR(100) NULL'
            ))


if __name__ == '__main__':
    migrate()
