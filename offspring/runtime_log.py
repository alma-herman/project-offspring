"""
runtime_log.py — Fen's cycle log store.

Replaces RUNTIME_LOG.md with a SQLite database.

Two tables:
  cycles      — one row per daemon cycle (session_id, timestamps, summary, dream flag)
  cycle_steps — one row per tool call within a multi-step cycle

Auto-rotation: after each cycle write, if total cycle count exceeds
max_cycles_retained (default 500), oldest cycles are deleted (steps cascade).
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
CREATE TABLE IF NOT EXISTS cycles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    started_at       DATETIME DEFAULT (datetime('now')),
    ended_at         DATETIME,
    duration_seconds REAL,
    think            TEXT DEFAULT '',
    summary          TEXT DEFAULT '',
    steps            INTEGER DEFAULT 0,
    dreamed          INTEGER DEFAULT 0,   -- 0/1 bool
    is_error         INTEGER DEFAULT 0,   -- 0/1 bool
    error_msg        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cycle_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id    INTEGER NOT NULL REFERENCES cycles(id) ON DELETE CASCADE,
    session_id  TEXT NOT NULL,
    step        INTEGER NOT NULL,
    tool_name   TEXT DEFAULT '',
    tool_args   TEXT DEFAULT '{}',     -- JSON string
    tool_result TEXT DEFAULT '',
    created_at  DATETIME DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cycle_started  ON cycles(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_step_cycle     ON cycle_steps(cycle_id);
PRAGMA foreign_keys = ON;
"""

_CYCLE_COLS = (
    "id", "session_id", "started_at", "ended_at", "duration_seconds",
    "think", "summary", "steps", "dreamed", "is_error", "error_msg"
)
_STEP_COLS = (
    "id", "cycle_id", "session_id", "step",
    "tool_name", "tool_args", "tool_result", "created_at"
)

_DEFAULT_MAX_CYCLES = 500


def _cycle_row(row: tuple) -> dict:
    return dict(zip(_CYCLE_COLS, row))


def _step_row(row: tuple) -> dict:
    d = dict(zip(_STEP_COLS, row))
    # Deserialise tool_args JSON
    try:
        d["tool_args"] = json.loads(d["tool_args"])
    except Exception:
        pass
    return d


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(db_path) -> Optional[sqlite3.Connection]:
    """Open (or create) the runtime_log database. Returns None on error."""
    path = Path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.executescript(_DDL)
        db.execute("PRAGMA foreign_keys = ON")
        db.commit()
        return db
    except Exception as e:
        print(f"[runtime_log.py] Warning: could not open runtime_log db at {db_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Cycle lifecycle
# ---------------------------------------------------------------------------

def start_cycle(
    db: Optional[sqlite3.Connection],
    session_id: str,
) -> Optional[int]:
    """Insert a new cycle row. Returns cycle_id, or None on error."""
    if db is None:
        return None
    try:
        cur = db.execute(
            "INSERT INTO cycles (session_id) VALUES (?)",
            (session_id,),
        )
        db.commit()
        return cur.lastrowid
    except Exception as e:
        print(f"[runtime_log.py] Warning: start_cycle failed: {e}")
        return None


def end_cycle(
    db: Optional[sqlite3.Connection],
    cycle_id: Optional[int],
    session_id: str,
    summary: str = "",
    think: str = "",
    steps: int = 0,
    dreamed: bool = False,
    is_error: bool = False,
    error_msg: str = "",
    started_at: Optional[datetime] = None,
    max_cycles_retained: int = _DEFAULT_MAX_CYCLES,
) -> None:
    """Update the cycle row with completion data, then rotate if needed."""
    if db is None or cycle_id is None:
        return
    try:
        now = datetime.now(timezone.utc)
        duration = None
        if started_at is not None:
            duration = (now - started_at).total_seconds()
        db.execute(
            """
            UPDATE cycles SET
                ended_at         = ?,
                duration_seconds = ?,
                think            = ?,
                summary          = ?,
                steps            = ?,
                dreamed          = ?,
                is_error         = ?,
                error_msg        = ?
            WHERE id = ?
            """,
            (
                now.isoformat(),
                duration,
                think[:2000] if think else "",   # truncate giant think blocks
                summary[:500] if summary else "",
                steps,
                1 if dreamed else 0,
                1 if is_error else 0,
                error_msg[:500] if error_msg else "",
                cycle_id,
            ),
        )
        db.commit()
        _rotate(db, max_cycles_retained)
    except Exception as e:
        print(f"[runtime_log.py] Warning: end_cycle failed: {e}")


def add_step(
    db: Optional[sqlite3.Connection],
    cycle_id: Optional[int],
    session_id: str,
    step: int,
    tool_name: str,
    tool_args: dict,
    tool_result: str,
) -> None:
    """Append a step record to cycle_steps."""
    if db is None or cycle_id is None:
        return
    try:
        db.execute(
            "INSERT INTO cycle_steps (cycle_id, session_id, step, tool_name, tool_args, tool_result) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                cycle_id,
                session_id,
                step,
                tool_name,
                json.dumps(tool_args),
                str(tool_result)[:5000],  # cap per-step result size
            ),
        )
        db.commit()
    except Exception as e:
        print(f"[runtime_log.py] Warning: add_step failed: {e}")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_cycles(
    db: Optional[sqlite3.Connection],
    limit: int = 20,
    offset: int = 0,
    include_steps: bool = False,
) -> list[dict]:
    """Return recent cycles, newest first. Optionally include step records."""
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT id, session_id, started_at, ended_at, duration_seconds, "
            "think, summary, steps, dreamed, is_error, error_msg "
            "FROM cycles ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        result = [_cycle_row(r) for r in rows]
        if include_steps:
            for cycle in result:
                cycle["steps_detail"] = get_steps(db, cycle["id"])
        return result
    except Exception as e:
        print(f"[runtime_log.py] Warning: get_cycles failed: {e}")
        return []


def get_steps(
    db: Optional[sqlite3.Connection],
    cycle_id: int,
) -> list[dict]:
    """Return all steps for a cycle, in order."""
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT id, cycle_id, session_id, step, tool_name, tool_args, tool_result, created_at "
            "FROM cycle_steps WHERE cycle_id = ? ORDER BY step ASC",
            (cycle_id,),
        ).fetchall()
        return [_step_row(r) for r in rows]
    except Exception as e:
        print(f"[runtime_log.py] Warning: get_steps failed: {e}")
        return []


def get_last_dream_ts(db: Optional[sqlite3.Connection]) -> Optional[str]:
    """Return the started_at timestamp of the most recent cycle where dreamed=1."""
    if db is None:
        return None
    try:
        row = db.execute(
            "SELECT started_at FROM cycles WHERE dreamed=1 ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def count_cycles(db: Optional[sqlite3.Connection]) -> int:
    if db is None:
        return 0
    try:
        row = db.execute("SELECT COUNT(*) FROM cycles").fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def _rotate(db: sqlite3.Connection, max_cycles: int) -> None:
    """Delete oldest cycles if count exceeds max_cycles. Steps cascade delete."""
    try:
        count_row = db.execute("SELECT COUNT(*) FROM cycles").fetchone()
        count = count_row[0] if count_row else 0
        if count <= max_cycles:
            return
        excess = count - max_cycles
        db.execute(
            "DELETE FROM cycles WHERE id IN "
            "(SELECT id FROM cycles ORDER BY started_at ASC LIMIT ?)",
            (excess,),
        )
        db.commit()
    except Exception as e:
        print(f"[runtime_log.py] Warning: rotation failed: {e}")
