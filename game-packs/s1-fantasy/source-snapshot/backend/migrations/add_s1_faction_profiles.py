"""Add versioned S1 faction dossiers and explicit world-thread links."""

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
    statements: list[str] = []
    if 'adventurer_s1_faction_profiles' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_faction_profiles (
                {id_column},
                faction_key VARCHAR(120) NOT NULL,
                canonical_name VARCHAR(160) NOT NULL,
                aliases JSON,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                identity_summary TEXT NOT NULL DEFAULT '',
                scope_keys JSON,
                trigger_keywords JSON,
                importance INTEGER NOT NULL DEFAULT 50,
                facets JSON,
                revision INTEGER NOT NULL DEFAULT 1,
                source_candidate_ids JSON,
                first_seen_log_id INTEGER,
                last_seen_log_id INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            (
                'CREATE INDEX idx_advs1_faction_profile_status_importance '
                'ON adventurer_s1_faction_profiles (status, importance, updated_at)'
            ),
            (
                'CREATE UNIQUE INDEX ix_adventurer_s1_faction_profiles_faction_key '
                'ON adventurer_s1_faction_profiles (faction_key)'
            ),
            (
                'CREATE UNIQUE INDEX ix_adventurer_s1_faction_profiles_canonical_name '
                'ON adventurer_s1_faction_profiles (canonical_name)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profiles_status '
                'ON adventurer_s1_faction_profiles (status)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profiles_importance '
                'ON adventurer_s1_faction_profiles (importance)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profiles_first_seen_log_id '
                'ON adventurer_s1_faction_profiles (first_seen_log_id)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profiles_last_seen_log_id '
                'ON adventurer_s1_faction_profiles (last_seen_log_id)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profiles_updated_at '
                'ON adventurer_s1_faction_profiles (updated_at)'
            ),
        ])
    if 'adventurer_s1_faction_profile_threads' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_faction_profile_threads (
                {id_column},
                faction_profile_id INTEGER NOT NULL
                    REFERENCES adventurer_s1_faction_profiles(id) ON DELETE CASCADE,
                world_story_thread_id INTEGER NOT NULL,
                relation_role VARCHAR(20) NOT NULL DEFAULT 'involved',
                source_log_id INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_faction_profile_thread
                    UNIQUE (faction_profile_id, world_story_thread_id)
            )""",
            (
                'CREATE INDEX idx_advs1_faction_thread_lookup '
                'ON adventurer_s1_faction_profile_threads '
                '(world_story_thread_id, relation_role)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profile_threads_source_log_id '
                'ON adventurer_s1_faction_profile_threads (source_log_id)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profile_threads_faction_profile_id '
                'ON adventurer_s1_faction_profile_threads (faction_profile_id)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profile_threads_world_story_thread_id '
                'ON adventurer_s1_faction_profile_threads (world_story_thread_id)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profile_threads_relation_role '
                'ON adventurer_s1_faction_profile_threads (relation_role)'
            ),
        ])
    if 'adventurer_s1_faction_profile_history' not in tables:
        statements.extend([
            f"""CREATE TABLE adventurer_s1_faction_profile_history (
                {id_column},
                faction_profile_id INTEGER NOT NULL
                    REFERENCES adventurer_s1_faction_profiles(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL,
                snapshot JSON NOT NULL,
                change_summary TEXT NOT NULL DEFAULT '',
                source_candidate_ids JSON,
                source_log_id INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_advs1_faction_profile_history_revision
                    UNIQUE (faction_profile_id, revision)
            )""",
            (
                'CREATE INDEX ix_adventurer_s1_faction_profile_history_source_log_id '
                'ON adventurer_s1_faction_profile_history (source_log_id)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profile_history_faction_profile_id '
                'ON adventurer_s1_faction_profile_history (faction_profile_id)'
            ),
            (
                'CREATE INDEX ix_adventurer_s1_faction_profile_history_created_at '
                'ON adventurer_s1_faction_profile_history (created_at)'
            ),
        ])
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
