"""Drop obsolete S1 item locked columns."""
from extensions import db
from sqlalchemy import inspect, text


def _drop_column_if_exists(conn, table_name: str, column_name: str) -> None:
    inspector = inspect(conn)
    if not inspector.has_table(table_name):
        return
    columns = {col['name'] for col in inspector.get_columns(table_name)}
    if column_name not in columns:
        return
    conn.execute(text(f'ALTER TABLE {table_name} DROP COLUMN {column_name}'))


with db.engine.connect() as conn:
    _drop_column_if_exists(conn, 'adventurer_s1_equipment', 'locked')
    _drop_column_if_exists(conn, 'adventurer_s1_warehouse_items', 'locked')
    conn.commit()

print('S1 item locked columns dropped')
