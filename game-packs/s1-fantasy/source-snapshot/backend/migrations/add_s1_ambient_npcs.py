"""Add S1 ambient NPC hidden state and idempotent interaction receipts."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    id_column = (
        'id SERIAL PRIMARY KEY'
        if db.engine.dialect.name == 'postgresql'
        else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    )
    statements = []
    if 'adventurer_s1_ambient_npc_states' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_ambient_npc_states (
                {id_column},
                adventurer_id INTEGER NOT NULL,
                npc_key VARCHAR(64) NOT NULL,
                affinity_score INTEGER NOT NULL DEFAULT 0,
                interaction_count INTEGER NOT NULL DEFAULT 0,
                recent_text_keys JSON NOT NULL DEFAULT '{{}}',
                last_interaction_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_ambient_npc_state
                    UNIQUE (adventurer_id, npc_key),
                CONSTRAINT ck_advs1_ambient_npc_affinity_range
                    CHECK (affinity_score BETWEEN -100 AND 100)
            )""",
            "CREATE INDEX ix_adventurer_s1_ambient_npc_states_adventurer_id "
            "ON adventurer_s1_ambient_npc_states (adventurer_id)",
        ])
    if 'adventurer_s1_ambient_npc_interactions' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_ambient_npc_interactions (
                {id_column},
                adventurer_id INTEGER NOT NULL,
                npc_key VARCHAR(64) NOT NULL,
                encounter_id VARCHAR(64) NOT NULL,
                surface_key VARCHAR(32) NOT NULL,
                action_key VARCHAR(64) NOT NULL,
                request_id VARCHAR(64) NOT NULL,
                request_fingerprint VARCHAR(64) NOT NULL,
                template_revision INTEGER NOT NULL,
                response_key VARCHAR(96) NOT NULL,
                response_text TEXT NOT NULL,
                item_source VARCHAR(20),
                item_id INTEGER,
                item_name VARCHAR(100),
                affinity_delta INTEGER NOT NULL DEFAULT 0,
                affinity_before INTEGER NOT NULL,
                affinity_after INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_ambient_npc_request
                    UNIQUE (adventurer_id, request_id),
                CONSTRAINT uq_advs1_ambient_npc_action
                    UNIQUE (adventurer_id, encounter_id, action_key)
            )""",
            "CREATE INDEX ix_adventurer_s1_ambient_npc_interactions_adventurer_id "
            "ON adventurer_s1_ambient_npc_interactions (adventurer_id)",
            "CREATE INDEX ix_advs1_ambient_npc_encounter "
            "ON adventurer_s1_ambient_npc_interactions (adventurer_id, encounter_id)",
        ])
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
