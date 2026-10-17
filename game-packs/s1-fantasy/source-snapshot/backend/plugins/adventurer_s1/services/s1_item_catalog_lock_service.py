"""Transaction-scoped locks for S1 catalog names and snapshot materialization."""

from __future__ import annotations

import hashlib
import unicodedata

from sqlalchemy import text

from db_repository import ConcurrencyError, db_manager
from extensions import db


_CATALOG_NAME_LOCK_NAMESPACE = 0x5331434E  # "S1CN"

CATALOG_BUSY_ERROR = '物品图鉴正在更新，请稍后重试'


class CatalogBusyError(ConcurrencyError):
    """The whole caller-owned transaction must be retried, never partially settled."""

    def __init__(self):
        super().__init__(CATALOG_BUSY_ERROR)


def _catalog_name_lock_key(name: str) -> int:
    normalized = unicodedata.normalize('NFKC', str(name or '').strip()).casefold()
    digest = hashlib.blake2s(
        normalized.encode('utf-8'),
        digest_size=4,
        person=b'S1CAT',
    ).digest()
    return int.from_bytes(digest, byteorder='big', signed=True)


def lock_catalog_names(
    names,
    *,
    shared: bool,
    wait: bool,
) -> bool:
    """Acquire catalog-name locks in deterministic order for the current transaction.

    Callers using ``wait=False`` must abort their operation when False is returned. Some
    earlier keys may already be held until that transaction ends.
    """
    lock_keys = sorted({
        _catalog_name_lock_key(name)
        for name in (names or [])
        if str(name or '').strip()
    })
    if not lock_keys or db.session.get_bind().dialect.name != 'postgresql':
        return True

    if shared and wait:
        function_name = 'pg_advisory_xact_lock_shared'
    elif shared:
        function_name = 'pg_try_advisory_xact_lock_shared'
    elif wait:
        function_name = 'pg_advisory_xact_lock'
    else:
        function_name = 'pg_try_advisory_xact_lock'

    statement = text(f'''
        SELECT lock_key, {function_name}(:namespace, lock_key) AS acquired
        FROM (
            SELECT DISTINCT unnest(CAST(:lock_keys AS INTEGER[])) AS lock_key
        ) AS ordered_locks
        ORDER BY lock_key
    ''')
    result, _ = db_manager.execute_statement(
        statement,
        {
            'namespace': _CATALOG_NAME_LOCK_NAMESPACE,
            'lock_keys': lock_keys,
        },
        raise_errors=True,
    )
    rows = result.fetchall()
    if wait:
        return True
    return all(bool(row.acquired) for row in rows)
