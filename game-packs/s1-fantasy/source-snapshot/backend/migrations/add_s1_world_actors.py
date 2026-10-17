"""Add bounded S1 world actors, descriptive faction relations and thread links."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    id_column = 'id SERIAL PRIMARY KEY' if db.engine.dialect.name == 'postgresql' else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    statements = []
    if 'adventurer_s1_world_actors' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_world_actors (
                {id_column}, canonical_name VARCHAR(120) NOT NULL UNIQUE, aliases JSON,
                actor_type VARCHAR(30) NOT NULL DEFAULT 'individual', status VARCHAR(20) NOT NULL DEFAULT 'active',
                identity_summary TEXT NOT NULL DEFAULT '', stable_principles TEXT, power_tier VARCHAR(30),
                power_boundary TEXT, capability_profile JSON,
                capability_revision INTEGER NOT NULL DEFAULT 0, capability_source_log_id INTEGER,
                long_term_goal TEXT, current_agenda TEXT, current_state TEXT,
                scope_keys JSON, trigger_keywords JSON, importance INTEGER NOT NULL DEFAULT 50,
                source_candidate_ids JSON,
                first_seen_log_id INTEGER, last_seen_log_id INTEGER,
                last_active_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX idx_advs1_world_actor_status_importance ON adventurer_s1_world_actors (status, importance, updated_at)",
            "CREATE INDEX ix_adventurer_s1_world_actors_capability_source_log_id ON adventurer_s1_world_actors (capability_source_log_id)",
        ])
    if 'adventurer_s1_world_actor_faction_relations' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_world_actor_faction_relations (
                {id_column}, actor_id INTEGER NOT NULL REFERENCES adventurer_s1_world_actors(id) ON DELETE CASCADE,
                faction_key VARCHAR(120) NOT NULL, faction_name VARCHAR(160) NOT NULL,
                public_stance TEXT, private_view TEXT, interests TEXT, concerns TEXT, dependencies TEXT,
                unresolved_matters TEXT, behavior_boundary TEXT, last_change_summary TEXT,
                source_log_id INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_actor_faction_relation UNIQUE (actor_id, faction_key)
            )""",
            "CREATE INDEX idx_advs1_actor_faction_updated ON adventurer_s1_world_actor_faction_relations (actor_id, updated_at)",
        ])
    if 'adventurer_s1_world_story_thread_actors' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_world_story_thread_actors (
                {id_column},
                world_story_thread_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL REFERENCES adventurer_s1_world_actors(id) ON DELETE CASCADE,
                involvement VARCHAR(20) NOT NULL DEFAULT 'direct', narrative_role VARCHAR(40),
                lease_required BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_world_thread_actor UNIQUE (world_story_thread_id, actor_id)
            )""",
            "CREATE INDEX idx_advs1_world_thread_actor_lease ON adventurer_s1_world_story_thread_actors (world_story_thread_id, lease_required)",
        ])
    if 'adventurer_s1_world_actor_interactions' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_world_actor_interactions (
                {id_column}, actor_id INTEGER NOT NULL REFERENCES adventurer_s1_world_actors(id) ON DELETE CASCADE,
                expedition_log_id INTEGER,
                world_story_thread_id INTEGER,
                interaction_summary TEXT NOT NULL DEFAULT '', actor_change_summary TEXT, relation_change_summary TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_actor_interaction_source UNIQUE (actor_id, expedition_log_id, world_story_thread_id)
            )""",
            "CREATE INDEX idx_advs1_actor_interaction_recent ON adventurer_s1_world_actor_interactions (actor_id, created_at)",
        ])
    lease_columns = {
        column['name'] for column in inspector.get_columns('adventurer_s1_narrative_leases')
    } if 'adventurer_s1_narrative_leases' in tables else set()
    if 'world_actor_id' not in lease_columns:
        statements.extend([
            "ALTER TABLE adventurer_s1_narrative_leases ADD COLUMN world_actor_id INTEGER REFERENCES adventurer_s1_world_actors(id) ON DELETE SET NULL",
            "CREATE INDEX ix_adventurer_s1_narrative_leases_world_actor_id ON adventurer_s1_narrative_leases (world_actor_id)",
        ])
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
