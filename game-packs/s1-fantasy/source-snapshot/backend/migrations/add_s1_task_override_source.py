"""为 adventurer_s1_task_overrides 增加 source_template_id（派生新任务版本）。"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'gupiao.db')


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(adventurer_s1_task_overrides)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'source_template_id' not in columns:
        cursor.execute(
            "ALTER TABLE adventurer_s1_task_overrides ADD COLUMN source_template_id VARCHAR(50)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_advs1_task_override_source "
            "ON adventurer_s1_task_overrides (source_template_id)"
        )
        print("Added source_template_id to adventurer_s1_task_overrides")

    conn.commit()
    conn.close()
    print("Migration add_s1_task_override_source completed")


if __name__ == '__main__':
    migrate()
