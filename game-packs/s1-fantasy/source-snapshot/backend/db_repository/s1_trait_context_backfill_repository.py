"""Short scalar reads for background trait routing coverage scans."""

from sqlalchemy import select

from extensions import db


def read_backfill_rows(statement):
    """Materialize one bounded query before hashing or projecting its results."""
    with db.engine.connect() as connection:
        connection = connection.execution_options(isolation_level='AUTOCOMMIT')
        return connection.execute(statement).all()


def read_backfill_cache_rows(source_hashes: set[str]) -> dict:
    if not source_hashes:
        return {}
    from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache as Cache

    rows = read_backfill_rows(select(
        Cache.source_hash,
        Cache.status,
        Cache.trait_context_rule,
        Cache.trait_context_audit_status,
        Cache.trait_context_audit_receipt,
    ).where(Cache.source_hash.in_(source_hashes)))
    return {row.source_hash: row for row in rows}
