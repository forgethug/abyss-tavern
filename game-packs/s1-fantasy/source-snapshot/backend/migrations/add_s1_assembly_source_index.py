"""Online PostgreSQL index for exact-name assembly provenance lookups.

Run migrate() in an initialized admin application context. Existing databases
require this explicit migration; startup auto-migration deliberately skips it.
An interrupted/invalid or differently defined index requires operator review.
"""

from sqlalchemy import text

from extensions import db


_INDEX_NAME = 'idx_advs1_assembly_source_name'
_CREATE_SQL = (
    'CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_advs1_assembly_source_name '
    'ON public.adventurer_s1_building_usage_logs '
    "((CAST(result ->> 'new_name' AS VARCHAR)), id) "
    "WHERE CAST(result ->> 'type' AS VARCHAR) = 'item_assembly_workshop'"
)


def _canonical_expression(value):
    # pg_get_indexdef/pg_get_expr render casts and parentheses differently from
    # CREATE INDEX. Ignore only these equivalent, unbounded string relabels.
    value = ''.join(str(value).split())
    for token in ('::charactervarying', '::text', '(', ')'):
        value = value.replace(token, '')
    return value


def _verify_index(connection):
    row = connection.execute(text("""
        SELECT i.indisvalid, i.indisready, i.indisunique,
               i.indrelid = to_regclass('public.adventurer_s1_building_usage_logs')
                   AS correct_table,
               am.amname AS method,
               pg_get_expr(i.indpred, i.indrelid) AS predicate,
               ARRAY(SELECT pg_get_indexdef(i.indexrelid, n, true)
                     FROM generate_series(1, i.indnatts) AS n) AS columns
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_am am ON am.oid = c.relam
        WHERE i.indexrelid = to_regclass(:index_name)
    """), {'index_name': f'public.{_INDEX_NAME}'}).mappings().first()
    if row is None:
        return False
    if not (
        row['indisvalid'] and row['indisready'] and not row['indisunique']
        and row['correct_table'] and row['method'] == 'btree'
        and [_canonical_expression(value) for value in row['columns']]
        == ["result->>'new_name'", 'id']
        and _canonical_expression(row['predicate'])
        == "result->>'type'='item_assembly_workshop'"
    ):
        raise RuntimeError(f'{_INDEX_NAME}: invalid or unexpected index; inspect before recovery')
    return True


def migrate():
    """Create/verify the index without changing business rows or dropping indexes."""
    if db.engine.dialect.name != 'postgresql':
        raise RuntimeError('This online migration requires PostgreSQL')
    with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        previous = {
            key: connection.execute(text(f'SHOW {key}')).scalar()
            for key in ('statement_timeout', 'lock_timeout')
        }
        try:
            connection.execute(text("SET statement_timeout='120s'"))
            connection.execute(text("SET lock_timeout='5s'"))
            if not _verify_index(connection):
                connection.execute(text(_CREATE_SQL))
                if not _verify_index(connection):
                    raise RuntimeError(f'{_INDEX_NAME}: index missing after creation')
        finally:
            try:
                for key, value in previous.items():
                    connection.execute(
                        text('SELECT set_config(:key, :value, false)'),
                        {'key': key, 'value': value},
                    )
            except Exception:
                connection.invalidate()
                raise
    return [_INDEX_NAME]
