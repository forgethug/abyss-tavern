"""Keep witnessed public risk notices in the same version as S1 location state."""

from sqlalchemy import inspect, text

from extensions import db


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    additions = {
        'adventurer_s1_world_location_states': ('public_risk_notice',),
        'adventurer_s1_world_location_state_history': (
            'previous_public_risk_notice', 'new_public_risk_notice',
        ),
    }
    statements = []
    for table, fields in additions.items():
        if table not in tables:
            continue
        columns = {column['name'] for column in inspector.get_columns(table)}
        for field in fields:
            if field not in columns:
                statements.append(
                    f"ALTER TABLE {table} ADD COLUMN {field} TEXT NOT NULL DEFAULT ''"
                )
    with db.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
