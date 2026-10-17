"""PostgreSQL session advisory locks isolated from the application QueuePool."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, URL
from sqlalchemy.pool import NullPool


@dataclass(slots=True)
class PostgreSQLAdvisoryLockHandle:
    """An acquired session lock bound to one dedicated physical connection."""

    connection: Connection
    _engine: Engine
    _key: int | str
    _closed: bool = False

    def release(self) -> bool:
        """Unlock on the owning connection, then permanently close that session."""
        if self._closed or self.connection.closed:
            self.discard()
            return False
        statement = (
            text('SELECT pg_advisory_unlock(:key)')
            if type(self._key) is int
            else text('SELECT pg_advisory_unlock(hashtext(:key))')
        )
        try:
            return bool(self.connection.execute(
                statement,
                {'key': self._key},
            ).scalar())
        finally:
            self.discard()

    def discard(self) -> None:
        """Close the owning PostgreSQL session without issuing an unlock query."""
        if self._closed:
            return
        self._closed = True
        try:
            self.connection.close()
        finally:
            self._engine.dispose()


def acquire_postgresql_advisory_lock(
    database_url: str | URL,
    key: int | str,
    *,
    engine_options: Mapping[str, Any] | None = None,
) -> PostgreSQLAdvisoryLockHandle | None:
    """Try to acquire a session advisory lock outside the application pool."""
    if type(key) is not int and (not isinstance(key, str) or not key):
        raise ValueError('PostgreSQL advisory lock key must be an int or non-empty str')

    connect_args: dict[str, Any] = {}
    if isinstance(engine_options, Mapping):
        configured_connect_args = engine_options.get('connect_args')
        if isinstance(configured_connect_args, Mapping):
            connect_args = dict(configured_connect_args)

    engine = create_engine(
        database_url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    connection = None
    try:
        if engine.dialect.name != 'postgresql':
            raise ValueError('PostgreSQL advisory lock requires a PostgreSQL database')
        connection = engine.connect().execution_options(
            isolation_level='AUTOCOMMIT',
        )
        statement = (
            text('SELECT pg_try_advisory_lock(:key)')
            if type(key) is int
            else text('SELECT pg_try_advisory_lock(hashtext(:key))')
        )
        acquired = bool(connection.execute(
            statement,
            {'key': key},
        ).scalar())
        if not acquired:
            connection.close()
            engine.dispose()
            return None
        return PostgreSQLAdvisoryLockHandle(
            connection=connection,
            _engine=engine,
            _key=key,
        )
    except Exception:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        engine.dispose()
        raise
