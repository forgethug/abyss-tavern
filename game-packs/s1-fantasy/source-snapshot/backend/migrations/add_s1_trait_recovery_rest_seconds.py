"""Add cumulative dorm-rest seconds for S1 trait natural recovery."""
from extensions import db
from sqlalchemy import inspect, text


def _add_column_if_missing(conn, existing_columns: set[str], ddl: str, column_name: str) -> None:
    if column_name in existing_columns:
        return
    conn.execute(text(ddl))
    existing_columns.add(column_name)


with db.engine.connect() as conn:
    inspector = inspect(conn)
    existing = {col['name'] for col in inspector.get_columns('adventurer_s1')}
    _add_column_if_missing(
        conn,
        existing,
        "ALTER TABLE adventurer_s1 ADD COLUMN trait_recovery_rest_seconds INTEGER NOT NULL DEFAULT 0",
        'trait_recovery_rest_seconds',
    )
    conn.commit()

print('adventurer_s1.trait_recovery_rest_seconds migration completed')
