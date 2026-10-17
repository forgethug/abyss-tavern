"""新增 S1 幸运大转盘成功抽取历史表。"""

import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'gupiao.db')


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adventurer_s1_lucky_wheel_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adventurer_id INTEGER NOT NULL REFERENCES adventurer_s1(id) ON DELETE CASCADE,
            building_instance_id INTEGER NOT NULL
                REFERENCES adventurer_s1_building_instances(id) ON DELETE CASCADE,
            source_currency_log_id INTEGER NULL UNIQUE
                REFERENCES adventurer_s1_currency_logs(id) ON DELETE SET NULL,
            item_name VARCHAR(100) NOT NULL,
            item_rarity VARCHAR(20) NOT NULL,
            payment VARCHAR(10) NOT NULL,
            gold_cost INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
    """)
    columns = {
        row[1] for row in cursor.execute(
            'PRAGMA table_info(adventurer_s1_lucky_wheel_spins)'
        ).fetchall()
    }
    if 'source_currency_log_id' not in columns:
        cursor.execute(
            'ALTER TABLE adventurer_s1_lucky_wheel_spins '
            'ADD COLUMN source_currency_log_id INTEGER NULL '
            'REFERENCES adventurer_s1_currency_logs(id) ON DELETE SET NULL'
        )
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_advs1_wheel_spin_currency_log
        ON adventurer_s1_lucky_wheel_spins (source_currency_log_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_advs1_wheel_spin_adv_created
        ON adventurer_s1_lucky_wheel_spins (adventurer_id, created_at)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_advs1_wheel_spin_building_created
        ON adventurer_s1_lucky_wheel_spins (building_instance_id, created_at)
    """)
    conn.commit()
    conn.close()
    print('Migration add_s1_lucky_wheel_spins completed')


if __name__ == '__main__':
    migrate()
