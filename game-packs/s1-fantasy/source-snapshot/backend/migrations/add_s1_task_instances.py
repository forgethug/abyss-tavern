"""新增任务实例表，用于任务栏建筑的任务接取与完成跟踪。"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'gupiao.db')


def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adventurer_s1_task_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adventurer_id INTEGER NOT NULL REFERENCES adventurer_s1(id) ON DELETE CASCADE,
            task_template_id VARCHAR(50) NOT NULL,
            building_instance_id INTEGER REFERENCES adventurer_s1_building_instances(id) ON DELETE SET NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'accepted',
            started_at DATETIME NOT NULL,
            completed_at DATETIME,
            created_at DATETIME NOT NULL
        )
    """)

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_advs1_task_adv ON adventurer_s1_task_instances (adventurer_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_advs1_task_status ON adventurer_s1_task_instances (status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_advs1_task_adv_status ON adventurer_s1_task_instances (adventurer_id, status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_advs1_task_building ON adventurer_s1_task_instances (building_instance_id)"
    )

    conn.commit()
    conn.close()
    print("Migration add_s1_task_instances completed")


if __name__ == '__main__':
    migrate()
