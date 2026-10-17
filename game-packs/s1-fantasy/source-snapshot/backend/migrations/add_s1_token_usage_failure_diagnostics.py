"""Add structured AI failure diagnostics to S1 token usage records."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_token_usages'
_COLUMN_DDLS = {
    'failure_stage': 'VARCHAR(80)',
    'validation_error_code': 'VARCHAR(160)',
    'finish_reason': 'VARCHAR(40)',
    'json_object_mode': 'BOOLEAN',
    'provider_request_id': 'VARCHAR(160)',
    'repair_applied': 'BOOLEAN NOT NULL DEFAULT FALSE',
    'failed_response_excerpt': 'TEXT',
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
