"""Extend S1 ambient NPC receipts for AI gifts and daily limits.

The migration deliberately leaves historical receipt rows with NULL daily
fields.  PostgreSQL and SQLite both allow multiple NULL values through the
four-column unique index, so legacy interactions retain their original
encounter-scoped semantics.
"""

from sqlalchemy import inspect, text

from extensions import db


_INTERACTION_TABLE = 'adventurer_s1_ambient_npc_interactions'
_TOKEN_USAGE_TABLE = 'adventurer_s1_token_usages'
_DAILY_INDEX = 'uq_advs1_ambient_npc_daily_action'
_DAILY_INDEX_COLUMNS = (
    'adventurer_id',
    'npc_key',
    'daily_limit_key',
    'interaction_day',
)
_TOKEN_CORRELATION_INDEX = (
    'ix_adventurer_s1_token_usages_correlation_id'
)

_INTERACTION_COLUMN_DDLS = {
    'interaction_day': 'DATE',
    'daily_limit_key': 'VARCHAR(32)',
    'item_type': 'VARCHAR(50)',
    'item_rarity': 'VARCHAR(20)',
    'item_snapshot_hash': 'VARCHAR(64)',
    'item_snapshot': 'JSON',
    'item_remaining_quantity': 'INTEGER',
    'item_removed': 'BOOLEAN',
    'ai_cache_key': 'VARCHAR(64)',
    'ai_result_source': 'VARCHAR(20)',
    'ai_affinity_effect': 'VARCHAR(24)',
    'ai_response_texts': 'JSON',
    'ai_metadata': 'JSON',
    'ai_model': 'VARCHAR(100)',
    'ai_provider': 'VARCHAR(32)',
    'ai_profile_id': 'VARCHAR(64)',
    'ai_provider_request_id': 'VARCHAR(160)',
    'ai_failure_stage': 'VARCHAR(80)',
}

_TOKEN_USAGE_COLUMN_DDLS = {
    'correlation_id': 'VARCHAR(64)',
}


def _quote(identifier: str) -> str:
    """Quote a migration-owned static identifier for both supported DBs."""
    return f'"{identifier}"'


def _table_exists(connection, table_name: str) -> bool:
    return table_name in inspect(connection).get_table_names()


def _add_missing_columns(connection, table_name: str, column_ddls: dict) -> None:
    if not _table_exists(connection, table_name):
        return
    existing = {
        column['name']
        for column in inspect(connection).get_columns(table_name)
    }
    for column_name, column_ddl in column_ddls.items():
        if column_name in existing:
            continue
        connection.execute(text(
            f'ALTER TABLE {_quote(table_name)} '
            f'ADD COLUMN {_quote(column_name)} {column_ddl}'
        ))
        existing.add(column_name)


def _matching_unique_constraint(connection):
    for constraint in inspect(connection).get_unique_constraints(
        _INTERACTION_TABLE,
    ):
        if constraint.get('name') == _DAILY_INDEX:
            return constraint
    return None


def _matching_index(connection, table_name: str, index_name: str):
    for index in inspect(connection).get_indexes(table_name):
        if index.get('name') == index_name:
            return index
    return None


def _ensure_daily_unique_index(connection) -> None:
    if not _table_exists(connection, _INTERACTION_TABLE):
        return

    expected_columns = list(_DAILY_INDEX_COLUMNS)
    constraint = _matching_unique_constraint(connection)
    if constraint is not None:
        actual_columns = list(constraint.get('column_names') or [])
        if actual_columns != expected_columns:
            raise RuntimeError(
                f'{_DAILY_INDEX} exists with unexpected columns: '
                f'{actual_columns!r}'
            )
        return

    existing_index = _matching_index(
        connection,
        _INTERACTION_TABLE,
        _DAILY_INDEX,
    )
    if existing_index is not None:
        actual_columns = list(existing_index.get('column_names') or [])
        if (
            bool(existing_index.get('unique'))
            and actual_columns == expected_columns
        ):
            return
        # auto_migrate historically created every model Index as non-unique.
        # This name is owned by the ambient-NPC model, so repair it explicitly.
        connection.execute(text(
            f'DROP INDEX IF EXISTS {_quote(_DAILY_INDEX)}'
        ))

    columns_sql = ', '.join(_quote(name) for name in _DAILY_INDEX_COLUMNS)
    connection.execute(text(
        f'CREATE UNIQUE INDEX IF NOT EXISTS {_quote(_DAILY_INDEX)} '
        f'ON {_quote(_INTERACTION_TABLE)} ({columns_sql})'
    ))

    verified = _matching_index(
        connection,
        _INTERACTION_TABLE,
        _DAILY_INDEX,
    )
    if (
        verified is None
        or not bool(verified.get('unique'))
        or list(verified.get('column_names') or []) != expected_columns
    ):
        raise RuntimeError(
            f'failed to verify unique daily ambient NPC index {_DAILY_INDEX}'
        )


def _ensure_token_correlation_index(connection) -> None:
    if not _table_exists(connection, _TOKEN_USAGE_TABLE):
        return
    existing = _matching_index(
        connection,
        _TOKEN_USAGE_TABLE,
        _TOKEN_CORRELATION_INDEX,
    )
    expected_columns = ['correlation_id']
    if existing is not None:
        if list(existing.get('column_names') or []) == expected_columns:
            return
        connection.execute(text(
            f'DROP INDEX IF EXISTS {_quote(_TOKEN_CORRELATION_INDEX)}'
        ))
    connection.execute(text(
        f'CREATE INDEX IF NOT EXISTS {_quote(_TOKEN_CORRELATION_INDEX)} '
        f'ON {_quote(_TOKEN_USAGE_TABLE)} ({_quote("correlation_id")})'
    ))


def migrate() -> None:
    # Defer the plugin-model import until the application has initialized db.
    from plugins.adventurer_s1.models import (
        AdventurerS1AmbientNpcGiftAiCache,
    )

    with db.engine.begin() as connection:
        _add_missing_columns(
            connection,
            _INTERACTION_TABLE,
            _INTERACTION_COLUMN_DDLS,
        )
        _add_missing_columns(
            connection,
            _TOKEN_USAGE_TABLE,
            _TOKEN_USAGE_COLUMN_DDLS,
        )
        AdventurerS1AmbientNpcGiftAiCache.__table__.create(
            bind=connection,
            checkfirst=True,
        )
        _ensure_daily_unique_index(connection)
        _ensure_token_correlation_index(connection)


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
