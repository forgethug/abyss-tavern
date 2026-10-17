from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable, Optional, Tuple, Type

from sqlalchemy import text

from db_repository.exceptions import (
    ConcurrencyError,
    DatabaseError,
    TransactionError,
)
from extensions import db


_TRANSACTION_DEPTH = ContextVar("runtime_database_transaction_depth", default=0)


class DatabaseManager:
    def query(self, *entities):
        return db.session.query(*entities)

    def get(self, model: Type[Any], record_id: Any):
        return db.session.get(model, record_id)

    def add(self, instance: Any, *, flush: bool = True) -> Tuple[bool, Optional[str]]:
        try:
            db.session.add(instance)
            if flush:
                db.session.flush()
            return True, None
        except Exception as exc:
            return False, str(exc)

    def add_all(
        self,
        instances: Iterable[Any],
        *,
        flush: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        try:
            db.session.add_all(list(instances))
            if flush:
                db.session.flush()
            return True, None
        except Exception as exc:
            return False, str(exc)

    def delete_instance(
        self,
        instance: Any,
        *,
        flush: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        try:
            db.session.delete(instance)
            if flush:
                db.session.flush()
            return True, None
        except Exception as exc:
            return False, str(exc)

    def flush(self) -> Tuple[bool, Optional[str]]:
        try:
            db.session.flush()
            return True, None
        except Exception as exc:
            return False, str(exc)

    def refresh(self, instance: Any) -> Tuple[bool, Optional[str]]:
        try:
            db.session.refresh(instance)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def commit(self) -> Tuple[bool, Optional[str]]:
        try:
            db.session.commit()
            return True, None
        except Exception as exc:
            db.session.rollback()
            return False, str(exc)

    def rollback(self) -> None:
        db.session.rollback()

    def begin_nested(self):
        return db.session.begin_nested()

    def remove(self) -> None:
        db.session.remove()

    @contextmanager
    def readonly_snapshot(self):
        """Give pack reads a separate connection without touching caller state."""
        connection = db.engine.connect()
        sqlite_query_only = None
        try:
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            elif connection.dialect.name == "sqlite":
                sqlite_query_only = connection.exec_driver_sql("PRAGMA query_only").scalar()
                connection.exec_driver_sql("PRAGMA query_only = ON")
                connection.rollback()
            with connection.begin():
                if connection.dialect.name == "postgresql":
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                yield connection
        finally:
            connection.rollback()
            if sqlite_query_only is not None:
                connection.exec_driver_sql(
                    "PRAGMA query_only = ON" if sqlite_query_only else "PRAGMA query_only = OFF"
                )
                connection.rollback()
            connection.info.pop("admin_database_read_table_names", None)
            connection.close()

    def execute_statement(self, statement, params=None, *, connection=None, raise_errors=False):
        try:
            executor = connection if connection is not None else db.session
            return executor.execute(statement, params or {}), None
        except Exception as exc:
            if raise_errors:
                raise
            return None, str(exc)

    @contextmanager
    def transaction(self):
        token = _TRANSACTION_DEPTH.set(_TRANSACTION_DEPTH.get() + 1)
        try:
            yield db.session
            try:
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                raise TransactionError(
                    f"Transaction commit failed: {exc}",
                    original_error=exc,
                )
        except (TransactionError, ConcurrencyError):
            raise
        except Exception as exc:
            db.session.rollback()
            raise DatabaseError(f"Transaction failed: {exc}", original_error=exc)
        finally:
            _TRANSACTION_DEPTH.reset(token)

    @staticmethod
    def transaction_depth():
        return _TRANSACTION_DEPTH.get()

    def execute_raw(self, sql: str, params: Optional[dict] = None):
        try:
            return db.session.execute(text(sql), params or {}), None
        except Exception as exc:
            return None, str(exc)

    def bulk_insert_mappings(self, model: Type[Any], mappings: list) -> Tuple[bool, Optional[str]]:
        try:
            db.session.bulk_insert_mappings(model, mappings)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def bulk_update_mappings(self, model: Type[Any], mappings: list) -> Tuple[bool, Optional[str]]:
        try:
            db.session.bulk_update_mappings(model, mappings)
            return True, None
        except Exception as exc:
            return False, str(exc)


db_manager = DatabaseManager()
