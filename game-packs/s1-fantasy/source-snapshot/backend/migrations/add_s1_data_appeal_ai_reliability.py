"""Add S1 data-appeal AI lease and non-sensitive route diagnostics."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_COLUMNS = {
    'adventurer_s1_data_appeals': {
        'ai_claim_token': 'VARCHAR(64)',
        'ai_claimed_at': 'TIMESTAMP',
        'ai_heartbeat_at': 'TIMESTAMP',
        'ai_owner_boot_id': 'VARCHAR(128)',
    },
    'adventurer_s1_token_usages': {
        'profile_id': 'VARCHAR(100)',
        'provider': 'VARCHAR(80)',
    },
}


def migrate() -> None:
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    with db.engine.begin() as conn:
        for table_name, columns in _TABLE_COLUMNS.items():
            if table_name not in table_names:
                continue
            existing = {
                column['name']
                for column in inspector.get_columns(table_name)
            }
            for column_name, column_ddl in columns.items():
                if column_name in existing:
                    continue
                conn.execute(text(
                    f'ALTER TABLE {table_name} '
                    f'ADD COLUMN {column_name} {column_ddl}'
                ))
                existing.add(column_name)

        if 'adventurer_s1_data_appeals' in table_names:
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_advs1_data_appeal_ai_heartbeat '
                'ON adventurer_s1_data_appeals '
                '(status, ai_status, ai_heartbeat_at)'
            ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
