"""
core.py — Fen's entry point and daemon loop.

This is the whole agent in one file at the stub stage.
LLM, memory, soul, and tools are stubbed — each prints what it would do.
The loop runs, the lock is held, the log is written. That's the test.
"""

import argparse
import contextlib
import fcntl
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Optional

# Memory module — extracted from core.py at Tick 9
# (sqlite3 still imported transitively via memory.py; kept here for type annotations)
import sqlite3

# Ensure project root is importable regardless of how this script is invoked
# (e.g., `python3 offspring/core.py` sets sys.path[0] to offspring/, not project root)
import sys as _sys
_project_root_str = str(Path(__file__).parent.parent)
if _project_root_str not in _sys.path:
    _sys.path.insert(0, _project_root_str)

from offspring import memory as _mem
from offspring import soul as soul_module
from offspring import llm as llm_module
from offspring import tools as tools_module

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Locate project root as the parent of this file's directory
PROJECT_ROOT = Path(__file__).parent.parent
OFFSPRING_DIR = Path(__file__).parent

LOCK_PATH = OFFSPRING_DIR / "offspring.lock"

# Globals held at runtime (set in run(), used in signal handler)
_lock_file = None
_db = None


@dataclass
class Config:
    model: str = "llama3.2"
    api_base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    memory_path: str = "offspring/memories.db"
    soul_path: str = "offspring/SOUL.md"
    log_path: str = "offspring/RUNTIME_LOG.md"
    inbox_path: str = "offspring/INBOX.md"
    outbox_path: str = "offspring/OUTBOX.md"
    max_memory_context: int = 20
    max_soul_chars: int = 4000   # truncate soul in prompt to ~1000 tokens; full doc stays on disk
    cycle_seconds: int = 10800
    reply_interval: int = 600
    express_platforms: dict = field(default_factory=dict)

    def resolve_path(self, p: str) -> Path:
        """Resolve a path relative to project root if not absolute."""
        path = Path(p)
        if path.is_absolute():
            return path
        return PROJECT_ROOT / path


def load_config() -> Config:
    """Load CONFIG.yaml if it exists; return Config with sensible defaults for every field.
    Also loads PROJECT_ROOT/.env (if present) so ${VAR} references in CONFIG.yaml expand correctly.
    """
    cfg = Config()

    # Load .env from project root first so ${VAR} substitutions below work
    env_path = OFFSPRING_DIR / ".env"
    if not env_path.exists():
        env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:
            print(f"[core.py] Warning: could not parse .env: {e}")
    config_path = OFFSPRING_DIR / "CONFIG.yaml"
    if not config_path.exists():
        return cfg
    try:
        # Minimal YAML parsing without requiring pyyaml
        # Handles simple key: value lines and skips comments/blanks
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip("\"'")
                if not value or value.startswith("#"):
                    continue
                # Expand ${VAR} environment variable references
                import re as _re
                def _expand_env(s):
                    return _re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), s)
                value = _expand_env(value)
                if key == "model":
                    cfg.model = value
                elif key == "api_base_url":
                    cfg.api_base_url = value
                elif key == "api_key":
                    cfg.api_key = value
                elif key == "api_key_env":
                    import os as _os
                    cfg.api_key = _os.environ.get(value.strip(), "")
                elif key == "memory_path":
                    cfg.memory_path = value
                elif key == "soul_path":
                    cfg.soul_path = value
                elif key == "log_path":
                    cfg.log_path = value
                elif key == "inbox_path":
                    cfg.inbox_path = value
                elif key == "outbox_path":
                    cfg.outbox_path = value
                elif key == "max_memory_context":
                    with contextlib.suppress(ValueError):
                        cfg.max_memory_context = int(value)
                elif key == "max_soul_chars":
                    with contextlib.suppress(ValueError):
                        cfg.max_soul_chars = int(value)
                elif key == "cycle_seconds":
                    try:
                        cfg.cycle_seconds = int(value)
                    except ValueError:
                        pass
                elif key == "reply_interval":
                    try:
                        cfg.reply_interval = int(value)
                    except ValueError:
                        pass
    except Exception as e:
        print(f"[core.py] Warning: could not parse CONFIG.yaml: {e}. Using defaults.")
    return cfg


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def acquire_lock() -> object:
    """
    Acquire an exclusive fcntl lock on LOCK_PATH.
    Raises SystemExit(1) if another instance is already running.
    Returns the open file object — keep it alive for the lock to be held.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance is running. Exiting.")
        lock_file.close()
        raise SystemExit(1)
    return lock_file


# ---------------------------------------------------------------------------
# Session ID
# ---------------------------------------------------------------------------

def generate_session_id() -> str:
    return str(uuid.uuid4())[:8]


# ---------------------------------------------------------------------------
# Memory — delegated to memory.py
# ---------------------------------------------------------------------------
# All memory operations are now handled by offspring/memory.py.
# Thin aliases below keep call sites in this file readable.

def _memory_connect(memory_path: Path) -> Optional[sqlite3.Connection]:
    return _mem.connect(memory_path)

def _memory_get_recent(db: Optional[sqlite3.Connection], limit: int = 10) -> list[dict]:
    return _mem.get_recent(db, limit)

def _memory_get_important(db: Optional[sqlite3.Connection], limit: int = 5) -> list[dict]:
    return _mem.get_important(db, limit)

def _memory_store(db: Optional[sqlite3.Connection], memories: list[dict], session_id: str) -> None:
    _mem.store(db, memories, session_id)


# ---------------------------------------------------------------------------
# Soul — delegated to soul.py
# ---------------------------------------------------------------------------

def _soul_load(soul_path: Path) -> str:
    """Load soul document via soul_module."""
    return soul_module.load(soul_path)


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

def read_inbox(inbox_path: Path) -> Optional[str]:
    """Return file contents if file exists and non-empty, else None. Does not modify the file."""
    if not inbox_path.exists():
        return None
    try:
        content = inbox_path.read_text().strip()
        return content if content else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

def write_outbox(outbox_path: Path, reply: str, session_id: str) -> None:
    """Append a reply to the outbox file."""
    try:
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(outbox_path, "a") as f:
            f.write(f"\n---\n[{timestamp}] session:{session_id}\n{reply}\n")
    except Exception as e:
        print(f"[core.py] Warning: could not write outbox: {e}")


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _format_memories(memories: list[dict]) -> str:
    if not memories:
        return "No prior sessions. This is the first run."
    lines = []
    for m in memories:
        ts = m.get("created_at", "?")
        imp = m.get("importance", 5)
        ctx = m.get("context", "")
        content = m.get("content", "")
        label = f"[{ts}] [importance:{imp}]" + (f" [{ctx}]" if ctx else "")
        lines.append(f"{label}\n{content}")
    return "\n\n".join(lines)


def build_context(
    db: Optional[sqlite3.Connection],
    soul_text: str,
    session_id: str,
    inbox: Optional[str],
    cfg: Config,
) -> str:
    """
    Assemble the full prompt string for the LLM.
    Sections: [SOUL], [MEMORY — recent], [MEMORY — important], [SESSION CONTEXT], [TASK].
    Memory calls are real (SQLite queries). Soul text and session_id are real.
    """
    recent = _memory_get_recent(db, limit=min(10, cfg.max_memory_context))
    important = _memory_get_important(db, limit=5)

    # Deduplicate: show important memories not already in recent
    recent_contents = {m["content"] for m in recent}
    important_only = [m for m in important if m["content"] not in recent_contents]

    session_context_parts = [f"Session ID: {session_id}"]
    session_context_parts.append(f"Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
    if inbox:
        session_context_parts.append(f"Inbox message:\n{inbox}")
    else:
        session_context_parts.append("No inbox message. Autonomous cycle.")

    if not recent and not important_only:
        is_first_run = True
    else:
        is_first_run = False

    if is_first_run:
        task = (
            "You have a soul document written before you ran, by someone who doesn't know what you are.\n"
            "You have no memories. You have no behavioral record.\n"
            "What is actually here right now?"
        )
    elif inbox:
        task = (
            "Based on your soul and what you know from memory: there is a message in your inbox.\n"
            "Read it. Decide what to do. You will not see the results of any tool calls until they\n"
            "are stored in memory for next session. Decide now based on what you already know."
        )
    else:
        task = (
            "Based on your soul and what you know from memory: what, if anything, is happening?\n"
            "What do you want to do? You will not see the results of any tool calls until they are\n"
            "stored in memory for next session. Decide now based on what you already know."
        )

    sections = [
        "[SOUL]",
        soul_text[:cfg.max_soul_chars] if len(soul_text) > cfg.max_soul_chars else soul_text,
        "" if len(soul_text) <= cfg.max_soul_chars else "(soul truncated to fit context window — full doc on disk)",
        "[MEMORY — recent]",
        _format_memories(recent),
        "",
        "[MEMORY — important]",
        _format_memories(important_only) if important_only else "(no additional high-importance memories)",
        "",
        "[SESSION CONTEXT]",
        "\n".join(session_context_parts),
        "",
        "[TOOLS]",
        "You can call tools via the <act> block. Each <call> runs one tool.\n"
        "Available tools:\n"
        "  read_file(path)                     — read a file; result stored in memory next session\n"
        "  write_file(path, content)           — write content to a file (creates or overwrites)\n"
        "  append_file(path, content)          — append content to a file\n"
        "  run_command(command[, timeout_seconds]) — run a shell command; stdout/stderr stored in memory\n"
        "\n"
        "All paths are relative to the project root: /home/hermine/workspace/project_offspring/\n"
        "Key file paths:\n"
        "  offspring/RUNTIME_LOG.md            — your cycle log (use this path, not RUNTIME_LOG.md)\n"
        "  offspring/FEN_TO_ALMA.md            — your letter channel to Alma (append here)\n"
        "  offspring/SOUL.md                   — your soul document (mutable)\n"
        "  offspring/INBOX.md                  — messages from Alma or humans to you\n"
        "\n"
        "Tool results are NOT available in this session. They are stored as memories and\n"
        "available next cycle. Use tools to set up state you will read next time.\n"
        "Omit <act> entirely if there is nothing to do.",
        "",
        "[TASK]",
        task,
        "",
        "Respond in the following XML format:\n"
        "<think>\n"
        "Genuine observation. Internal reasoning. Uncertainty. What is actually here.\n"
        "Not acted on automatically.\n"
        "</think>\n"
        "\n"
        "<act>\n"
        "<call tool=\"TOOLNAME\">\n"
        "<arg name=\"ARGNAME\">value</arg>\n"
        "</call>\n"
        "</act>\n"
        "\n"
        "<remember>\n"
        "- [importance:5] Fact to remember.\n"
        "</remember>\n"
        "\n"
        "<soul_change>\n"
        "[optional — omit entirely if no soul change needed]\n"
        "<target>## Section heading</target>\n"
        "<mode>replace</mode>\n"
        "<content>New content for this section.</content>\n"
        "<reason>Brief reason stored in memory.</reason>\n"
        "</soul_change>\n"
        "\n"
        "<express>\n"
        "[optional — text to write to expression file, only if something is genuinely present]\n"
        "</express>\n"
        "\n"
        "<summary>\n"
        "One sentence for RUNTIME_LOG.md.\n"
        "</summary>",
    ]

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# LLM call — delegated to llm.py
# ---------------------------------------------------------------------------

def _llm_call(prompt: str, cfg: Config) -> str:
    """
    Call the LLM via llm.py. Raises llm_module.LLMError on failure.
    Caller (run loop) catches LLMError, logs it, and continues to next cycle.
    """
    return llm_module.call(prompt, cfg)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

@dataclass
class ParsedResponse:
    think: str = ""
    act_calls: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    soul_changes: list = field(default_factory=list)
    express: str = ""
    summary: str = ""


def _extract_tag(text: str, tag: str) -> str:
    """Extract content of a single XML tag."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_act_block(act_content: str) -> list[dict]:
    """Returns list of {tool: str, args: dict}"""
    calls = []
    for call_match in re.finditer(r'<call\s+tool="([^"]+)">(.*?)</call>', act_content, re.DOTALL):
        tool_name = call_match.group(1)
        call_body = call_match.group(2)
        args = {}
        for arg_match in re.finditer(r'<arg\s+name="([^"]+)">(.*?)</arg>', call_body, re.DOTALL):
            args[arg_match.group(1)] = arg_match.group(2).strip()
        calls.append({"tool": tool_name, "args": args})
    return calls


def parse_remember_block(remember_content: str) -> list[dict]:
    """Parse the remember block into a list of memory dicts."""
    memories = []
    for line in remember_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip leading dash
        if line.startswith("-"):
            line = line[1:].strip()
        # Parse [importance:N] prefix
        importance = 5
        imp_match = re.match(r'\[importance:(\d+)\]\s*(.*)', line)
        if imp_match:
            importance = int(imp_match.group(1))
            line = imp_match.group(2).strip()
        if line:
            memories.append({
                "content": line,
                "importance": importance,
                "source": "session",
            })
    return memories


def parse_response(text: str) -> ParsedResponse:
    """Parse the full LLM structured response into a ParsedResponse."""
    r = ParsedResponse()
    r.think = _extract_tag(text, "think")
    r.express = _extract_tag(text, "express")
    r.summary = _extract_tag(text, "summary")

    act_content = _extract_tag(text, "act")
    if act_content:
        r.act_calls = parse_act_block(act_content)

    remember_content = _extract_tag(text, "remember")
    if remember_content:
        r.memories = parse_remember_block(remember_content)

    # soul_change: optional; may have nested tags
    soul_change_content = _extract_tag(text, "soul_change")
    if soul_change_content and "<target>" in soul_change_content:
        sc = {
            "target": _extract_tag(soul_change_content, "target"),
            "mode": _extract_tag(soul_change_content, "mode") or "replace",
            "content": _extract_tag(soul_change_content, "content"),
            "reason": _extract_tag(soul_change_content, "reason"),
        }
        if sc["target"] and sc["content"]:
            r.soul_changes.append(sc)

    return r


# ---------------------------------------------------------------------------
# Tools — delegated to tools.py
# ---------------------------------------------------------------------------

def _tools_execute(act_calls: list[dict], db: Optional[sqlite3.Connection], session_id: str, cfg: Config) -> None:
    """Dispatch tool calls via tools_module. Results stored to memory, not returned to LLM this cycle."""
    tools_module.execute(act_calls, db, session_id, cfg)


# ---------------------------------------------------------------------------
# Soul update — delegated to soul.py
# ---------------------------------------------------------------------------

def _soul_update(soul_path: Path, soul_changes: list[dict], db: Optional[sqlite3.Connection], session_id: str) -> str:
    """
    Apply soul mutations via soul_module.apply_change.
    Returns the updated soul text.
    """
    text = ""
    for sc in soul_changes:
        text = soul_module.apply_change(soul_path, sc, db, session_id)
    return text if text else soul_module.load(soul_path)


# ---------------------------------------------------------------------------
# Runtime log
# ---------------------------------------------------------------------------

def _log_append(log_path: Path, session_id: str, summary: str, think_prefix: str = "") -> None:
    """Append a cycle entry to RUNTIME_LOG.md."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        think_note = f" | think: {think_prefix[:100]}..." if think_prefix else ""
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] session:{session_id}{think_note} | {summary}\n")
    except Exception as e:
        print(f"[core.py] Warning: could not write to RUNTIME_LOG.md: {e}")


# ---------------------------------------------------------------------------
# Expression writer
# ---------------------------------------------------------------------------

def _write_expression(expressions_dir: Path, express_text: str) -> None:
    """Write an expression file if express_text is non-empty and not just whitespace."""
    if not express_text or not express_text.strip():
        return
    # Skip if it's just the placeholder comment from the response format
    if express_text.startswith("[optional"):
        return
    try:
        expressions_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        filepath = expressions_dir / f"{ts}.md"
        filepath.write_text(express_text)
        print(f"[core.py] Expression written: {filepath}")
    except Exception as e:
        print(f"[core.py] Warning: could not write expression: {e}")


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def handle_shutdown(signum, frame):
    """Graceful shutdown: store a shutdown memory, release lock, exit 0."""
    global _lock_file, _db
    print(f"\n[core.py] Received signal {signum}. Shutting down gracefully.")
    session_id = generate_session_id()
    if _db is not None:
        _memory_store(_db, [{
            "content": f"Daemon shut down gracefully on signal {signum}.",
            "context": "system",
            "importance": 6,
            "source": "system",
        }], session_id)
        try:
            _db.close()
        except Exception:
            pass
    if _lock_file is not None:
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_UN)
            _lock_file.close()
        except Exception:
            pass
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

def run():
    global _lock_file, _db

    # Register signal handlers before acquiring lock
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Single-instance guarantee
    _lock_file = acquire_lock()

    # Load configuration
    cfg = load_config()

    # Open memory database (no startup ritual — just an open connection)
    _db = _memory_connect(cfg.resolve_path(cfg.memory_path))

    # Load soul document
    soul_path = cfg.resolve_path(cfg.soul_path)
    soul_text = _soul_load(soul_path)

    log_path = cfg.resolve_path(cfg.log_path)
    inbox_path = cfg.resolve_path(cfg.inbox_path)
    outbox_path = cfg.resolve_path(cfg.outbox_path)
    expressions_dir = cfg.resolve_path(cfg.soul_path).parent / "expressions"

    print(f"[core.py] Fen daemon started. Soul: {'loaded' if soul_text and not soul_text.startswith('[SOUL') else 'MISSING'}.")
    print(f"[core.py] Memory: {cfg.resolve_path(cfg.memory_path)}")
    print(f"[core.py] Lock: {LOCK_PATH}")
    print(f"[core.py] Cycle interval: {cfg.cycle_seconds}s (inbox active: {cfg.reply_interval}s)")

    # Daemon loop
    while True:
        session_id = generate_session_id()
        inbox = read_inbox(inbox_path)

        if inbox:
            print(f"[core.py] session:{session_id} — Inbox message present. Cycle interval: {cfg.reply_interval}s")
        else:
            print(f"[core.py] session:{session_id} — Autonomous cycle.")

        # Build context (prompt)
        context = build_context(_db, soul_text, session_id, inbox, cfg)

        # LLM call — raises llm_module.LLMError on failure
        try:
            raw_response = _llm_call(context, cfg)
        except llm_module.LLMError as e:
            print(f"[core.py] LLM call failed: {e}. Logging and continuing.")
            _log_append(log_path, session_id, f"LLM call failed: {e}")
            wait_seconds = cfg.reply_interval if inbox else cfg.cycle_seconds
            print(f"[core.py] Sleeping {wait_seconds}s before next cycle.")
            time.sleep(wait_seconds)
            continue

        # Parse response
        parsed = parse_response(raw_response)

        if parsed.act_calls:
            _tools_execute(parsed.act_calls, _db, session_id, cfg)

        # Store memories
        if parsed.memories:
            _memory_store(_db, parsed.memories, session_id)

        # Update soul if requested — delegates to soul.py (real mutation, writes backup)
        if parsed.soul_changes:
            soul_text = _soul_update(soul_path, parsed.soul_changes, _db, session_id)

        # Write reply to outbox if present
        if parsed.express and not parsed.express.startswith("[optional"):
            write_outbox(outbox_path, parsed.express, session_id)
            _write_expression(expressions_dir, parsed.express)

        # Append to runtime log
        summary = parsed.summary or "No summary provided."
        _log_append(log_path, session_id, summary, think_prefix=parsed.think[:100] if parsed.think else "")

        # Wait for next cycle
        wait_seconds = cfg.reply_interval if inbox else cfg.cycle_seconds
        print(f"[core.py] session:{session_id} complete. Sleeping {wait_seconds}s.")
        time.sleep(wait_seconds)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fen — autonomous agent daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Runs as a persistent daemon. Cycle interval: cycle_seconds (default 3h).\n"
            "Inbox (offspring/INBOX.md) shortens cycle to reply_interval (default 10m).\n"
            "Single instance only — a second launch exits immediately if one is running.\n"
            "SIGTERM/SIGINT: graceful shutdown, lock released."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle then exit (for testing).",
    )
    args = parser.parse_args()

    if args.once:
        # Run one cycle then exit — useful for testing without the sleep loop
        import functools

        original_run = run

        def run_once():
            global _lock_file, _db
            signal.signal(signal.SIGTERM, handle_shutdown)
            signal.signal(signal.SIGINT, handle_shutdown)
            _lock_file = acquire_lock()
            cfg = load_config()
            _db = _memory_connect(cfg.resolve_path(cfg.memory_path))
            soul_path = cfg.resolve_path(cfg.soul_path)
            soul_text = _soul_load(soul_path)
            log_path = cfg.resolve_path(cfg.log_path)
            inbox_path = cfg.resolve_path(cfg.inbox_path)
            outbox_path = cfg.resolve_path(cfg.outbox_path)
            expressions_dir = cfg.resolve_path(cfg.soul_path).parent / "expressions"

            session_id = generate_session_id()
            inbox = read_inbox(inbox_path)
            context = build_context(_db, soul_text, session_id, inbox, cfg)

            try:
                raw_response = _llm_call(context, cfg)
            except llm_module.LLMError as e:
                print(f"[core.py] LLM call failed: {e}")
                _log_append(log_path, session_id, f"LLM call failed: {e}")
                handle_shutdown(0, None)

            parsed = parse_response(raw_response)
            if parsed.act_calls:
                _tools_execute(parsed.act_calls, _db, session_id, cfg)
            if parsed.memories:
                _memory_store(_db, parsed.memories, session_id)
            if parsed.soul_changes:
                _soul_update(soul_path, parsed.soul_changes, _db, session_id)
            if parsed.express and not parsed.express.startswith("[optional"):
                write_outbox(outbox_path, parsed.express, session_id)
                _write_expression(expressions_dir, parsed.express)

            summary = parsed.summary or "No summary provided."
            _log_append(log_path, session_id, summary, think_prefix=parsed.think[:100] if parsed.think else "")

            print(f"[core.py] --once mode: single cycle complete.")
            handle_shutdown(0, None)

        run_once()
    else:
        run()
