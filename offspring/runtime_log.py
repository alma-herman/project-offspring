"""SQLite wrapper for runtime_log.db — cycle and step logging with rotation."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class RuntimeLog:
    db: sqlite3.Connection
    max_cycles_retained: int = 500


def connect(db_path: str, max_cycles_retained: int = 500) -> RuntimeLog:
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ended_at DATETIME,
            duration_seconds REAL,
            think TEXT,
            summary TEXT,
            steps INTEGER DEFAULT 1,
            dreamed BOOLEAN DEFAULT FALSE,
            is_error BOOLEAN DEFAULT FALSE,
            error_msg TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS cycle_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER REFERENCES cycles(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL,
            step INTEGER NOT NULL,
            tool_name TEXT,
            tool_args TEXT,
            tool_result TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_cycle_started ON cycles(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_step_cycle ON cycle_steps(cycle_id);
    """)
    db.commit()
    return RuntimeLog(db=db, max_cycles_retained=max_cycles_retained)


def start_cycle(log: RuntimeLog, session_id: str) -> int:
    cur = log.db.execute(
        "INSERT INTO cycles (session_id) VALUES (?)", (session_id,)
    )
    log.db.commit()
    return cur.lastrowid


def add_step(log: RuntimeLog, cycle_id: int, session_id: str, step: int,
             tool_name: str, tool_args_json: str, tool_result: str) -> None:
    log.db.execute(
        "INSERT INTO cycle_steps (cycle_id, session_id, step, tool_name, tool_args, tool_result) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (cycle_id, session_id, step, tool_name, tool_args_json, tool_result),
    )
    log.db.commit()


def end_cycle(log: RuntimeLog, cycle_id: int, think: str = '',
              summary: str = '', steps: int = 1, dreamed: bool = False,
              is_error: bool = False, error_msg: str = '') -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = log.db.execute(
        "SELECT started_at FROM cycles WHERE id=?", (cycle_id,)
    ).fetchone()
    duration = None
    if row and row["started_at"]:
        try:
            started = datetime.fromisoformat(row["started_at"].replace(" ", "T"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            ended = datetime.now(timezone.utc)
            duration = (ended - started).total_seconds()
        except Exception:
            duration = None

    log.db.execute(
        "UPDATE cycles SET ended_at=?, duration_seconds=?, think=?, summary=?, "
        "steps=?, dreamed=?, is_error=?, error_msg=? WHERE id=?",
        (now, duration, think, summary, steps, dreamed, is_error, error_msg, cycle_id),
    )
    log.db.commit()
    _rotate(log)


def _rotate(log: RuntimeLog) -> None:
    count = get_cycle_count(log)
    if count > log.max_cycles_retained:
        excess = count - log.max_cycles_retained
        log.db.execute(
            "DELETE FROM cycles WHERE id IN ("
            "  SELECT id FROM cycles ORDER BY started_at ASC LIMIT ?"
            ")",
            (excess,),
        )
        log.db.commit()


def get_recent_cycles(log: RuntimeLog, limit: int = 20) -> list:
    rows = log.db.execute(
        "SELECT * FROM cycles ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_cycle_steps(log: RuntimeLog, cycle_id: int) -> list:
    rows = log.db.execute(
        "SELECT * FROM cycle_steps WHERE cycle_id=? ORDER BY step ASC",
        (cycle_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cycle_count(log: RuntimeLog) -> int:
    row = log.db.execute("SELECT COUNT(*) FROM cycles").fetchone()
    return row[0]


def record_dream(log: RuntimeLog, session_id: str, summary: str) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = log.db.execute(
        "INSERT INTO cycles (session_id, started_at, ended_at, dreamed, summary) "
        "VALUES (?, ?, ?, TRUE, ?)",
        (session_id, now, now, summary),
    )
    log.db.commit()
    _rotate(log)
    return cur.lastrowid
