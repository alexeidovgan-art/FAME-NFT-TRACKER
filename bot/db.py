import os, sqlite3
from contextlib import contextmanager

DB = os.getenv("DATABASE_PATH", "data.sqlite3")

@contextmanager
def conn():
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()

def init_db():
    with conn() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, price TEXT NOT NULL,
            details TEXT NOT NULL, gift_url TEXT NOT NULL, owner_url TEXT,
            topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
            claimed_by INTEGER, message_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS workers (
            user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
        )""")

def set_worker(user_id):
    with conn() as db:
        db.execute("INSERT INTO workers(user_id,enabled) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET enabled=1", (user_id,))

def is_worker(user_id):
    with conn() as db:
        row=db.execute("SELECT enabled FROM workers WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["enabled"])

def get_listing(listing_id):
    with conn() as db:
        return db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()

def claim_listing(listing_id, user_id):
    with conn() as db:
        cur=db.execute("UPDATE listings SET status='claimed',claimed_by=? WHERE id=? AND status='open'", (user_id,listing_id))
        return cur.rowcount == 1
