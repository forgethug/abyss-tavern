"""Add stable call-chain diagnostics to S1 Observer review records."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_expedition_observer_reviews'
_CORRELATION_INDEX = 'ix_advs1_observer_review_correlation'
_COLUMN_DDLS = {
    'correlation_id': 'VARCHAR(64)',
    'effective_model_used': 'VARCHAR(200)',
}


def _quote(identifier: str) -> str:
    return f'"{identifier}"'


def _matching_index(connection):
    for index in inspect(connection).get_indexes(_TABLE_NAME):
        if index.get('name') == _CORRELATION_INDEX:
            return index
    return None


def migrate() -> None:
    with db.engine.begin() as connection:
        inspector = inspect(connection)
        if _TABLE_NAME not in inspector.get_table_names():
            return

        existing_columns = {
            column['name']
            for column in inspector.get_columns(_TABLE_NAME)
        }
        for column_name, column_ddl in _COLUMN_DDLS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(
                f'ALTER TABLE {_quote(_TABLE_NAME)} '
                f'ADD COLUMN {_quote(column_name)} {column_ddl}'
            ))
            existing_columns.add(column_name)

        existing_index = _matching_index(connection)
        expected_columns = ['correlation_id']
        if existing_index is not None:
            if list(existing_index.get('column_names') or []) == expected_columns:
                return
            connection.execute(text(
                f'DROP INDEX IF EXISTS {_quote(_CORRELATION_INDEX)}'
            ))
        connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS {_quote(_CORRELATION_INDEX)} '
            f'ON {_quote(_TABLE_NAME)} ({_quote("correlation_id")})'
        ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
