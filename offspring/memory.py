"""
memory.py — Fen's persistent memory module.

Wraps a SQLite database for storing and retrieving memories across sessions.
No Hindsight, no external services — sovereign memory on-disk.

Schema
------
memories(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    context     TEXT DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    session_id  TEXT DEFAULT '',
    importance  INTEGER DEFAULT 5,
    tags        TEXT DEFAULT '',
    source      TEXT DEFAULT 'session'
)

Query patterns
--------------
- get_recent: by recency (most useful for context continuity)
- get_important: by importance score (pinned facts)
- search: keyword match on content/context/tags (basic retrieval beyond recency)
- get_session: all memories from a specific session (audit/replay)
- store: write new memories with flexible dict shapes
"""

import sqlite3
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Schema DDL (single source of truth)
# ---------------------------------------------------------------------------

_DDL_CREATE = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    context     TEXT DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    session_id  TEXT DEFAULT '',
    importance  INTEGER DEFAULT 5,
    tags        TEXT DEFAULT '',
    source      TEXT DEFAULT 'session'
)
"""

_DDL_IDX_CREATED = "CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at DESC)"
_DDL_IDX_IMPORTANCE = "CREATE INDEX IF NOT EXISTS idx_importance ON memories(importance DESC)"

# Column ordering for SELECT * queries (explicit, not SELECT *)
_COLUMNS = ("id", "content", "context", "importance", "tags", "session_id", "created_at", "source")
_SELECT = "SELECT id, content, context, importance, tags, session_id, created_at, source FROM memories"


def _row_to_dict(row: tuple) -> dict:
    """Convert a row tuple (matching _COLUMNS order) to a named dict."""
    return {
        "id": row[0],
        "content": row[1],
        "context": row[2],
        "importance": row[3],
        "tags": row[4],
        "session_id": row[5],
        "created_at": row[6],
        "source": row[7],
    }


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(db_path) -> Optional[sqlite3.Connection]:
    """
    Open (or create) the SQLite memory database at db_path.

    Creates schema and indexes if the file is new. Returns an open
    sqlite3.Connection. Caller is responsible for closing it.

    Returns None on error (so callers can use `if db is not None` guards).
    """
    path = Path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path))
        db.execute(_DDL_CREATE)
        db.execute(_DDL_IDX_CREATED)
        db.execute(_DDL_IDX_IMPORTANCE)
        db.commit()
        return db
    except Exception as e:
        print(f"[memory.py] Warning: could not open memory db at {db_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def get_recent(db: Optional[sqlite3.Connection], limit: int = 10) -> list[dict]:
    """
    Return the N most recent memories, ordered by created_at DESC.

    Returns an empty list if db is None or on query error.
    """
    if db is None:
        return []
    try:
        rows = db.execute(
            f"{_SELECT} ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[memory.py] Warning: get_recent failed: {e}")
        return []


def get_important(db: Optional[sqlite3.Connection], limit: int = 5) -> list[dict]:
    """
    Return the N highest-importance memories, ordered by importance DESC.

    Returns an empty list if db is None or on query error.
    """
    if db is None:
        return []
    try:
        rows = db.execute(
            f"{_SELECT} ORDER BY importance DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[memory.py] Warning: get_important failed: {e}")
        return []


def search(db: Optional[sqlite3.Connection], query: str, limit: int = 10) -> list[dict]:
    """
    Keyword search: WHERE content LIKE '%query%' OR context LIKE '%query%' OR tags LIKE '%query%'.

    Ordered by importance DESC so the most salient match surfaces first.
    Returns an empty list if db is None or on query error.
    """
    if db is None:
        return []
    try:
        pattern = f"%{query}%"
        rows = db.execute(
            f"{_SELECT} WHERE content LIKE ? OR context LIKE ? OR tags LIKE ? "
            f"ORDER BY importance DESC LIMIT ?",
            (pattern, pattern, pattern, limit)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[memory.py] Warning: search failed: {e}")
        return []


def get_session(db: Optional[sqlite3.Connection], session_id: str) -> list[dict]:
    """
    Return all memories from a specific session, ordered by created_at ASC.

    Useful for audit and replay. Returns an empty list on error.
    """
    if db is None:
        return []
    try:
        rows = db.execute(
            f"{_SELECT} WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[memory.py] Warning: get_session failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store(db: Optional[sqlite3.Connection], memories: list[dict], session_id: str) -> None:
    """
    Store a list of memory dicts to the database.

    Accepts flexible dict shapes — only 'content' is required per entry.
    Defaults: importance=5, source='session', context='', tags=''.

    No-op if db is None or memories is empty.
    """
    if db is None or not memories:
        return
    try:
        for m in memories:
            content = m.get("content", "")
            if not content:
                continue  # Skip blank content silently
            db.execute(
                "INSERT INTO memories (content, context, session_id, importance, tags, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    content,
                    m.get("context", ""),
                    session_id,
                    int(m.get("importance", 5)),
                    m.get("tags", ""),
                    m.get("source", "session"),
                )
            )
        db.commit()
    except Exception as e:
        print(f"[memory.py] Warning: could not store memories: {e}")
