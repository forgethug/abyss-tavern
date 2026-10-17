"""Add S1 full-text appeal daily/balance-pool quota persistence."""
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta

from sqlalchemy import inspect, text

from extensions import db


_APPEAL_TABLE = 'adventurer_s1_data_appeals'
_QUOTA_TABLE = 'adventurer_s1_data_appeal_quota'
_SETTINGS_TABLE = 'adventurer_s1_settings'
_ACTIVATION_KEY = 'data_appeal_other_balance_started_on'
_APPEAL_COLUMNS = {
    'quota_bucket_date': 'DATE',
    'quota_source': 'VARCHAR(20)',
}
_QUOTA_COLUMNS = {
    'other_balance': 'INTEGER NOT NULL DEFAULT 0',
    'other_balance_date': 'DATE',
    'other_daily_limit_snapshot': 'INTEGER',
}
_INDEXES = {
    'ix_advs1_data_appeal_user_quota_bucket': (
        'user_id, quota_bucket_date'
    ),
    'ix_advs1_data_appeal_user_quota_source_date': (
        'user_id, quota_source, quota_bucket_date'
    ),
}


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if _APPEAL_TABLE not in tables:
        return

    with db.engine.begin() as connection:
        connection_inspector = inspect(connection)
        appeal_columns = {
            column['name']
            for column in connection_inspector.get_columns(_APPEAL_TABLE)
        }
        for column_name, sql_type in _APPEAL_COLUMNS.items():
            if column_name not in appeal_columns:
                connection.execute(text(
                    f'ALTER TABLE {_APPEAL_TABLE} '
                    f'ADD COLUMN {column_name} {sql_type}'
                ))

        indexes = {
            index['name']
            for index in inspect(connection).get_indexes(_APPEAL_TABLE)
        }
        for index_name, index_columns in _INDEXES.items():
            if index_name not in indexes:
                connection.execute(text(
                    f'CREATE INDEX {index_name} '
                    f'ON {_APPEAL_TABLE} ({index_columns})'
                ))

        if _QUOTA_TABLE in tables:
            quota_columns = {
                column['name']
                for column in inspect(connection).get_columns(_QUOTA_TABLE)
            }
            for column_name, sql_type in _QUOTA_COLUMNS.items():
                if column_name not in quota_columns:
                    connection.execute(text(
                        f'ALTER TABLE {_QUOTA_TABLE} '
                        f'ADD COLUMN {column_name} {sql_type}'
                    ))

        if _SETTINGS_TABLE in tables:
            now = _portable_utc_now()
            beijing_date = (now + timedelta(hours=8)).date().isoformat()
            connection.execute(
                text(
                    f'INSERT INTO {_SETTINGS_TABLE} '
                    '(key, value, description, created_at, updated_at) '
                    'VALUES (:key, :value, :description, :now, :now) '
                    'ON CONFLICT (key) DO NOTHING'
                ),
                {
                    'key': _ACTIVATION_KEY,
                    'value': beijing_date,
                    'description': '全文申诉余额池启用的北京时间日期',
                    'now': now,
                },
            )


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
