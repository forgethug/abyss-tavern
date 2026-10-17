"""Add structured natural recovery metadata to S1 adventurer traits."""
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
    dialect = db.engine.dialect.name
    if dialect == 'postgresql':
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_policy VARCHAR(20) NOT NULL DEFAULT 'none'",
            'recovery_policy',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_stage VARCHAR(20)",
            'recovery_stage',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_progress INTEGER NOT NULL DEFAULT 0",
            'recovery_progress',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_required INTEGER",
            'recovery_required',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovered_desc TEXT",
            'recovered_desc',
        )
    else:
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_policy VARCHAR(20) NOT NULL DEFAULT 'none'",
            'recovery_policy',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_stage VARCHAR(20)",
            'recovery_stage',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_progress INTEGER NOT NULL DEFAULT 0",
            'recovery_progress',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovery_required INTEGER",
            'recovery_required',
        )
        _add_column_if_missing(
            conn, existing,
            "ALTER TABLE adventurer_s1_traits ADD COLUMN recovered_desc TEXT",
            'recovered_desc',
        )
    conn.commit()

print("adventurer_s1_traits recovery metadata migration completed")
