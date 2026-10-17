"""Add lifecycle, retry and automatic sample-audit fields to S1 summaries."""

from sqlalchemy import inspect, text

from extensions import db


_TABLE_NAME = 'adventurer_s1_description_ai_cache'
_COLUMN_DDLS = {
    'status': "VARCHAR(24) NOT NULL DEFAULT 'active'",
    'failure_count': 'INTEGER NOT NULL DEFAULT 0',
    'next_retry_at': 'TIMESTAMP',
    'processing_token': 'VARCHAR(64)',
    'model_used': 'VARCHAR(120)',
    'audit_status': "VARCHAR(24) NOT NULL DEFAULT 'not_sampled'",
    'audit_detail': 'TEXT',
    'audited_at': 'TIMESTAMP',
}


def migrate(*, engine=None) -> None:
    bind = engine or db.engine
    inspector = inspect(bind)
    if _TABLE_NAME not in inspector.get_table_names():
        return

    with bind.begin() as conn:
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
        conn.execute(text(
            f'UPDATE {_TABLE_NAME} SET status = :active '
            'WHERE status IS NULL OR status = :empty'
        ), {'active': 'active', 'empty': ''})
        conn.execute(text(
            f'UPDATE {_TABLE_NAME} SET failure_count = 0 '
            'WHERE failure_count IS NULL'
        ))
        conn.execute(text(
            f'UPDATE {_TABLE_NAME} SET audit_status = :not_sampled '
            'WHERE audit_status IS NULL OR audit_status = :empty'
        ), {'not_sampled': 'not_sampled', 'empty': ''})
        conn.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_advs1_desc_ai_status '
            f'ON {_TABLE_NAME} (status)'
        ))
        conn.execute(text(
            f'CREATE INDEX IF NOT EXISTS ix_advs1_desc_ai_audit_status '
            f'ON {_TABLE_NAME} (audit_status)'
        ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
