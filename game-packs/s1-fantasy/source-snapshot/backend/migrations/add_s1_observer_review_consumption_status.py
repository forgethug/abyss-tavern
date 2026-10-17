"""Separate S1 Observer model-contract completion from final consumption."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_expedition_observer_reviews'
_COLUMN_DDLS = {
    'consumption_status': 'VARCHAR(20)',
    'consumption_failure_stage': 'VARCHAR(80)',
    'consumption_error_code': 'VARCHAR(160)',
}


def migrate() -> None:
    inspector = inspect(db.engine)
    if _TABLE_NAME not in inspector.get_table_names():
        return

    with db.engine.begin() as conn:
        existing = {
            column['name']
            for column in inspector.get_columns(_TABLE_NAME)
        }
        for column_name, column_ddl in _COLUMN_DDLS.items():
            if column_name in existing:
                continue
            conn.execute(text(
                f'ALTER TABLE {_TABLE_NAME} '
                f'ADD COLUMN {column_name} {column_ddl}'
            ))
            existing.add(column_name)


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
