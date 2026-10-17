"""Add S1 match ticket rejoin cooldowns and visible match errors."""
from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if 'adventurer_s1_match_tickets' not in tables:
        return

    with db.engine.begin() as conn:
        existing = {
            col['name']
            for col in inspector.get_columns('adventurer_s1_match_tickets')
        }
        if 'match_error' not in existing:
            conn.execute(text(
                "ALTER TABLE adventurer_s1_match_tickets "
                "ADD COLUMN match_error VARCHAR(300) NOT NULL DEFAULT ''"
            ))
        if 'queued_at' not in existing:
            conn.execute(text(
                "ALTER TABLE adventurer_s1_match_tickets "
                "ADD COLUMN queued_at TIMESTAMP"
            ))
            conn.execute(text(
                "UPDATE adventurer_s1_match_tickets "
                "SET queued_at = COALESCE(updated_at, created_at) "
                "WHERE status = 'queued' AND queued_at IS NULL"
            ))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_match_tickets_queued_at '
            'ON adventurer_s1_match_tickets (queued_at)'
        ))

        conn.execute(text(
            'CREATE TABLE IF NOT EXISTS adventurer_s1_match_ticket_rejoin_cooldowns ('
            'id SERIAL PRIMARY KEY, '
            'adventurer_id INTEGER NOT NULL REFERENCES adventurer_s1(id) ON DELETE CASCADE, '
            'ticket_id INTEGER NOT NULL REFERENCES adventurer_s1_match_tickets(id) ON DELETE CASCADE, '
            'blocked_until TIMESTAMP NOT NULL, '
            'created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, '
            'updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, '
            'CONSTRAINT uq_advs1_match_ticket_rejoin_cooldown UNIQUE (adventurer_id, ticket_id)'
            ')'
        ))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_advs1_match_ticket_rejoin_cooldowns_adventurer_id '
            'ON adventurer_s1_match_ticket_rejoin_cooldowns (adventurer_id)'
        ))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_advs1_match_ticket_rejoin_cooldowns_ticket_id '
            'ON adventurer_s1_match_ticket_rejoin_cooldowns (ticket_id)'
        ))


if __name__ == '__main__':
    from app import create_app
    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
