"""Durable, realm-local preparation leases for campaign phase handoffs."""
from sqlalchemy import text
from extensions import db


def migrate(*, engine=None):
    from plugins.adventurer_s1.models import AdventurerS1CampaignContinuation
    target = engine or db.engine
    with target.begin() as connection:
        if target.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                               {'key': 'gupiao:s1_campaign_continuations:v1'})
        AdventurerS1CampaignContinuation.__table__.create(connection, checkfirst=True)
        for index in AdventurerS1CampaignContinuation.__table__.indexes:
            index.create(connection, checkfirst=True)
