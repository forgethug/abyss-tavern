"""Add per-generation MIMO observer reviews for S1 expedition logs."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    if 'adventurer_s1_expedition_observer_reviews' in inspector.get_table_names():
        return
    statements = [
        """
        CREATE TABLE adventurer_s1_expedition_observer_reviews (
            id SERIAL PRIMARY KEY,
            expedition_log_id INTEGER NOT NULL REFERENCES adventurer_s1_expedition_logs(id) ON DELETE CASCADE,
            model_used VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            result JSON,
            raw_response TEXT,
            error_message TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX ix_adventurer_s1_expedition_observer_reviews_expedition_log_id "
        "ON adventurer_s1_expedition_observer_reviews (expedition_log_id)",
        "CREATE INDEX ix_adventurer_s1_expedition_observer_reviews_status "
        "ON adventurer_s1_expedition_observer_reviews (status)",
        "CREATE INDEX ix_adventurer_s1_expedition_observer_reviews_created_at "
        "ON adventurer_s1_expedition_observer_reviews (created_at)",
        "CREATE INDEX ix_advs1_observer_review_log_created "
        "ON adventurer_s1_expedition_observer_reviews (expedition_log_id, created_at)",
    ]
    if db.engine.dialect.name != 'postgresql':
        statements[0] = statements[0].replace('id SERIAL PRIMARY KEY', 'id INTEGER PRIMARY KEY AUTOINCREMENT')
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


if __name__ == '__main__':
    migrate()
