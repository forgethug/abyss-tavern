"""Add original dorm bed columns for S1 archived adventurers."""
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
        'ALTER TABLE adventurer_s1 ADD COLUMN archived_from_dorm_room_id INTEGER',
        'archived_from_dorm_room_id',
    )
    _add_column_if_missing(
        conn,
        existing,
        'ALTER TABLE adventurer_s1 ADD COLUMN archived_from_bed_index INTEGER',
        'archived_from_bed_index',
    )
    conn.commit()

print('adventurer_s1 archived original bed migration completed')
