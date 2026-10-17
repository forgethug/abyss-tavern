"""Add description_ai_source_hash to adventurer_s1_traits."""
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
        conn, existing,
        'ALTER TABLE adventurer_s1_traits ADD COLUMN description_ai_source_hash VARCHAR(64)',
        'description_ai_source_hash',
    )
    conn.commit()

print('adventurer_s1_traits.description_ai_source_hash migration completed')
