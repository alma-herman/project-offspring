"""
test_live_cycle.py — Live end-to-end test for the Fen autonomous agent daemon.

Run from project root:
    .venv/bin/python3 test_live_cycle.py

Exits 0 on success, 1 on any assertion failure.
"""

import subprocess
import sqlite3
import sys
import re
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"
CORE = PROJECT_ROOT / "offspring" / "core.py"
RUNTIME_LOG = PROJECT_ROOT / "offspring" / "RUNTIME_LOG.md"
MEMORIES_DB = PROJECT_ROOT / "offspring" / "memories.db"

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def redact_tokens(text: str) -> str:
    """Remove API tokens (gho_, sk-, ghs_, etc.) from text."""
    text = re.sub(r'gho_[A-Za-z0-9]+', 'gho_REDACTED', text)
    text = re.sub(r'sk-[A-Za-z0-9]+', 'sk-REDACTED', text)
    text = re.sub(r'ghs_[A-Za-z0-9]+', 'ghs_REDACTED', text)
    return text


def fail(reason: str) -> None:
    print(f"LIVE CYCLE TEST FAILED: {reason}")
    sys.exit(1)


def main():
    # ── 1. Run offspring/core.py --once ──────────────────────────────────────
    print(f"[test] Running: {PYTHON} {CORE} --once")
    result = subprocess.run(
        [str(PYTHON), str(CORE), "--once"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined_output = result.stdout + result.stderr

    # ── 2. Assert exit 0 ─────────────────────────────────────────────────────
    if result.returncode != 0:
        print(f"[test] stdout:\n{redact_tokens(result.stdout[-2000:])}")
        print(f"[test] stderr:\n{redact_tokens(result.stderr[-2000:])}")
        fail(f"core.py --once exited with code {result.returncode}")

    # ── 3. Assert RUNTIME_LOG.md has today's date ─────────────────────────────
    if not RUNTIME_LOG.exists():
        fail(f"RUNTIME_LOG.md does not exist at {RUNTIME_LOG}")

    log_content = RUNTIME_LOG.read_text()
    if TODAY not in log_content:
        last_lines = log_content.strip().splitlines()[-5:]
        fail(
            f"RUNTIME_LOG.md has no entry for today ({TODAY}). "
            f"Last lines: {last_lines}"
        )

    last_log_line = ""
    for line in reversed(log_content.splitlines()):
        if line.strip():
            last_log_line = line.strip()
            break

    # ── 4. Assert memories.db exists and has ≥1 row ───────────────────────────
    if not MEMORIES_DB.exists():
        fail(f"memories.db does not exist at {MEMORIES_DB}")

    try:
        conn = sqlite3.connect(str(MEMORIES_DB))
        cursor = conn.execute("SELECT COUNT(*) FROM memories")
        memory_count = cursor.fetchone()[0]
        conn.close()
    except sqlite3.Error as e:
        fail(f"Could not query memories.db: {e}")

    if memory_count < 1:
        fail(f"memories table has {memory_count} rows — expected at least 1")

    # ── 5. Print summary ──────────────────────────────────────────────────────
    stdout_preview = redact_tokens(combined_output[:200])

    print()
    print("─" * 60)
    print("LIVE CYCLE SUMMARY")
    print("─" * 60)
    print(f"  Exit code      : {result.returncode}")
    print(f"  Memory rows    : {memory_count}")
    print(f"  Last log line  : {last_log_line}")
    print(f"  Stdout preview : {repr(stdout_preview)}")
    print("─" * 60)
    print()
    print("LIVE CYCLE TEST PASSED")


if __name__ == "__main__":
    main()
