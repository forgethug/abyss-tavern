"""Repair legacy PostgreSQL integrity for the S1 narrative lease table."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from extensions import db


_TABLE = 'adventurer_s1_narrative_leases'
_SEQUENCE = 'adventurer_s1_narrative_leases_id_seq'
_PRIMARY_KEY = 'adventurer_s1_narrative_leases_pkey'
_ADVISORY_LOCK_KEY = 'gupiao:s1_narrative_lease_integrity:v1'

_INDEX_SPECS = (
    (
        'uq_advs1_narrative_lease_active_actor',
        ('world_actor_id',),
        ('status', 'active', 'world_actor_id', 'is not null'),
        """
        CREATE UNIQUE INDEX uq_advs1_narrative_lease_active_actor
        ON adventurer_s1_narrative_leases (world_actor_id)
        WHERE status = 'active' AND world_actor_id IS NOT NULL
        """,
    ),
    (
        'uq_advs1_narrative_lease_active_thread',
        ('world_story_thread_id',),
        (
            'status',
            'active',
            'world_story_thread_id',
            'is not null',
            'lease_type',
            'world_story_thread',
        ),
        """
        CREATE UNIQUE INDEX uq_advs1_narrative_lease_active_thread
        ON adventurer_s1_narrative_leases (world_story_thread_id)
        WHERE status = 'active'
          AND world_story_thread_id IS NOT NULL
          AND lease_type = 'world_story_thread'
        """,
    ),
)


def _has_id_primary_key(connection) -> bool:
    return bool(connection.execute(text("""
        SELECT EXISTS (
            SELECT 1
            FROM pg_constraint constraint_row
            JOIN pg_attribute attribute_row
              ON attribute_row.attrelid = constraint_row.conrelid
             AND attribute_row.attnum = ANY(constraint_row.conkey)
            WHERE constraint_row.conrelid = 'public.adventurer_s1_narrative_leases'::regclass
              AND constraint_row.contype = 'p'
            GROUP BY constraint_row.oid
            HAVING array_agg(
                attribute_row.attname::text
                ORDER BY attribute_row.attnum
            ) = ARRAY['id']::text[]
        )
    """)).scalar())


def _index_matches(
    connection,
    *,
    index_name: str,
    expected_columns: tuple[str, ...],
    predicate_tokens: tuple[str, ...],
) -> bool:
    row = connection.execute(text("""
        SELECT
            index_meta.indisunique,
            ARRAY(
                SELECT attribute_row.attname
                FROM unnest(index_meta.indkey) WITH ORDINALITY AS key_row(attnum, position)
                JOIN pg_attribute attribute_row
                  ON attribute_row.attrelid = index_meta.indrelid
                 AND attribute_row.attnum = key_row.attnum
                ORDER BY key_row.position
            ) AS indexed_columns,
            pg_get_expr(index_meta.indpred, index_meta.indrelid) AS predicate
        FROM pg_class index_row
        JOIN pg_namespace namespace_row ON namespace_row.oid = index_row.relnamespace
        JOIN pg_index index_meta ON index_meta.indexrelid = index_row.oid
        WHERE namespace_row.nspname = 'public'
          AND index_row.relname = :index_name
          AND index_meta.indrelid = 'public.adventurer_s1_narrative_leases'::regclass
    """), {'index_name': index_name}).first()
    if not row or not bool(row[0]) or tuple(row[1] or ()) != expected_columns:
        return False
    predicate = ' '.join(str(row[2] or '').lower().split())
    return all(token in predicate for token in predicate_tokens)


def _sync_id_sequence(connection) -> None:
    connection.execute(text(f'CREATE SEQUENCE IF NOT EXISTS {_SEQUENCE}'))
    connection.execute(text(
        f'ALTER SEQUENCE {_SEQUENCE} OWNED BY {_TABLE}.id'
    ))
    connection.execute(text(
        f"ALTER TABLE {_TABLE} ALTER COLUMN id "
        f"SET DEFAULT nextval('{_SEQUENCE}'::regclass)"
    ))
    connection.execute(text(f"""
        SELECT setval(
            '{_SEQUENCE}'::regclass,
            GREATEST(COALESCE((SELECT MAX(id) FROM {_TABLE}), 0), 1),
            EXISTS (SELECT 1 FROM {_TABLE})
        )
    """))


def migrate(*, engine=None) -> dict[str, Any]:
    """Restore ORM identity and active-lease constraints before S1 recovery.

    Old databases can contain this table without a real primary key. The
    sequence may then reuse an existing id, causing ORM updates to match more
    than one row. Duplicate rows are preserved: only later physical rows for
    the same id are assigned fresh sequence values.
    """
    target_engine = engine or db.engine
    stats: dict[str, Any] = {
        'table_found': False,
        'duplicate_ids_rekeyed': 0,
        'primary_key_added': False,
        'active_actor_conflicts': 0,
        'active_thread_conflicts': 0,
        'indexes_rebuilt': [],
    }
    if target_engine.dialect.name != 'postgresql':
        return stats

    with target_engine.begin() as connection:
        connection.execute(
            text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
            {'lock_key': _ADVISORY_LOCK_KEY},
        )
        table_found = bool(connection.execute(
            text("SELECT to_regclass('public.adventurer_s1_narrative_leases') IS NOT NULL")
        ).scalar())
        stats['table_found'] = table_found
        if not table_found:
            return stats

        # Prevent a live worker from consuming a just-resynchronized sequence
        # or inserting another active conflict before the constraints commit.
        connection.execute(text(
            f'LOCK TABLE {_TABLE} IN SHARE ROW EXCLUSIVE MODE'
        ))
        _sync_id_sequence(connection)
        if not _has_id_primary_key(connection):
            rekey_result = connection.execute(text(f"""
                WITH ranked AS (
                    SELECT
                        ctid,
                        ROW_NUMBER() OVER (
                            PARTITION BY id
                            ORDER BY created_at ASC NULLS FIRST, ctid
                        ) AS duplicate_rank
                    FROM {_TABLE}
                )
                UPDATE {_TABLE} lease_row
                SET id = nextval('{_SEQUENCE}'::regclass)
                FROM ranked
                WHERE lease_row.ctid = ranked.ctid
                  AND ranked.duplicate_rank > 1
            """))
            stats['duplicate_ids_rekeyed'] = max(int(rekey_result.rowcount or 0), 0)
            connection.execute(text(
                f'ALTER TABLE {_TABLE} ADD CONSTRAINT {_PRIMARY_KEY} PRIMARY KEY (id)'
            ))
            stats['primary_key_added'] = True
            _sync_id_sequence(connection)

        conflict_row = connection.execute(text(f"""
            SELECT
                (
                    SELECT COUNT(*)
                    FROM (
                        SELECT world_actor_id
                        FROM {_TABLE}
                        WHERE status = 'active' AND world_actor_id IS NOT NULL
                        GROUP BY world_actor_id
                        HAVING COUNT(*) > 1
                    ) actor_conflicts
                ),
                (
                    SELECT COUNT(*)
                    FROM (
                        SELECT world_story_thread_id
                        FROM {_TABLE}
                        WHERE status = 'active'
                          AND world_story_thread_id IS NOT NULL
                          AND lease_type = 'world_story_thread'
                        GROUP BY world_story_thread_id
                        HAVING COUNT(*) > 1
                    ) thread_conflicts
                )
        """)).first()
        stats['active_actor_conflicts'] = int(conflict_row[0] or 0)
        stats['active_thread_conflicts'] = int(conflict_row[1] or 0)
        if stats['active_actor_conflicts'] or stats['active_thread_conflicts']:
            raise RuntimeError(
                'S1 narrative lease active conflicts require explicit admin review: '
                f"actors={stats['active_actor_conflicts']}, "
                f"threads={stats['active_thread_conflicts']}"
            )

        for index_name, columns, predicate_tokens, create_sql in _INDEX_SPECS:
            if _index_matches(
                connection,
                index_name=index_name,
                expected_columns=columns,
                predicate_tokens=predicate_tokens,
            ):
                continue
            connection.execute(text(f'DROP INDEX IF EXISTS public.{index_name}'))
            connection.execute(text(create_sql))
            stats['indexes_rebuilt'].append(index_name)

        if not _has_id_primary_key(connection):
            raise RuntimeError(f'{_TABLE}.id primary key repair did not take effect')
        for index_name, columns, predicate_tokens, _ in _INDEX_SPECS:
            if not _index_matches(
                connection,
                index_name=index_name,
                expected_columns=columns,
                predicate_tokens=predicate_tokens,
            ):
                raise RuntimeError(f'{index_name} integrity repair did not take effect')

    return stats


if __name__ == '__main__':
    migrate()
