from typing import Any, Optional, Tuple, Type

from sqlalchemy.exc import SQLAlchemyError

from extensions import db


class DatabaseManager:
    def query(self, model: Type[Any]):
        return db.session.query(model)

    def add(self, record: Any) -> Tuple[bool, Optional[str]]:
        try:
            db.session.add(record)
            return True, None
        except SQLAlchemyError as exc:
            db.session.rollback()
            return False, str(exc)

    def delete(self, record: Any) -> Tuple[bool, Optional[str]]:
        try:
            db.session.delete(record)
            return True, None
        except SQLAlchemyError as exc:
            db.session.rollback()
            return False, str(exc)

    def flush(self) -> Tuple[bool, Optional[str]]:
        try:
            db.session.flush()
            return True, None
        except SQLAlchemyError as exc:
            db.session.rollback()
            return False, str(exc)

    def commit(self) -> Tuple[bool, Optional[str]]:
        try:
            db.session.commit()
            return True, None
        except SQLAlchemyError as exc:
            db.session.rollback()
            return False, str(exc)

    def rollback(self) -> None:
        db.session.rollback()


db_manager = DatabaseManager()

