"""Add player_review_status to adventurer_s1_data_appeals for async MIMO review."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    columns = {col['name'] for col in inspector.get_columns('adventurer_s1_data_appeals')}
    if 'player_review_status' in columns:
        return
    with db.engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE adventurer_s1_data_appeals "
            "ADD COLUMN player_review_status VARCHAR(20) NOT NULL DEFAULT 'none'"
        ))


if __name__ == '__main__':
    migrate()
