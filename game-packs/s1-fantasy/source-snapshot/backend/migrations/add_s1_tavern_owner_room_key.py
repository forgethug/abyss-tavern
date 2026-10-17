"""Add a stable nullable key for S1 system-owned dorm rooms."""

from sqlalchemy import inspect, text

from extensions import db


_MIGRATION_LOCK_KEY = 'gupiao:s1_tavern_owner_room_key:v1'
_TABLE_NAME = 'adventurer_s1_dorm_rooms'
_INDEX_NAME = 'uq_advs1_dorm_room_key'


def _quote(identifier: str) -> str:
    return f'"{identifier}"'


def _find_index(connection):
    for index in inspect(connection).get_indexes(_TABLE_NAME):
        if index.get('name') == _INDEX_NAME:
            return index
    return None


def migrate() -> None:
    with db.engine.begin() as connection:
        if db.engine.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )

        inspector = inspect(connection)
        if _TABLE_NAME not in inspector.get_table_names():
            return

        existing_columns = {
            column['name']
            for column in inspect(connection).get_columns(_TABLE_NAME)
        }
        if 'room_key' not in existing_columns:
            connection.execute(text(
                f'ALTER TABLE {_quote(_TABLE_NAME)} '
                'ADD COLUMN "room_key" VARCHAR(50) NULL'
            ))

        existing_index = _find_index(connection)
        expected_columns = ['room_key']
        if existing_index is not None and (
            not bool(existing_index.get('unique'))
            or list(existing_index.get('column_names') or []) != expected_columns
        ):
            connection.execute(text(
                f'DROP INDEX IF EXISTS {_quote(_INDEX_NAME)}'
            ))
            existing_index = None

        if existing_index is None:
            connection.execute(text(
                f'CREATE UNIQUE INDEX IF NOT EXISTS {_quote(_INDEX_NAME)} '
                f'ON {_quote(_TABLE_NAME)} ("room_key")'
            ))

        verified = _find_index(connection)
        if (
            verified is None
            or not bool(verified.get('unique'))
            or list(verified.get('column_names') or []) != expected_columns
        ):
            raise RuntimeError(
                f'failed to verify unique dorm room key index {_INDEX_NAME}'
            )


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
