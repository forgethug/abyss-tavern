"""为建筑贡献明细增加 purpose（build / upgrade）：捐献表 + 帮工表。"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'gupiao.db')

_TABLES = (
    ('adventurer_s1_building_donations', 'ix_advs1_building_donations_purpose'),
    ('adventurer_s1_building_helpers', 'ix_advs1_building_helpers_purpose'),
)


def _add_purpose_column(cursor, table: str, index_name: str) -> None:
    cursor.execute(f'PRAGMA table_info({table})')
    columns = [row[1] for row in cursor.fetchall()]
    if 'purpose' in columns:
        return
    cursor.execute(
        f'ALTER TABLE {table} '
        "ADD COLUMN purpose VARCHAR(20) NOT NULL DEFAULT 'build'"
    )
    cursor.execute(
        f'CREATE INDEX IF NOT EXISTS {index_name} ON {table} (purpose)'
    )
    print(f'Added purpose to {table}')


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for table, index_name in _TABLES:
        _add_purpose_column(cursor, table, index_name)

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_advs1_donation_inst_purpose_mat '
        'ON adventurer_s1_building_donations (building_instance_id, purpose, material_name)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_advs1_helper_inst_purpose '
        'ON adventurer_s1_building_helpers (building_instance_id, purpose)'
    )

    conn.commit()
    conn.close()
    print('Migration add_s1_building_donation_purpose completed')


if __name__ == '__main__':
    migrate()
