"""为 S1 纯正面特质装卸补齐持久化字段。"""

from sqlalchemy import inspect, text

from extensions import db


_MIGRATION_LOCK_KEY = 'adventurer_s1_trait_loadout_v1'


def migrate(*, engine=None) -> None:
    """幂等补齐玩家选择与独立效果性质审核状态。"""
    bind = engine or db.engine
    with bind.begin() as connection:
        if bind.dialect.name == 'postgresql':
            connection.execute(
                text('SELECT pg_advisory_xact_lock(hashtext(:lock_key))'),
                {'lock_key': _MIGRATION_LOCK_KEY},
            )

        table_names = set(inspect(connection).get_table_names())
        if 'adventurer_s1_traits' in table_names:
            trait_columns = {
                column['name']
                for column in inspect(connection).get_columns(
                    'adventurer_s1_traits'
                )
            }
            if 'equipped' not in trait_columns:
                connection.execute(text(
                    'ALTER TABLE "adventurer_s1_traits" '
                    'ADD COLUMN "equipped" BOOLEAN NOT NULL DEFAULT TRUE'
                ))

        cache_table = 'adventurer_s1_description_ai_cache'
        if cache_table in table_names:
            cache_columns = {
                column['name']
                for column in inspect(connection).get_columns(cache_table)
            }
            if 'trait_effect_polarity_audit_status' not in cache_columns:
                connection.execute(text(
                    f'ALTER TABLE "{cache_table}" '
                    'ADD COLUMN "trait_effect_polarity_audit_status" '
                    "VARCHAR(24) NOT NULL DEFAULT 'not_sampled'"
                ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
