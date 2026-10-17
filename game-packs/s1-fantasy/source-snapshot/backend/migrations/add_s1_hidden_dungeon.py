"""为 S1 探险日志增加实际进入的隐藏副本字段。"""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    table = 'adventurer_s1_expedition_logs'
    inspector = inspect(db.engine)
    columns = {column['name'] for column in inspector.get_columns(table)}
    with db.engine.begin() as connection:
        if 'hidden_dungeon' not in columns:
            connection.execute(text(
                f'ALTER TABLE {table} ADD COLUMN hidden_dungeon VARCHAR(100) NULL'
            ))
        index_names = {index['name'] for index in inspect(connection).get_indexes(table)}
        index_name = 'ix_adventurer_s1_expedition_logs_hidden_dungeon'
        if index_name not in index_names:
            connection.execute(text(
                f'CREATE INDEX {index_name} ON {table} (hidden_dungeon)'
            ))


if __name__ == '__main__':
    migrate()
