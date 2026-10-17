"""Extend S1 world-story threads with shared-campaign spine metadata."""
from __future__ import annotations

from typing import Any

from sqlalchemy import inspect, text

from extensions import db


_TABLE = 'adventurer_s1_world_story_threads'
_MIGRATION_LOCK_KEY = 'gupiao:s1_world_campaign_spine_migration:v1'
_COLUMN_DEFINITIONS = {
    'campaign_key': 'VARCHAR(64)',
    'campaign_root_id': 'INTEGER',
    'campaign_role': 'VARCHAR(24)',
    'campaign_agenda': 'TEXT',
    'campaign_spec_hash': 'VARCHAR(64)',
    'campaign_world': 'JSON',
}
_INDEX_DEFINITIONS = (
    (
        'uq_advs1_world_campaign_key',
        ('campaign_key',),
        True,
        f'CREATE UNIQUE INDEX uq_advs1_world_campaign_key '
        f'ON {_TABLE} (campaign_key)',
    ),
    (
        'ix_adventurer_s1_world_story_threads_campaign_root_id',
        ('campaign_root_id',),
        False,
        f'CREATE INDEX ix_adventurer_s1_world_story_threads_campaign_root_id '
        f'ON {_TABLE} (campaign_root_id)',
    ),
    (
        'ix_adventurer_s1_world_story_threads_campaign_role',
        ('campaign_role',),
        False,
        f'CREATE INDEX ix_adventurer_s1_world_story_threads_campaign_role '
        f'ON {_TABLE} (campaign_role)',
    ),
    (
        'idx_advs1_world_campaign_root_role',
        ('campaign_root_id', 'campaign_role'),
        False,
        f'CREATE INDEX idx_advs1_world_campaign_root_role '
        f'ON {_TABLE} (campaign_root_id, campaign_role)',
    ),
)


def _index_matches(index: dict, columns: tuple[str, ...], unique: bool) -> bool:
    return (
        tuple(index.get('column_names') or ()) == columns
        and bool(index.get('unique')) is unique
    )


def migrate(*, engine=None) -> dict[str, Any]:
    """Idempotently add campaign columns and repair index uniqueness.

    The generic auto-migrator cannot promote an existing ordinary index to a
    unique one, so this migration explicitly verifies and rebuilds the
    ``campaign_key`` index when required.
    """
    target_engine = engine or db.engine
    stats: dict[str, Any] = {
        'table_found': False,
        'columns_added': [],
        'indexes_created': [],
        'indexes_rebuilt': [],
    }
    with target_engine.begin() as connection:
        if target_engine.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )
        inspector = inspect(connection)
        if _TABLE not in inspector.get_table_names():
            return stats
        stats['table_found'] = True
        existing_columns = {
            column['name'] for column in inspector.get_columns(_TABLE)
        }
        existing_indexes = {
            index['name']: index for index in inspector.get_indexes(_TABLE)
        }
        for column_name, definition in _COLUMN_DEFINITIONS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(
                f'ALTER TABLE {_TABLE} ADD COLUMN {column_name} {definition}'
            ))
            stats['columns_added'].append(column_name)

        for index_name, columns, unique, create_sql in _INDEX_DEFINITIONS:
            existing = existing_indexes.get(index_name)
            if existing and _index_matches(existing, columns, unique):
                continue
            if existing:
                connection.execute(text(f'DROP INDEX IF EXISTS {index_name}'))
                stats['indexes_rebuilt'].append(index_name)
            else:
                stats['indexes_created'].append(index_name)
            connection.execute(text(create_sql))

    verified = inspect(target_engine)
    verified_columns = {
        column['name'] for column in verified.get_columns(_TABLE)
    }
    missing_columns = sorted(set(_COLUMN_DEFINITIONS) - verified_columns)
    if missing_columns:
        raise RuntimeError(
            f'{_TABLE} campaign columns missing after migration: {missing_columns}'
        )
    verified_indexes = {
        index['name']: index for index in verified.get_indexes(_TABLE)
    }
    for index_name, columns, unique, _ in _INDEX_DEFINITIONS:
        index = verified_indexes.get(index_name)
        if not index or not _index_matches(index, columns, unique):
            raise RuntimeError(
                f'{_TABLE} campaign index invalid after migration: {index_name}'
            )
    return stats


if __name__ == '__main__':
    migrate()
