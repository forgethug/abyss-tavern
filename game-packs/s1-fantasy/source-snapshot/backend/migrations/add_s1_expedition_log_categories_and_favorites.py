"""Add indexed S1 expedition categories and per-user log favorites."""

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from extensions import db


def _backfill_special_event_themes(connection) -> None:
    from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog
    from plugins.adventurer_s1.services.adventurer_s1_config_service import (
        resolve_s1_special_event_theme,
    )

    session = Session(bind=connection, autoflush=False)
    last_id = 0
    try:
        while True:
            rows = (
                session.query(
                    AdventurerS1ExpeditionLog.id,
                    AdventurerS1ExpeditionLog.full_log,
                )
                .filter(
                    AdventurerS1ExpeditionLog.id > last_id,
                    AdventurerS1ExpeditionLog.special_event_theme.is_(None),
                )
                .order_by(AdventurerS1ExpeditionLog.id.asc())
                .limit(200)
                .all()
            )
            if not rows:
                break
            mappings = []
            for log_id, full_log in rows:
                last_id = int(log_id)
                theme = resolve_s1_special_event_theme(full_log if isinstance(full_log, dict) else {})
                mappings.append({'id': int(log_id), 'special_event_theme': theme or ''})
            session.bulk_update_mappings(AdventurerS1ExpeditionLog, mappings)
            session.flush()
            session.expunge_all()
    finally:
        session.close()


def migrate() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if 'adventurer_s1_expedition_logs' not in tables:
        return

    with db.engine.begin() as connection:
        log_columns = {
            column['name']
            for column in inspector.get_columns('adventurer_s1_expedition_logs')
        }
        if 'special_event_theme' not in log_columns:
            connection.execute(text(
                'ALTER TABLE adventurer_s1_expedition_logs '
                'ADD COLUMN special_event_theme VARCHAR(32)'
            ))
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_expedition_logs_special_event_theme '
            'ON adventurer_s1_expedition_logs (special_event_theme)'
        ))

        if 'adventurer_s1_expedition_favorites' not in tables:
            id_column = 'SERIAL PRIMARY KEY'
            if db.engine.dialect.name != 'postgresql':
                id_column = 'INTEGER PRIMARY KEY AUTOINCREMENT'
            connection.execute(text(f"""
                CREATE TABLE adventurer_s1_expedition_favorites (
                    id {id_column},
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expedition_log_id INTEGER NOT NULL
                        REFERENCES adventurer_s1_expedition_logs(id) ON DELETE CASCADE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_advs1_expedition_favorite_user_log
                        UNIQUE (user_id, expedition_log_id)
                )
            """))
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_expedition_favorites_user_id '
            'ON adventurer_s1_expedition_favorites (user_id)'
        ))
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_expedition_favorites_expedition_log_id '
            'ON adventurer_s1_expedition_favorites (expedition_log_id)'
        ))
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_advs1_expedition_favorite_user_created '
            'ON adventurer_s1_expedition_favorites (user_id, created_at)'
        ))

        _backfill_special_event_themes(connection)


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
