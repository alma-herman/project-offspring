"""
api.py — Fen's FastAPI message service.

Runs on localhost:7744 as a background thread inside the daemon.
PHP UI and the caretaker cron interact with Fen through this API instead
of writing raw files.

Endpoints:
  POST /messages                    — submit an inbound message to Fen
  GET  /messages/unread             — messages Fen hasn't processed yet (daemon use)
  POST /messages/{id}/processed     — mark a message processed (daemon use)
  GET  /messages                    — paginated conversation view (PHP UI)
  PATCH /messages/{id}/fulfill      — mark a fen_to_alma request fulfilled (caretaker)
  GET  /status                      — daemon health + counts (PHP stream.php)
  GET  /cycles                      — recent cycles, paginated (PHP cycles tab)
  GET  /cycles/{id}/steps           — steps for one cycle
  GET  /memories                    — memories, paginated + filterable (PHP memories tab)

Usage (from daemon):
    from offspring.api import start_api_thread, set_databases, wake_event
    set_databases(mem_db, msg_db, log_db, cfg)
    start_api_thread()
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Module-level state (set by daemon before starting the API thread)
# ---------------------------------------------------------------------------

_mem_db: Optional[sqlite3.Connection] = None
_msg_db: Optional[sqlite3.Connection] = None
_log_db: Optional[sqlite3.Connection] = None
_cfg = None           # Config object from core.py
_soul_path: Optional[Path] = None
_lock_path: Optional[Path] = None
_wake_event: Optional[threading.Event] = None   # signals daemon to wake early


def set_databases(mem_db, msg_db, log_db, cfg, soul_path: Path, lock_path: Path, wake_event: threading.Event):
    """Called by daemon before starting the API thread."""
    global _mem_db, _msg_db, _log_db, _cfg, _soul_path, _lock_path, _wake_event
    _mem_db = mem_db
    _msg_db = msg_db
    _log_db = log_db
    _cfg = cfg
    _soul_path = soul_path
    _lock_path = lock_path
    _wake_event = wake_event


# ---------------------------------------------------------------------------
# Lazy imports (avoid circular imports at module load time)
# ---------------------------------------------------------------------------

def _mem():
    from offspring import memory
    return memory

def _msg():
    from offspring import messages
    return messages

def _log():
    from offspring import runtime_log
    return runtime_log


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Fen API", version="1.0.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class MessageIn(BaseModel):
    direction: str = "in"
    channel: str            # 'human' | 'alma' | 'fen_to_alma'
    from_agent: str         # 'martin' | 'alma' | 'fen'
    content: str
    session_id: str = ""


class FulfillIn(BaseModel):
    fulfilled_by: str       # 'alma' | 'martin'


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/messages", status_code=200)
def post_message(body: MessageIn):
    """Submit a message to Fen (direction='in') or record an outbound message."""
    if body.direction == "in":
        msg_id = _msg().store_inbound(
            _msg_db,
            channel=body.channel,
            from_agent=body.from_agent,
            content=body.content,
            session_id=body.session_id,
        )
    else:
        msg_id = _msg().store_outbound(
            _msg_db,
            channel=body.channel,
            from_agent=body.from_agent,
            content=body.content,
            session_id=body.session_id,
        )
    if msg_id is None:
        raise HTTPException(status_code=500, detail="Failed to store message")
    # Wake daemon if an inbound message arrived
    if body.direction == "in" and _wake_event is not None:
        _wake_event.set()
    # Fetch the stored message to return it
    msgs = _msg().get_messages(_msg_db, limit=1, offset=0)
    for m in msgs:
        if m["id"] == msg_id:
            return m
    return {"id": msg_id}


@app.get("/messages/unread")
def get_unread():
    """Return all unprocessed inbound messages. Used by daemon at cycle start."""
    return _msg().get_unread(_msg_db)


@app.post("/messages/{message_id}/processed", status_code=200)
def mark_message_processed(message_id: int):
    """Mark an inbound message as processed. Used by daemon after handling."""
    _msg().mark_processed(_msg_db, [message_id])
    return {"ok": True}


@app.get("/messages")
def list_messages(
    channel: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    """Paginated message list. Used by PHP UI conversation tab."""
    return _msg().get_messages(
        _msg_db,
        channel=channel,
        direction=direction,
        limit=limit,
        offset=offset,
    )


@app.patch("/messages/{message_id}/fulfill")
def fulfill_message(message_id: int, body: FulfillIn):
    """Mark a fen_to_alma message as fulfilled. Used by caretaker cron."""
    ok = _msg().mark_fulfilled(_msg_db, message_id, body.fulfilled_by)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to mark fulfilled")
    return {"ok": True}


@app.get("/status")
def get_status():
    """Daemon health and counts. Used by PHP stream.php."""
    import os
    import time

    # Daemon running? Check lock file contains a live PID.
    daemon_running = False
    daemon_pid = None
    if _lock_path and _lock_path.exists():
        try:
            pid_str = _lock_path.read_text().strip()
            if pid_str:
                pid = int(pid_str)
                os.kill(pid, 0)   # raises OSError if dead
                daemon_running = True
                daemon_pid = pid
        except (ValueError, OSError):
            pass

    # Last cycle info
    last_cycle = None
    last_cycle_ts = None
    if _log_db is not None:
        cycles = _log().get_cycles(_log_db, limit=1)
        if cycles:
            last_cycle = cycles[0]
            last_cycle_ts = last_cycle.get("started_at")

    # Counts — always open fresh read connections (shared connections are not thread-safe)
    memory_count = 0
    try:
        import sqlite3 as _sq
        _mp = Path(__file__).parent / "memories.db"
        if _mp.exists():
            _c = _sq.connect(str(_mp))
            row = _c.execute("SELECT COUNT(*) FROM memories").fetchone()
            memory_count = row[0] if row else 0
            _c.close()
    except Exception:
        pass

    unread_count = 0
    try:
        import sqlite3 as _sq
        _msgp = Path(__file__).parent / "messages.db"
        if _msgp.exists():
            _c = _sq.connect(str(_msgp))
            row = _c.execute("SELECT COUNT(*) FROM messages WHERE direction='in' AND processed=0").fetchone()
            unread_count = row[0] if row else 0
            _c.close()
    except Exception:
        pass

    # Soul mtime
    soul_mtime = None
    if _soul_path and _soul_path.exists():
        soul_mtime = _soul_path.stat().st_mtime

    return {
        "daemon_running": daemon_running,
        "daemon_pid": daemon_pid,
        "last_cycle_ts": last_cycle_ts,
        "last_cycle_session": last_cycle.get("session_id", "") if last_cycle else "",
        "last_cycle_summary": last_cycle.get("summary", "") if last_cycle else "",
        "memory_count": memory_count,
        "unread_count": unread_count,
        "soul_mtime": soul_mtime,
    }


@app.get("/cycles")
def list_cycles(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, le=100),
    include_steps: bool = Query(False),
):
    """Paginated cycle list. Used by PHP cycles tab."""
    offset = (page - 1) * per_page
    cycles = _log().get_cycles(
        _log_db,
        limit=per_page,
        offset=offset,
        include_steps=include_steps,
    )
    total = _log().count_cycles(_log_db)
    return {"cycles": cycles, "total": total, "page": page, "per_page": per_page}


@app.get("/cycles/{cycle_id}/steps")
def get_cycle_steps(cycle_id: int):
    """Return all steps for a specific cycle."""
    steps = _log().get_steps(_log_db, cycle_id)
    return {"cycle_id": cycle_id, "steps": steps}


@app.get("/memories")
def list_memories(
    q: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, le=200),
):
    """Paginated/filterable memory list. Used by PHP memories tab."""
    offset = (page - 1) * per_page
    if _mem_db is None:
        return {"memories": [], "total": 0}
    try:
        clauses = []
        params: list = []
        if q:
            clauses.append("(content LIKE ? OR context LIKE ? OR tags LIKE ?)")
            pq = f"%{q}%"
            params += [pq, pq, pq]
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count_row = _mem_db.execute(
            f"SELECT COUNT(*) FROM memories {where}", params
        ).fetchone()
        total = count_row[0] if count_row else 0
        rows = _mem_db.execute(
            f"SELECT id, content, context, importance, tags, session_id, created_at, source "
            f"FROM memories {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
        cols = ("id", "content", "context", "importance", "tags", "session_id", "created_at", "source")
        memories = [dict(zip(cols, r)) for r in rows]
        return {"memories": memories, "total": total, "page": page, "per_page": per_page}
    except Exception as e:
        return {"memories": [], "total": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Factory function (for testing and programmatic use)
# ---------------------------------------------------------------------------

def create_app(cfg, wake_event: Optional[threading.Event] = None) -> FastAPI:
    """
    Create and configure the FastAPI app with the given config.
    
    cfg must have attributes:
      messages_db, memories_db, runtime_log_db, soul_path
    
    This wires up module-level state so the endpoints can find their DBs.
    Also sets the wake_event if provided.
    Returns the FastAPI app (ready for TestClient or uvicorn).
    """
    global _msg_db, _mem_db, _log_db, _cfg, _soul_path, _lock_path, _wake_event

    from offspring import messages as _messages_mod
    from offspring import runtime_log as _runtime_log_mod

    _cfg = cfg
    _wake_event = wake_event

    # Open DB connections
    _msg_db = _messages_mod.connect(cfg.messages_db)
    _log_db = _runtime_log_mod.connect(cfg.runtime_log_db)

    # memories_db — optional, open if exists
    try:
        import sqlite3 as _sq
        _mp = Path(cfg.memories_db)
        _mp.parent.mkdir(parents=True, exist_ok=True)
        _mem_db = _sq.connect(str(_mp), check_same_thread=False)
        # Create table if not present so /status doesn't throw
        _mem_db.execute(
            "CREATE TABLE IF NOT EXISTS memories "
            "(id INTEGER PRIMARY KEY, content TEXT, context TEXT, "
            "importance INTEGER, tags TEXT, session_id TEXT, created_at DATETIME, source TEXT)"
        )
        _mem_db.commit()
    except Exception:
        _mem_db = None

    # soul path
    _soul_path = Path(cfg.soul_path)

    # lock path — look for offspring.lock next to soul
    _lock_path = _soul_path.parent / "offspring.lock"

    return app


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def start_api_thread(host: str = "127.0.0.1", port: int = 7744) -> threading.Thread:
    """
    Start the FastAPI server in a background daemon thread.
    Returns the thread (already started).
    """
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)

    def _run():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, name="fen-api", daemon=True)
    t.start()
    return t
