"""为 S1 预约补齐玩家末次手动操作时间。"""

from sqlalchemy import inspect, text

from extensions import db


_MIGRATION_LOCK_KEY = 'adventurer_s1_reservation_owner_activity_v1'
_TABLE_NAME = 'adventurer_s1_reservations'
_COLUMN_NAME = 'last_owner_action_at'


def migrate(*, engine=None) -> None:
    """幂等增加预约活动时间列，旧数据由启动回填统一初始化。"""
    bind = engine or db.engine
    with bind.begin() as connection:
        if bind.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )

        table_names = set(inspect(connection).get_table_names())
        if _TABLE_NAME not in table_names:
            return
        columns = {
            column['name']
            for column in inspect(connection).get_columns(_TABLE_NAME)
        }
        if _COLUMN_NAME not in columns:
            connection.execute(text(
                f'ALTER TABLE "{_TABLE_NAME}" '
                f'ADD COLUMN "{_COLUMN_NAME}" TIMESTAMP NULL'
            ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
