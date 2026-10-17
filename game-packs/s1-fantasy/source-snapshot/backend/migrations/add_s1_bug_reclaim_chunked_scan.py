"""Add resumable S1 bug-reclaim scan state and source-owned candidates."""

from sqlalchemy import inspect, text

from extensions import db


_SCAN_STATE_COLUMNS = {
    'dirty_revision': 'INTEGER NOT NULL DEFAULT 0',
    'scan_status': "VARCHAR(20) NOT NULL DEFAULT 'ready'",
    'scan_mode': "VARCHAR(20) NOT NULL DEFAULT 'incremental'",
    'scan_base_audit_log_id': 'INTEGER NOT NULL DEFAULT 0',
    'scan_target_audit_log_id': 'INTEGER NOT NULL DEFAULT 0',
    'scan_cursor_adventurer_id': 'INTEGER NOT NULL DEFAULT 0',
    'scan_total_adventurers': 'INTEGER NOT NULL DEFAULT 0',
    'scan_processed_adventurers': 'INTEGER NOT NULL DEFAULT 0',
    'scan_claim_token': 'VARCHAR(64)',
    'scan_claimed_at': 'TIMESTAMP',
    'scan_last_error': 'TEXT',
}

_CANDIDATE_COLUMNS = {
    'source_adventurer_id': 'INTEGER NOT NULL DEFAULT 0',
}


def _add_missing_columns(connection, inspector, table_name: str, columns: dict[str, str]) -> None:
    existing = {column['name'] for column in inspector.get_columns(table_name)}
    for column_name, column_ddl in columns.items():
        if column_name in existing:
            continue
        connection.execute(text(
            f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_ddl}'
        ))
        existing.add(column_name)


def migrate() -> None:
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    state_table = 'adventurer_s1_bug_reclaim_scan_states'
    candidate_table = 'adventurer_s1_bug_reclaim_candidates'
    market_listing_table = 'adventurer_s1_market_listings'

    with db.engine.begin() as connection:
        if state_table in table_names:
            _add_missing_columns(
                connection, inspector, state_table, _SCAN_STATE_COLUMNS,
            )
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_advs1_bug_reclaim_scan_status '
                f'ON {state_table} (scan_status)'
            ))

        if candidate_table in table_names:
            _add_missing_columns(
                connection, inspector, candidate_table, _CANDIDATE_COLUMNS,
            )
            connection.execute(text(
                f'UPDATE {candidate_table} '
                'SET source_adventurer_id = adventurer_id '
                'WHERE source_adventurer_id IS NULL OR source_adventurer_id = 0'
            ))
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS '
                'idx_advs1_bug_reclaim_candidate_source_mode '
                f'ON {candidate_table} (source_adventurer_id, detection_mode)'
            ))

        if market_listing_table in table_names:
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS idx_advs1_market_listing_buyer_status '
                f'ON {market_listing_table} (buyer_adventurer_id, status)'
            ))


if __name__ == '__main__':
    from app import create_app

    app = create_app('development')
    with app.app_context():
        migrate()
        print('migration done')
