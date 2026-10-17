"""Atomic persistence primitives for the S1 database deployment identity."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from extensions import db
from plugins.adventurer_s1.models import AdventurerS1DeploymentIdentity


class S1DeploymentIdentityRepository:
    """Expose only insert-once and locked-read operations for the singleton."""

    @staticmethod
    def insert_if_absent(
        *,
        realm_code: str,
        deployment_id: str,
        schema_revision: int,
        now: datetime,
    ) -> bool:
        values = {
            'id': 1,
            'realm_code': realm_code,
            'deployment_id': deployment_id,
            'schema_revision': schema_revision,
            'created_at': now,
            'updated_at': now,
        }
        dialect_name = db.session.get_bind().dialect.name
        if dialect_name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert

            statement = insert(AdventurerS1DeploymentIdentity).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=['id'])
            statement = statement.returning(AdventurerS1DeploymentIdentity.id)
            inserted_id = db.session.execute(statement).scalar_one_or_none()
            return inserted_id == 1

        if dialect_name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert

            statement = insert(AdventurerS1DeploymentIdentity).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=['id'])
            result = db.session.execute(statement)
            return bool(result.rowcount and result.rowcount > 0)

        # The application runs on PostgreSQL and tests on SQLite. Keep a safe
        # savepoint fallback for other SQLAlchemy dialects without poisoning the
        # caller-owned transaction when another process wins the singleton row.
        try:
            with db.session.begin_nested():
                row = AdventurerS1DeploymentIdentity(**values)
                db.session.add(row)
                db.session.flush()
            return True
        except IntegrityError:
            return False

    @staticmethod
    def lock_all_bounded() -> list[AdventurerS1DeploymentIdentity]:
        """Lock at most two rows so malformed legacy tables fail closed."""
        return (
            db.session.query(AdventurerS1DeploymentIdentity)
            .order_by(AdventurerS1DeploymentIdentity.id.asc())
            .limit(2)
            .populate_existing()
            .with_for_update()
            .all()
        )
