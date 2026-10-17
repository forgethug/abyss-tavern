"""Add generation_phase / generation_phase_at to S1 expedition logs (AI 生成心跳)."""
from sqlalchemy import inspect, text

from extensions import db


def _add_column_if_missing(conn, existing: set[str], ddl: str, column_name: str) -> None:
    if column_name in existing:
        return
    conn.execute(text(ddl))
    existing.add(column_name)


def migrate() -> None:
    inspector = inspect(db.engine)
    if 'adventurer_s1_expedition_logs' not in inspector.get_table_names():
        return
    with db.engine.begin() as conn:
        existing = {
            col['name']
            for col in inspector.get_columns('adventurer_s1_expedition_logs')
        }
        _add_column_if_missing(
            conn, existing,
            'ALTER TABLE adventurer_s1_expedition_logs ADD COLUMN generation_phase VARCHAR(20)',
            'generation_phase',
        )
        _add_column_if_missing(
            conn, existing,
            'ALTER TABLE adventurer_s1_expedition_logs ADD COLUMN generation_phase_at TIMESTAMP',
            'generation_phase_at',
        )
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_expedition_logs_generation_phase '
            'ON adventurer_s1_expedition_logs (generation_phase)'
        ))
        conn.execute(text(
            'CREATE INDEX IF NOT EXISTS ix_adventurer_s1_expedition_logs_generation_phase_at '
            'ON adventurer_s1_expedition_logs (generation_phase_at)'
        ))


if __name__ == '__main__':
    from app import create_app
    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
