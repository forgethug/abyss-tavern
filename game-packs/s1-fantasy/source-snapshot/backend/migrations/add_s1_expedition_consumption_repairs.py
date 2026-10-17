"""新增 S1 历史探险物品漏扣追回台账。"""

import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'gupiao.db')


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adventurer_s1_expedition_consumption_repairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expedition_log_id INTEGER NOT NULL,
            adventurer_id INTEGER NOT NULL,
            item_name VARCHAR(100) NOT NULL,
            equipment_id INTEGER NULL,
            reclaimed_quantity INTEGER NOT NULL,
            item_snapshot JSON NOT NULL,
            admin_user_id INTEGER NOT NULL,
            restored_at DATETIME NULL,
            restored_by_user_id INTEGER NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT uq_advs1_expedition_consumption_repair
                UNIQUE (expedition_log_id, adventurer_id, item_name)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS ix_advs1_exp_consumption_repair_log
        ON adventurer_s1_expedition_consumption_repairs (expedition_log_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS ix_advs1_exp_consumption_repair_adv
        ON adventurer_s1_expedition_consumption_repairs (adventurer_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS ix_advs1_exp_consumption_repair_restored
        ON adventurer_s1_expedition_consumption_repairs (restored_at)
    """)
    conn.commit()
    conn.close()
    print('Migration add_s1_expedition_consumption_repairs completed')


if __name__ == '__main__':
    migrate()
