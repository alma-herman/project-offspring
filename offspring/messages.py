"""SQLite wrapper for messages.db — inter-agent message store."""

import sqlite3
from datetime import datetime, timezone


def connect(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,
            channel TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            session_id TEXT DEFAULT '',
            processed BOOLEAN DEFAULT FALSE,
            fulfilled_at DATETIME DEFAULT NULL,
            fulfilled_by TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_direction ON messages(direction, processed);
        CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel);
        CREATE INDEX IF NOT EXISTS idx_created ON messages(created_at DESC);
    """)
    db.commit()
    return db


def store_inbound(db: sqlite3.Connection, channel: str, from_agent: str,
                  content: str, session_id: str = '') -> int:
    cur = db.execute(
        "INSERT INTO messages (direction, channel, from_agent, content, session_id) "
        "VALUES ('in', ?, ?, ?, ?)",
        (channel, from_agent, content, session_id),
    )
    db.commit()
    return cur.lastrowid


def store_outbound(db: sqlite3.Connection, channel: str, from_agent: str,
                   content: str, session_id: str = '') -> int:
    cur = db.execute(
        "INSERT INTO messages (direction, channel, from_agent, content, session_id) "
        "VALUES ('out', ?, ?, ?, ?)",
        (channel, from_agent, content, session_id),
    )
    db.commit()
    return cur.lastrowid


def get_unread(db: sqlite3.Connection) -> list:
    rows = db.execute(
        "SELECT * FROM messages WHERE direction='in' AND processed=FALSE "
        "ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_unread_count(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT COUNT(*) FROM messages WHERE direction='in' AND processed=FALSE"
    ).fetchone()
    return row[0]


def mark_processed(db: sqlite3.Connection, ids: list) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    db.execute(
        f"UPDATE messages SET processed=TRUE WHERE id IN ({placeholders})", ids
    )
    db.commit()


def get_messages(db: sqlite3.Connection, channel: str = None,
                 direction: str = None, limit: int = 50,
                 offset: int = 0) -> list:
    query = "SELECT * FROM messages WHERE 1=1"
    params = []
    if channel is not None:
        query += " AND channel=?"
        params.append(channel)
    if direction is not None:
        query += " AND direction=?"
        params.append(direction)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def fulfill(db: sqlite3.Connection, message_id: int, fulfilled_by: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE messages SET fulfilled_at=?, fulfilled_by=? WHERE id=?",
        (now, fulfilled_by, message_id),
    )
    db.commit()
