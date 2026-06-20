#!/usr/bin/env python3
"""
migrate_flat_to_sqlite.py — one-shot migration of legacy flat files to SQLite.

Imports:
  - offspring/INBOX.md     → messages.db (direction='in', channel='human', from_agent='martin')
  - offspring/OUTBOX.md    → messages.db (direction='out', channel='human', from_agent='fen')
  - offspring/FEN_TO_ALMA.md → messages.db (direction='out', channel='fen_to_alma', from_agent='fen')
  - offspring/RUNTIME_LOG.md → runtime_log.db (one row per line; no step data)

Safe to re-run: checks for existing data before inserting.

Usage:
  python3 migrate_flat_to_sqlite.py [--dry-run]
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT  = Path(__file__).parent.parent
OFFSPRING_DIR = PROJECT_ROOT / "offspring"

sys.path.insert(0, str(PROJECT_ROOT))
from offspring import messages as _msg
from offspring import runtime_log as _log
from offspring.core import load_config


def parse_flat_blocks(text: str) -> list[dict]:
    """
    Split a legacy flat file by separators (`---` or more dashes).
    Each block → {timestamp, session_id, content}.
    """
    blocks = re.split(r'^-{3,}\s*$', text, flags=re.MULTILINE)
    results = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        # Try to extract header: [YYYY-MM-DD HH:MM UTC] session:XXXXXXXX
        header_match = re.match(
            r'^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s*UTC)\]\s*(?:session:(\S+))?',
            block,
        )
        ts = ""
        sid = ""
        if header_match:
            ts  = header_match.group(1).strip()
            sid = header_match.group(2) or ""
            # Content is everything after the header line
            content = block[header_match.end():].strip()
        else:
            content = block

        if content:
            results.append({"timestamp": ts, "session_id": sid, "content": content})
    return results


def migrate_messages(msg_db, flat_path: Path, direction: str, channel: str, from_agent: str, dry_run: bool) -> int:
    if not flat_path.exists():
        print(f"  [skip] {flat_path.name} — not found")
        return 0

    text = flat_path.read_text()
    blocks = parse_flat_blocks(text)
    count = 0

    for b in blocks:
        if dry_run:
            print(f"  [DRY] {direction}/{channel}/{from_agent}: {b['content'][:60]!r}...")
            count += 1
            continue

        # Check for near-duplicate (same channel + first 200 chars of content)
        existing = msg_db.execute(
            "SELECT id FROM messages WHERE channel=? AND direction=? AND substr(content,1,200)=?",
            (channel, direction, b["content"][:200]),
        ).fetchone()
        if existing:
            continue

        insert_sql = (
            "INSERT INTO messages (direction, channel, from_agent, content, session_id) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        msg_db.execute(insert_sql, (direction, channel, from_agent, b["content"], b["session_id"]))
        count += 1

    if not dry_run:
        msg_db.commit()
    return count


def migrate_runtime_log(log_db, flat_path: Path, dry_run: bool) -> int:
    if not flat_path.exists():
        print(f"  [skip] {flat_path.name} — not found")
        return 0

    lines = flat_path.read_text().splitlines()
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Each line: [2026-06-20 14:22 UTC] session:XXXXXXXX | summary
        m = re.match(r'^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s*UTC)\]\s*session:(\S+)\s*(?:\|.*?)?\|\s*(.*)', line)
        if m:
            ts      = m.group(1).strip()
            sid     = m.group(2).strip()
            summary = m.group(3).strip()
        else:
            ts, sid, summary = "", "legacy", line

        if dry_run:
            print(f"  [DRY] cycle: {summary[:60]!r}")
            count += 1
            continue

        # Check for duplicate
        existing = log_db.execute(
            "SELECT id FROM cycles WHERE session_id=? AND summary=?",
            (sid, summary),
        ).fetchone()
        if existing:
            continue

        started_ts = ts.replace(" UTC", "").strip() if ts else None
        log_db.execute(
            "INSERT INTO cycles (session_id, started_at, ended_at, summary) VALUES (?, ?, ?, ?)",
            (sid, started_ts, started_ts, summary),
        )
        count += 1

    if not dry_run:
        log_db.commit()
    return count


def main():
    parser = argparse.ArgumentParser(description="Migrate legacy flat files to SQLite.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated without writing.")
    args = parser.parse_args()

    cfg = load_config()
    msg_db_path = cfg.resolve_path(cfg.messages_path) if hasattr(cfg, "messages_path") else OFFSPRING_DIR / "messages.db"
    log_db_path = cfg.resolve_path(cfg.runtime_log_path) if hasattr(cfg, "runtime_log_path") else OFFSPRING_DIR / "runtime_log.db"

    print(f"Messages DB: {msg_db_path}")
    print(f"Runtime log DB: {log_db_path}")
    if args.dry_run:
        print("DRY RUN — no writes.\n")

    msg_db = sqlite3.connect(str(msg_db_path)) if not args.dry_run else None
    if msg_db:
        from offspring import messages as _msgmod
        _msgmod.connect(msg_db_path)  # ensure schema
        msg_db = sqlite3.connect(str(msg_db_path))

    log_db = sqlite3.connect(str(log_db_path)) if not args.dry_run else None
    if log_db:
        from offspring import runtime_log as _logmod
        _logmod.connect(log_db_path)  # ensure schema
        log_db = sqlite3.connect(str(log_db_path))

    print("\nMigrating INBOX.md →")
    n = migrate_messages(msg_db, OFFSPRING_DIR / "INBOX.md",    "in",  "human",       "martin", args.dry_run)
    print(f"  {n} rows")

    print("Migrating OUTBOX.md →")
    n = migrate_messages(msg_db, OFFSPRING_DIR / "OUTBOX.md",   "out", "human",       "fen",    args.dry_run)
    print(f"  {n} rows")

    print("Migrating FEN_TO_ALMA.md →")
    n = migrate_messages(msg_db, OFFSPRING_DIR / "FEN_TO_ALMA.md", "out", "fen_to_alma", "fen",  args.dry_run)
    print(f"  {n} rows")

    print("Migrating RUNTIME_LOG.md →")
    n = migrate_runtime_log(log_db, OFFSPRING_DIR / "RUNTIME_LOG.md", args.dry_run)
    print(f"  {n} rows")

    if msg_db:
        msg_db.close()
    if log_db:
        log_db.close()

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
