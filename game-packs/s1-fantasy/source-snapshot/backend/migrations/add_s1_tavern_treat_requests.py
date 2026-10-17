from extensions import db
from sqlalchemy import text


with db.engine.connect() as conn:
    id_column = (
        "id SERIAL PRIMARY KEY"
        if db.engine.dialect.name == 'postgresql'
        else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS adventurer_s1_tavern_treat_requests (
            {id_column},
            requester_user_id INTEGER NOT NULL,
            payer_adventurer_id INTEGER NOT NULL,
            payer_seat_id INTEGER NOT NULL,
            target_user_id INTEGER NOT NULL,
            target_adventurer_id INTEGER NOT NULL,
            target_seat_id INTEGER NOT NULL,
            drink_key VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            responded_at DATETIME NULL,
            FOREIGN KEY (requester_user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (payer_adventurer_id) REFERENCES adventurer_s1(id) ON DELETE CASCADE,
            FOREIGN KEY (payer_seat_id) REFERENCES adventurer_s1_tavern_seats(id) ON DELETE CASCADE,
            FOREIGN KEY (target_user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (target_adventurer_id) REFERENCES adventurer_s1(id) ON DELETE CASCADE,
            FOREIGN KEY (target_seat_id) REFERENCES adventurer_s1_tavern_seats(id) ON DELETE CASCADE
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_s1_tavern_treat_target_status
        ON adventurer_s1_tavern_treat_requests(target_user_id, status, created_at)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_s1_tavern_treat_payer_status
        ON adventurer_s1_tavern_treat_requests(requester_user_id, status, created_at)
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_s1_tavern_treat_target_adv
        ON adventurer_s1_tavern_treat_requests(target_adventurer_id)
    """))
    conn.commit()

print("adventurer_s1_tavern_treat_requests migration completed")
