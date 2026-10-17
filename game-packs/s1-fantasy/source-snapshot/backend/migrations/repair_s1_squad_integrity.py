"""Install the squad membership foreign key omitted by legacy auto-migrate."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from extensions import db


_ADVISORY_LOCK_KEY = 'gupiao:s1_squad_integrity:v1'
_CONSTRAINTS = (
    (
        'fk_adventurer_s1_squad_id',
        'adventurer_s1',
        'squad_id',
        'adventurer_s1_squads',
        'id',
        'n',
        """
        ALTER TABLE adventurer_s1
        ADD CONSTRAINT fk_adventurer_s1_squad_id
        FOREIGN KEY (squad_id)
        REFERENCES adventurer_s1_squads (id)
        ON DELETE SET NULL
        NOT VALID
        """,
    ),
)


def _foreign_key_rows(
    connection,
    *,
    table_name: str,
    column_name: str,
    referred_table: str,
    referred_column: str,
):
    return connection.execute(text("""
        SELECT
            constraint_row.conname,
            constraint_row.convalidated,
            constraint_row.confdeltype
        FROM pg_constraint constraint_row
        WHERE constraint_row.contype = 'f'
          AND constraint_row.conrelid = to_regclass(:qualified_table)
          AND constraint_row.confrelid = to_regclass(:qualified_referred_table)
          AND constraint_row.conkey = ARRAY[(
              SELECT attribute_row.attnum
              FROM pg_attribute attribute_row
              WHERE attribute_row.attrelid = constraint_row.conrelid
                AND attribute_row.attname = :column_name
                AND NOT attribute_row.attisdropped
          )]::smallint[]
          AND constraint_row.confkey = ARRAY[(
              SELECT attribute_row.attnum
              FROM pg_attribute attribute_row
              WHERE attribute_row.attrelid = constraint_row.confrelid
                AND attribute_row.attname = :referred_column
                AND NOT attribute_row.attisdropped
          )]::smallint[]
    """), {
        'qualified_table': f'public.{table_name}',
        'qualified_referred_table': f'public.{referred_table}',
        'column_name': column_name,
        'referred_column': referred_column,
    }).all()


def _named_constraint_exists(connection, table_name: str, constraint_name: str):
    return bool(connection.execute(text("""
        SELECT EXISTS (
            SELECT 1
            FROM pg_constraint constraint_row
            WHERE constraint_row.conrelid = to_regclass(:qualified_table)
              AND constraint_row.conname = :constraint_name
        )
    """), {
        'qualified_table': f'public.{table_name}',
        'constraint_name': constraint_name,
    }).scalar())


def migrate(*, engine=None) -> dict[str, Any]:
    """Add and verify the adventurer-to-squad membership foreign key.

    ``auto_migrate`` creates missing tables and columns, but intentionally does
    not add a foreign key when it appends a column to an existing table. This
    idempotent PostgreSQL repair closes that upgrade-path gap without deleting
    or rewriting ambiguous data. ``leader_adventurer_id`` remains a checked weak
    reference because legacy ``adventurer_s1.id`` has no database-level unique
    constraint that PostgreSQL can safely reference.
    """
    target_engine = engine or db.engine
    stats: dict[str, Any] = {
        'tables_found': False,
        'constraints_added': [],
        'constraints_reused': [],
        'constraints_validated': [],
    }
    if target_engine.dialect.name != 'postgresql':
        return stats

    with target_engine.begin() as connection:
        connection.execute(
            text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
            {'lock_key': _ADVISORY_LOCK_KEY},
        )
        tables_found = bool(connection.execute(text("""
            SELECT
                to_regclass('public.adventurer_s1') IS NOT NULL
                AND to_regclass('public.adventurer_s1_squads') IS NOT NULL
        """)).scalar())
        stats['tables_found'] = tables_found
        if not tables_found:
            return stats

        required_columns = {
            ('adventurer_s1', 'squad_id'),
            ('adventurer_s1_squads', 'id'),
            ('adventurer_s1_squads', 'leader_adventurer_id'),
        }
        present_columns = {
            (row[0], row[1])
            for row in connection.execute(text("""
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN (
                      'adventurer_s1',
                      'adventurer_s1_squads'
                  )
            """)).all()
        }
        missing_columns = sorted(required_columns - present_columns)
        if missing_columns:
            raise RuntimeError(
                'S1 squad auto-migrate did not create required columns: '
                + ', '.join(f'{table}.{column}' for table, column in missing_columns)
            )

        integrity_counts = connection.execute(text("""
            SELECT
                (
                    SELECT COUNT(*)
                    FROM adventurer_s1 adventurer
                    LEFT JOIN adventurer_s1_squads squad
                      ON squad.id = adventurer.squad_id
                    WHERE adventurer.squad_id IS NOT NULL
                      AND squad.id IS NULL
                ),
                (
                    SELECT COUNT(*)
                    FROM adventurer_s1_squads squad
                    LEFT JOIN adventurer_s1 leader
                      ON leader.id = squad.leader_adventurer_id
                    WHERE leader.id IS NULL
                ),
                (
                    SELECT COUNT(*)
                    FROM adventurer_s1_squads squad
                    JOIN adventurer_s1 leader
                      ON leader.id = squad.leader_adventurer_id
                    WHERE leader.squad_id IS DISTINCT FROM squad.id
                )
        """)).first()
        orphan_members = int(integrity_counts[0] or 0)
        orphan_leaders = int(integrity_counts[1] or 0)
        detached_leaders = int(integrity_counts[2] or 0)
        if orphan_members or orphan_leaders or detached_leaders:
            raise RuntimeError(
                'S1 squad integrity requires explicit review: '
                f'orphan_members={orphan_members}, '
                f'orphan_leaders={orphan_leaders}, '
                f'detached_leaders={detached_leaders}'
            )

        for (
            constraint_name,
            table_name,
            column_name,
            referred_table,
            referred_column,
            delete_action,
            create_sql,
        ) in _CONSTRAINTS:
            rows = _foreign_key_rows(
                connection,
                table_name=table_name,
                column_name=column_name,
                referred_table=referred_table,
                referred_column=referred_column,
            )
            if rows and any(row[2] != delete_action for row in rows):
                raise RuntimeError(
                    f'{table_name}.{column_name} has a foreign key with an '
                    'unexpected ON DELETE action'
                )
            if (
                rows
                and all(row[0] != constraint_name for row in rows)
                and _named_constraint_exists(
                    connection,
                    table_name,
                    constraint_name,
                )
            ):
                raise RuntimeError(
                    f'{constraint_name} exists with an unexpected definition'
                )
            if not rows:
                if _named_constraint_exists(
                    connection,
                    table_name,
                    constraint_name,
                ):
                    raise RuntimeError(
                        f'{constraint_name} exists with an unexpected definition'
                    )
                connection.execute(text(create_sql))
                stats['constraints_added'].append(constraint_name)
                rows = _foreign_key_rows(
                    connection,
                    table_name=table_name,
                    column_name=column_name,
                    referred_table=referred_table,
                    referred_column=referred_column,
                )
            else:
                stats['constraints_reused'].extend(
                    row[0] for row in rows if row[0] != constraint_name
                )

            for actual_name, is_validated, _ in rows:
                if is_validated:
                    continue
                quoted_name = (
                    connection.dialect.identifier_preparer.quote(actual_name)
                )
                connection.execute(text(
                    f'ALTER TABLE {table_name} '
                    f'VALIDATE CONSTRAINT {quoted_name}'
                ))
                stats['constraints_validated'].append(actual_name)

            verified_rows = _foreign_key_rows(
                connection,
                table_name=table_name,
                column_name=column_name,
                referred_table=referred_table,
                referred_column=referred_column,
            )
            if (
                not verified_rows
                or any(row[2] != delete_action for row in verified_rows)
                or not all(bool(row[1]) for row in verified_rows)
            ):
                raise RuntimeError(
                    f'{constraint_name} validation did not take effect'
                )

    return stats


if __name__ == '__main__':
    migrate()
