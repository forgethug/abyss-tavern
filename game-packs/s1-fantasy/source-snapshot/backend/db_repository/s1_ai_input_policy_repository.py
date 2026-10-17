"""Bounded independent scalar reads; release the connection before AI work."""
from sqlalchemy import select
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1DescriptionAiCache as Cache

def load_policy_rows(source_hashes):
    keys = set(source_hashes)
    if not keys:
        return []
    with db.engine.connect() as connection:
        connection = connection.execution_options(isolation_level='AUTOCOMMIT')
        return connection.execute(select(
            Cache.source_hash, Cache.ai_input_policy,
        ).where(Cache.source_hash.in_(keys))).all()
