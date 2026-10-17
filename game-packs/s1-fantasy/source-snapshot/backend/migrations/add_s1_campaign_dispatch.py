"""Add the campaign hall's current invitation, published reports and squad binding."""
from sqlalchemy import inspect, text

from extensions import db


def migrate(*, engine=None):
    from plugins.adventurer_s1.models import (
        AdventurerS1CampaignDispatch, AdventurerS1CampaignDispatchReport,
        AdventurerS1CampaignDispatchClaim,
        AdventurerS1MatchTicket,
    )
    target = engine or db.engine
    table_name = AdventurerS1MatchTicket.__tablename__
    columns = {
        'campaign_dispatch_id': 'INTEGER',
        'campaign_dispatch_revision': 'INTEGER',
        'campaign_task_title': 'VARCHAR(120)',
        'campaign_task_cooldown_until': 'TIMESTAMP',
    }
    with target.begin() as connection:
        if target.dialect.name == 'postgresql':
            connection.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),
                               {'key': 'gupiao:s1_campaign_dispatch:v1'})
        for model in (AdventurerS1CampaignDispatch, AdventurerS1CampaignDispatchReport,
                      AdventurerS1CampaignDispatchClaim):
            model.__table__.create(connection, checkfirst=True)
        dispatch_table = AdventurerS1CampaignDispatch.__tablename__
        dispatch_columns = {c['name'] for c in inspect(connection).get_columns(dispatch_table)}
        if 'max_active_teams' not in dispatch_columns:
            connection.execute(text(
                f'ALTER TABLE {dispatch_table} ADD COLUMN max_active_teams INTEGER NOT NULL DEFAULT 1'))
        if not inspect(connection).has_table(table_name):
            return
        existing = {c['name'] for c in inspect(connection).get_columns(table_name)}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {name} {definition}'))
        # Names and SQL identifiers are module-owned constants.
        connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_match_tickets_campaign_dispatch_id '
            f'ON {table_name} (campaign_dispatch_id)'
        ))
        connection.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_match_tickets_campaign_task_cooldown_until '
            f'ON {table_name} (campaign_task_cooldown_until)'
        ))
