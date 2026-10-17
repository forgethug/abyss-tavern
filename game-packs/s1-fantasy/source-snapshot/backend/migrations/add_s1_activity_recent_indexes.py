"""Add S1 activity indexes without blocking normal PostgreSQL writes.

Run ``migrate()`` in an initialized application context, before deploying the
model indexes to an existing database. New databases get the same indexes from
model metadata. Each concurrent build commits separately; rerunning verifies
completed indexes and resumes missing ones. An interrupted/invalid index needs
explicit operator recovery and is never silently skipped or dropped.
"""

from sqlalchemy import text

from extensions import db


_INDEXES = (
    ('adventurer_s1_currency_logs', 'idx_advs1_currency_log_adv_recent'),
    ('adventurer_s1_item_logs', 'idx_advs1_item_log_adv_recent'),
    ('adventurer_s1_material_logs', 'idx_advs1_material_log_adv_recent'),
    ('adventurer_s1_building_usage_logs', 'idx_advs1_usage_log_adv_recent'),
)
_COLUMNS = ['adventurer_id', 'created_at', 'id']


def _verify_index(connection, table_name, index_name):
    row = connection.execute(text("""
        SELECT i.indisvalid, i.indisready, i.indisunique,
               i.indpred IS NULL AS unfiltered,
               i.indrelid = to_regclass(:table_name) AS correct_table,
               am.amname AS method,
               ARRAY(SELECT pg_get_indexdef(i.indexrelid, n, true)
                     FROM generate_series(1, i.indnatts) AS n) AS columns
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_am am ON am.oid = c.relam
        WHERE i.indexrelid = to_regclass(:index_name)
    """), {
        'table_name': f'public.{table_name}',
        'index_name': f'public.{index_name}',
    }).mappings().first()
    if row is None:
        return False
    if not (
        row['indisvalid'] and row['indisready'] and not row['indisunique']
        and row['unfiltered'] and row['correct_table']
        and row['method'] == 'btree' and list(row['columns']) == _COLUMNS
    ):
        raise RuntimeError(f'{index_name}: invalid or unexpected index; inspect before recovery')
    return True


def migrate():
    """Create/verify only these four indexes; never change business rows."""
    if db.engine.dialect.name != 'postgresql':
        raise RuntimeError('This online migration requires PostgreSQL')
    completed = []
    with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        previous = {
            key: connection.execute(text(f'SHOW {key}')).scalar()
            for key in ('statement_timeout', 'lock_timeout')
        }
        try:
            connection.execute(text("SET statement_timeout='120s'"))
            connection.execute(text("SET lock_timeout='5s'"))
            for table_name, index_name in _INDEXES:
                if not _verify_index(connection, table_name, index_name):
                    # Identifiers are migration-owned constants, never user input.
                    connection.execute(text(
                        f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} '
                        f'ON public.{table_name} (adventurer_id, created_at, id)'
                    ))
                    if not _verify_index(connection, table_name, index_name):
                        raise RuntimeError(f'{index_name}: index missing after creation')
                completed.append(index_name)
        finally:
            # SET is session-scoped in AUTOCOMMIT; restore before pool return.
            try:
                for key, value in previous.items():
                    connection.execute(text('SELECT set_config(:key, :value, false)'),
                                       {'key': key, 'value': value})
            except Exception:
                connection.invalidate()
                raise
    return completed
