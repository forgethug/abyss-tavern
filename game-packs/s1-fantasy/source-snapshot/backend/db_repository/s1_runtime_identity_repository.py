"""Persistence primitives for Runtime-local central identity projections."""

from __future__ import annotations

from extensions import db
from models.user import User


class S1RuntimeIdentityRepository:
    @staticmethod
    def upsert(values: dict) -> None:
        dialect_name = db.session.get_bind().dialect.name
        if dialect_name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        elif dialect_name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert
        else:
            user = db.session.get(User, values['id'])
            if user is None:
                user = User(id=values['id'], username=values['username'])
                db.session.add(user)
            for key, value in values.items():
                if key not in {'id', 'created_at'}:
                    setattr(user, key, value)
            db.session.flush()
            return

        statement = insert(User).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[User.id],
            set_={
                key: value
                for key, value in values.items()
                if key not in {'id', 'created_at'}
            },
        )
        db.session.execute(statement)

    @staticmethod
    def get_by_id(user_id: int) -> User | None:
        return db.session.get(User, int(user_id))

    @staticmethod
    def get_by_username(username: str) -> User | None:
        return User.query.filter_by(username=username).first()
