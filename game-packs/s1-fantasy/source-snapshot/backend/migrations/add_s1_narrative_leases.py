"""Add S1 narrative lease table for public plot injection locking."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    if 'adventurer_s1_narrative_leases' in inspector.get_table_names():
        return

    id_column = 'id SERIAL PRIMARY KEY'
    if db.engine.dialect.name != 'postgresql':
        id_column = 'id INTEGER PRIMARY KEY AUTOINCREMENT'

    statements = [
        f"""
        CREATE TABLE adventurer_s1_narrative_leases (
            {id_column},
            target_dungeon VARCHAR(100) NOT NULL,
            plot_signature VARCHAR(180) NOT NULL,
            lease_type VARCHAR(30) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            party_id INTEGER REFERENCES adventurer_s1_parties(id) ON DELETE SET NULL,
            expedition_log_id INTEGER REFERENCES adventurer_s1_expedition_logs(id) ON DELETE SET NULL,
            world_story_thread_id INTEGER REFERENCES adventurer_s1_world_story_threads(id) ON DELETE SET NULL,
            dm_handoff_state_id INTEGER REFERENCES adventurer_s1_dm_handoff_states(id) ON DELETE SET NULL,
            source_log_id INTEGER REFERENCES adventurer_s1_expedition_logs(id) ON DELETE SET NULL,
            required_adventurer_ids JSON,
            required_item_names JSON,
            title VARCHAR(160),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            released_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX ix_adventurer_s1_narrative_leases_target_dungeon ON adventurer_s1_narrative_leases (target_dungeon)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_plot_signature ON adventurer_s1_narrative_leases (plot_signature)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_lease_type ON adventurer_s1_narrative_leases (lease_type)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_status ON adventurer_s1_narrative_leases (status)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_party_id ON adventurer_s1_narrative_leases (party_id)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_expedition_log_id ON adventurer_s1_narrative_leases (expedition_log_id)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_world_story_thread_id ON adventurer_s1_narrative_leases (world_story_thread_id)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_dm_handoff_state_id ON adventurer_s1_narrative_leases (dm_handoff_state_id)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_source_log_id ON adventurer_s1_narrative_leases (source_log_id)",
        "CREATE INDEX ix_adventurer_s1_narrative_leases_expires_at ON adventurer_s1_narrative_leases (expires_at)",
        "CREATE INDEX idx_advs1_narrative_lease_active_sig ON adventurer_s1_narrative_leases (target_dungeon, plot_signature, status)",
        "CREATE INDEX idx_advs1_narrative_lease_log_status ON adventurer_s1_narrative_leases (expedition_log_id, status)",
    ]
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
