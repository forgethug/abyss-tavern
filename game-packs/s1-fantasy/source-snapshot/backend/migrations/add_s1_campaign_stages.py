"""Opt-in campaign stages; existing campaigns keep their original behavior."""
from sqlalchemy import text
from extensions import db


def migrate(*, engine=None):
    from plugins.adventurer_s1.models import AdventurerS1CampaignStageState, AdventurerS1CampaignStageOperation
    target = engine or db.engine
    with target.begin() as connection:
        if target.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                               {'key': 'gupiao:s1_campaign_stages:v1'})
        for model in (AdventurerS1CampaignStageState, AdventurerS1CampaignStageOperation):
            model.__table__.create(connection, checkfirst=True)
            for index in model.__table__.indexes:
                index.create(connection, checkfirst=True)
