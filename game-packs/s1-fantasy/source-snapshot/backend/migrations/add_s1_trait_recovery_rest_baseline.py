"""Add per-trait dorm-rest baseline to ignore pre-activation banked rest."""
from extensions import db
from sqlalchemy import inspect, text


def _add_column_if_missing(conn, existing_columns: set[str], ddl: str, column_name: str) -> None:
    if column_name in existing_columns:
        return
    conn.execute(text(ddl))
    existing_columns.add(column_name)


with db.engine.connect() as conn:
    inspector = inspect(conn)
    existing = {
        col['name']
        for col in inspector.get_columns('adventurer_s1_traits')
    }
    _add_column_if_missing(
        conn,
        existing,
        'ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_rest_baseline_seconds INTEGER',
        'recovery_rest_baseline_seconds',
    )
    conn.commit()

print('adventurer_s1_traits.recovery_rest_baseline_seconds migration completed')
