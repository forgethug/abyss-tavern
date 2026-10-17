"""Atomic replay protection for region-2 Runtime relay envelopes."""

from __future__ import annotations

from sqlalchemy import delete, select

from extensions import db
from plugins.adventurer_s1.models import AdventurerS1RuntimeRelayNonce


_EXPIRED_CLEANUP_LIMIT = 1000


class S1RuntimeRelayNonceRepository:
    """Consume one nonce in a transaction isolated from gameplay writes."""

    @staticmethod
    def consume(
        *,
        nonce_digest: str,
        expires_at: int,
        now: int,
    ) -> bool:
        table = AdventurerS1RuntimeRelayNonce.__table__
        engine = db.engine
        dialect_name = engine.dialect.name
        values = {
            'nonce_digest': nonce_digest,
            'expires_at': expires_at,
        }

        if dialect_name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert

            statement = insert(table).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[table.c.nonce_digest]
            ).returning(table.c.nonce_digest)
        elif dialect_name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert

            statement = insert(table).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[table.c.nonce_digest]
            )
        else:
            raise RuntimeError('unsupported Runtime relay nonce database')

        with engine.begin() as connection:
            result = connection.execute(statement)
            inserted = (
                result.scalar_one_or_none() is not None
                if dialect_name == 'postgresql'
                else bool(result.rowcount and result.rowcount > 0)
            )
            if not inserted:
                return False

            # About one accepted request in 256 performs bounded indexed
            # cleanup. This keeps the hot path O(1) without another scheduler.
            if nonce_digest.startswith('00'):
                expired = (
                    select(table.c.nonce_digest)
                    .where(table.c.expires_at < now)
                    .order_by(
                        table.c.expires_at.asc(),
                        table.c.nonce_digest.asc(),
                    )
                    .limit(_EXPIRED_CLEANUP_LIMIT)
                )
                connection.execute(
                    delete(table).where(table.c.nonce_digest.in_(expired))
                )
            return True
