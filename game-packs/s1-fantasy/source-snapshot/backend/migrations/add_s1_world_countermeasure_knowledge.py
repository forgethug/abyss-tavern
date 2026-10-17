"""Add durable, audience-scoped S1 world countermeasure knowledge."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if (
        'adventurer_s1_world_actors' not in tables
        or 'adventurer_s1_world_countermeasure_knowledge' in tables
    ):
        return
    id_column = (
        'id SERIAL PRIMARY KEY'
        if db.engine.dialect.name == 'postgresql'
        else 'id INTEGER PRIMARY KEY AUTOINCREMENT'
    )
    statements = [
        f"""CREATE TABLE adventurer_s1_world_countermeasure_knowledge (
            {id_column},
            first_source_expedition_log_id INTEGER NOT NULL,
            last_source_expedition_log_id INTEGER NOT NULL,
            evidence_source_expedition_log_id INTEGER,
            observer_world_actor_id INTEGER NOT NULL
                REFERENCES adventurer_s1_world_actors(id) ON DELETE CASCADE,
            observer_capability_revision INTEGER NOT NULL,
            audience_key VARCHAR(140) NOT NULL,
            audience_type VARCHAR(20) NOT NULL,
            faction_key VARCHAR(120),
            learning_fingerprint VARCHAR(64) NOT NULL,
            source_refs JSON NOT NULL,
            encountered_method TEXT NOT NULL,
            unresolved_reason TEXT NOT NULL,
            knowledge_level VARCHAR(20) NOT NULL,
            evidence_event_indexes JSON NOT NULL,
            evidence_event_quotes JSON,
            observed_effect TEXT NOT NULL,
            learned_boundary TEXT NOT NULL,
            countermeasure_direction TEXT,
            encounter_count INTEGER NOT NULL DEFAULT 1,
            last_opportunity_expedition_log_id INTEGER,
            last_opportunity_source_expedition_log_id INTEGER,
            last_opportunity_at TIMESTAMP,
            opportunity_count INTEGER NOT NULL DEFAULT 0,
            last_application_expedition_log_id INTEGER,
            last_applied_source_expedition_log_id INTEGER,
            application_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_advs1_countermeasure_audience_method UNIQUE (
                audience_key, learning_fingerprint
            )
        )""",
        (
            'CREATE INDEX idx_advs1_countermeasure_audience_recent '
            'ON adventurer_s1_world_countermeasure_knowledge (audience_key, updated_at)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_first_source_log '
            'ON adventurer_s1_world_countermeasure_knowledge '
            '(first_source_expedition_log_id)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_last_source_log '
            'ON adventurer_s1_world_countermeasure_knowledge '
            '(last_source_expedition_log_id)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_observer '
            'ON adventurer_s1_world_countermeasure_knowledge (observer_world_actor_id)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_audience_type '
            'ON adventurer_s1_world_countermeasure_knowledge (audience_type)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_faction '
            'ON adventurer_s1_world_countermeasure_knowledge (faction_key)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_level '
            'ON adventurer_s1_world_countermeasure_knowledge (knowledge_level)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_count '
            'ON adventurer_s1_world_countermeasure_knowledge (encounter_count)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_last_opportunity_log '
            'ON adventurer_s1_world_countermeasure_knowledge '
            '(last_opportunity_expedition_log_id)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_last_application_log '
            'ON adventurer_s1_world_countermeasure_knowledge '
            '(last_application_expedition_log_id)'
        ),
        (
            'CREATE INDEX idx_advs1_countermeasure_opportunity_due '
            'ON adventurer_s1_world_countermeasure_knowledge '
            '(audience_type, last_opportunity_at, observer_world_actor_id)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_created '
            'ON adventurer_s1_world_countermeasure_knowledge (created_at)'
        ),
        (
            'CREATE INDEX ix_advs1_countermeasure_updated '
            'ON adventurer_s1_world_countermeasure_knowledge (updated_at)'
        ),
    ]
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
