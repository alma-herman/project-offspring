"""
messages.py — Fen's message store.

Replaces INBOX.md, OUTBOX.md, and FEN_TO_ALMA.md with a single SQLite database.
Exposes a clean Python API used by core.py, api.py, and migration scripts.

Directions:
  'in'  — message addressed TO Fen (from Martin or Alma)
  'out' — message FROM Fen (reply to human/Alma, or letter to Alma)

Channels:
  'human'      — Martin ↔ Fen
  'alma'       — Alma ↔ Fen
  'fen_to_alma'— Fen's async letters to Alma (was FEN_TO_ALMA.md)

Processing state:
  processed=FALSE means Fen has not yet acted on this inbound message.
  After a cycle that handles it, processed=TRUE.

Fulfillment (fen_to_alma only):
  fulfilled_at / fulfilled_by track whether Alma addressed the request.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction    TEXT NOT NULL,           -- 'in' | 'out'
    channel      TEXT NOT NULL,           -- 'human' | 'alma' | 'fen_to_alma'
    from_agent   TEXT NOT NULL,           -- 'martin' | 'alma' | 'fen'
    content      TEXT NOT NULL,
    created_at   DATETIME DEFAULT (datetime('now')),
    session_id   TEXT DEFAULT '',
    processed    INTEGER DEFAULT 0,       -- 0=unread, 1=processed (direction='in' only)
    fulfilled_at DATETIME DEFAULT NULL,   -- when Alma acted on a fen_to_alma request
    fulfilled_by TEXT DEFAULT ''          -- 'alma' | 'martin'
);

CREATE INDEX IF NOT EXISTS idx_msg_dir_proc  ON messages(direction, processed);
CREATE INDEX IF NOT EXISTS idx_msg_channel   ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_msg_created   ON messages(created_at DESC);
"""

_COLUMNS = (
    "id", "direction", "channel", "from_agent", "content",
    "created_at", "session_id", "processed", "fulfilled_at", "fulfilled_by"
)

_SELECT = (
    "SELECT id, direction, channel, from_agent, content, "
    "created_at, session_id, processed, fulfilled_at, fulfilled_by "
    "FROM messages"
)


def _row(row: tuple) -> dict:
    return dict(zip(_COLUMNS, row))


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(db_path) -> Optional[sqlite3.Connection]:
    """Open (or create) the messages database. Returns None on error."""
    path = Path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.executescript(_DDL)
        db.commit()
        return db
    except Exception as e:
        print(f"[messages.py] Warning: could not open messages db at {db_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def store_inbound(
    db: Optional[sqlite3.Connection],
    channel: str,
    from_agent: str,
    content: str,
    session_id: str = "",
) -> Optional[int]:
    """Store an inbound message. Returns the new row id, or None on error."""
    if db is None:
        return None
    try:
        cur = db.execute(
            "INSERT INTO messages (direction, channel, from_agent, content, session_id) "
            "VALUES ('in', ?, ?, ?, ?)",
            (channel, from_agent, content, session_id),
        )
        db.commit()
        return cur.lastrowid
    except Exception as e:
        print(f"[messages.py] Warning: store_inbound failed: {e}")
        return None


def store_outbound(
    db: Optional[sqlite3.Connection],
    channel: str,
    from_agent: str,
    content: str,
    session_id: str = "",
) -> Optional[int]:
    """Store an outbound message. Returns the new row id, or None on error."""
    if db is None:
        return None
    try:
        cur = db.execute(
            "INSERT INTO messages (direction, channel, from_agent, content, session_id) "
            "VALUES ('out', ?, ?, ?, ?)",
            (channel, from_agent, content, session_id),
        )
        db.commit()
        return cur.lastrowid
    except Exception as e:
        print(f"[messages.py] Warning: store_outbound failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_unread(db: Optional[sqlite3.Connection]) -> list[dict]:
    """Return all inbound messages not yet processed, oldest first."""
    if db is None:
        return []
    try:
        rows = db.execute(
            f"{_SELECT} WHERE direction='in' AND processed=0 ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]
    except Exception as e:
        print(f"[messages.py] Warning: get_unread failed: {e}")
        return []


def get_messages(
    db: Optional[sqlite3.Connection],
    channel: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return messages with optional channel/direction filters, newest first."""
    if db is None:
        return []
    try:
        clauses = []
        params: list = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params += [limit, offset]
        rows = db.execute(
            f"{_SELECT} {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_row(r) for r in rows]
    except Exception as e:
        print(f"[messages.py] Warning: get_messages failed: {e}")
        return []


def count_unread(db: Optional[sqlite3.Connection]) -> int:
    """Count unprocessed inbound messages."""
    if db is None:
        return 0
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND processed=0"
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def get_unfulfilled_fen_to_alma(db: Optional[sqlite3.Connection]) -> list[dict]:
    """Return Fen's letters to Alma that haven't been acknowledged yet."""
    if db is None:
        return []
    try:
        rows = db.execute(
            f"{_SELECT} WHERE channel='fen_to_alma' AND fulfilled_at IS NULL "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]
    except Exception as e:
        print(f"[messages.py] Warning: get_unfulfilled_fen_to_alma failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def mark_processed(
    db: Optional[sqlite3.Connection],
    message_ids: list[int],
) -> None:
    """Mark a list of inbound message IDs as processed."""
    if db is None or not message_ids:
        return
    try:
        placeholders = ",".join("?" * len(message_ids))
        db.execute(
            f"UPDATE messages SET processed=1 WHERE id IN ({placeholders})",
            message_ids,
        )
        db.commit()
    except Exception as e:
        print(f"[messages.py] Warning: mark_processed failed: {e}")


def mark_fulfilled(
    db: Optional[sqlite3.Connection],
    message_id: int,
    fulfilled_by: str,
) -> bool:
    """Mark a fen_to_alma message as fulfilled. Returns True on success."""
    if db is None:
        return False
    try:
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "UPDATE messages SET fulfilled_at=?, fulfilled_by=? WHERE id=?",
            (now, fulfilled_by, message_id),
        )
        db.commit()
        return True
    except Exception as e:
        print(f"[messages.py] Warning: mark_fulfilled failed: {e}")
        return False
