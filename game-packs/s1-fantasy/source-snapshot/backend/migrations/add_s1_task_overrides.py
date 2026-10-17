"""新增任务栏运营覆盖表 adventurer_s1_task_overrides。"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'gupiao.db')


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adventurer_s1_task_overrides (
            task_template_id VARCHAR(50) PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT 1,
            quota_reset_at DATETIME,
            overrides_json TEXT,
            updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    print("Migration add_s1_task_overrides completed")


if __name__ == '__main__':
    migrate()
