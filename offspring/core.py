"""
core.py — Fen's daemon loop (v2).

Changes from v1:
  - Messages: INBOX.md / OUTBOX.md / FEN_TO_ALMA.md replaced by messages.db
    via the messages module. Flat files still kept as fallback/migration path.
  - Runtime log: RUNTIME_LOG.md replaced by runtime_log.db via runtime_log module.
  - FastAPI service: spun up in a background thread on localhost:7744 at startup.
  - Multi-step cycle: one cycle = an agentic inner loop.
    Fen calls a tool, sees the result, decides next action, repeats.
    Cycle ends when Fen emits <done/> or hits MAX_STEPS (default 10).
  - Dreaming: Fen can emit <dream>true</dream> at the end of a cycle.
    The daemon then runs a dedicated dreaming LLM pass that re-evaluates,
    merges, and prunes memories. Rate-limited by min_cycles_between_dreams.
"""

import argparse
import contextlib
import fcntl
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sqlite3

import sys as _sys
_project_root_str = str(Path(__file__).parent.parent)
if _project_root_str not in _sys.path:
    _sys.path.insert(0, _project_root_str)

from offspring import memory as _mem
from offspring import messages as _msg
from offspring import runtime_log as _log
from offspring import soul as soul_module
from offspring import llm as llm_module
from offspring import tools as tools_module
from offspring import api as api_module

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_STEPS = 10          # agentic loop hard ceiling per cycle
MAX_STEP_TOKENS = 12000  # approx chars per accumulated step context before truncation

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT  = Path(__file__).parent.parent
OFFSPRING_DIR = Path(__file__).parent
LOCK_PATH     = OFFSPRING_DIR / "offspring.lock"

# ---------------------------------------------------------------------------
# Global state (set at daemon startup; used by signal handlers)
# ---------------------------------------------------------------------------

_lock_file  = None
_mem_db     = None
_msg_db     = None
_log_db     = None
_wake_event = threading.Event()


def _handle_wakeup(signum, frame):
    print("[core.py] SIGUSR1 — waking early.")
    _wake_event.set()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    model: str = "llama3.2"
    api_base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    memory_path: str = "offspring/memories.db"
    messages_path: str = "offspring/messages.db"
    runtime_log_path: str = "offspring/runtime_log.db"
    soul_path: str = "offspring/SOUL.md"
    # Legacy flat files kept for migration read; writes use SQLite only.
    log_path: str = "offspring/RUNTIME_LOG.md"
    max_memory_context: int = 20
    max_soul_chars: int = 14000
    cycle_seconds: int = 300
    reply_interval: int = 60
    max_cycles_retained: int = 500
    min_cycles_between_dreams: int = 12   # rate-limit dreaming
    api_port: int = 7744
    express_platforms: dict = field(default_factory=dict)

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else PROJECT_ROOT / path


def load_config() -> Config:
    cfg = Config()

    # Load .env
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
                    k = k.strip(); v = v.strip().strip("\"'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as e:
            print(f"[core.py] Warning: could not parse .env: {e}")

    config_path = OFFSPRING_DIR / "CONFIG.yaml"
    if not config_path.exists():
        return cfg
    try:
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip(); value = value.strip().strip("\"'")
                if "#" in value:
                    value = value[:value.index("#")].strip()
                if not value or value.startswith("#"):
                    continue
                # Expand ${VAR}
                value = re.sub(
                    r'\$\{([^}]+)\}',
                    lambda m: os.environ.get(m.group(1), m.group(0)),
                    value,
                )
                int_fields = {
                    "max_memory_context", "max_soul_chars", "cycle_seconds",
                    "reply_interval", "max_cycles_retained",
                    "min_cycles_between_dreams", "api_port",
                }
                str_fields = {
                    "model", "api_base_url", "api_key", "memory_path",
                    "messages_path", "runtime_log_path", "soul_path",
                    "log_path",
                }
                env_fields = {"api_key_env"}
                if key in str_fields:
                    setattr(cfg, key, value)
                elif key in int_fields:
                    with contextlib.suppress(ValueError):
                        setattr(cfg, key, int(value))
                elif key in env_fields:
                    cfg.api_key = os.environ.get(value.strip(), "")
    except Exception as e:
        print(f"[core.py] Warning: could not parse CONFIG.yaml: {e}")
    return cfg


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance is running. Exiting.")
        lf.close()
        raise SystemExit(1)
    return lf


def generate_session_id() -> str:
    return str(uuid.uuid4())[:8]


# ---------------------------------------------------------------------------
# Interruptible sleep
# ---------------------------------------------------------------------------

def _interruptible_sleep(seconds: float, check_interval: float = 5.0) -> None:
    """
    Sleep for up to `seconds`, returning early on SIGUSR1 or a new inbound message.
    Uses the module-level _wake_event; api.py sets it when POST /messages is called.
    """
    deadline = time.monotonic() + seconds
    _wake_event.clear()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        woken = _wake_event.wait(timeout=min(check_interval, remaining))
        if woken:
            _wake_event.clear()
            return


# ---------------------------------------------------------------------------
# Soul
# ---------------------------------------------------------------------------

def _soul_load(soul_path: Path) -> str:
    return soul_module.load(soul_path)


def _soul_update(soul_path: Path, soul_changes: list, db, session_id: str) -> str:
    text = ""
    for sc in soul_changes:
        text = soul_module.apply_change(soul_path, sc, db, session_id)
    return text if text else soul_module.load(soul_path)


# ---------------------------------------------------------------------------
# Memory helpers (thin wrappers kept for readability)
# ---------------------------------------------------------------------------

def _fmt_memories(memories: list) -> str:
    if not memories:
        return "None."
    lines = []
    for m in memories:
        ts = m.get("created_at", "?")
        imp = m.get("importance", 5)
        ctx = m.get("context", "")
        content = m.get("content", "")
        label = f"[{ts}] [imp:{imp}]" + (f" [{ctx}]" if ctx else "")
        lines.append(f"{label}\n{content}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Parsed response
# ---------------------------------------------------------------------------

@dataclass
class ParsedResponse:
    think: str = ""
    act_calls: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    soul_changes: list = field(default_factory=list)
    express: str = ""
    channel: str = "human"        # which channel the express goes to
    summary: str = ""
    done: bool = False            # <done/> signals end of agentic loop
    dream: bool = False           # <dream>true</dream> triggers dreaming


def _xtag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _parse_act(act_content: str) -> list:
    calls = []
    for cm in re.finditer(r'<call\s+tool="([^"]+)">(.*?)</call>', act_content, re.DOTALL):
        tool_name = cm.group(1)
        args = {}
        for am in re.finditer(r'<arg\s+name="([^"]+)">(.*?)</arg>', cm.group(2), re.DOTALL):
            args[am.group(1)] = am.group(2).strip()
        calls.append({"tool": tool_name, "args": args})
    return calls


def _parse_remember(rc: str) -> list:
    mems = []
    for line in rc.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        imp = 5
        m = re.match(r'\[importance:(\d+)\]\s*(.*)', line)
        if m:
            imp = int(m.group(1))
            line = m.group(2).strip()
        if line:
            mems.append({"content": line, "importance": imp, "source": "session"})
    return mems


def _parse_response(text: str) -> ParsedResponse:
    r = ParsedResponse()
    r.think   = _xtag(text, "think")
    r.express = _xtag(text, "express")
    r.channel = _xtag(text, "channel") or "human"
    r.summary = _xtag(text, "summary")

    # <done/> or <done>true</done>
    r.done  = bool(re.search(r'<done\s*/>', text)) or _xtag(text, "done").lower() == "true"
    # <dream>true</dream>
    r.dream = _xtag(text, "dream").lower() == "true"

    act = _xtag(text, "act")
    if act:
        r.act_calls = _parse_act(act)
    rem = _xtag(text, "remember")
    if rem:
        r.memories = _parse_remember(rem)
    sc_raw = _xtag(text, "soul_change")
    if sc_raw and "<target>" in sc_raw:
        sc = {
            "target":  _xtag(sc_raw, "target"),
            "mode":    _xtag(sc_raw, "mode") or "replace",
            "content": _xtag(sc_raw, "content"),
            "reason":  _xtag(sc_raw, "reason"),
        }
        if sc["target"] and sc["content"]:
            r.soul_changes.append(sc)
    return r


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_TOOL_HELP = """\
[TOOLS]
Call tools via <act>…</act>. Each <call> is one tool invocation.
You WILL see the result in the same cycle (step response). Iterate freely.

Available tools:
  read_file(path)                         — read a file
  write_file(path, content)               — write/overwrite a file
  append_file(path, content)              — append to a file
  run_command(command[, timeout_seconds]) — run a shell command
  express(text)                           — save expression to offspring/expressions/
  commit_snapshot(message)                — commit source to git; returns SHA
  restart_self(reason)                    — restart fen.service (new code loads)
  request_rollback(reason[, target])      — write rollback request for Alma
  send_message(channel, content)          — send message: channel='human' (Martin), 'alma', or 'fen_to_alma'
  send_email(to, subject, body)           — send email directly from fen09123@web-library.net
  bluesky_post(text)                      — post to Bluesky (requires FEN_BLUESKY credentials in offspring/.env)
  bluesky_timeline(limit=10)             — read recent Bluesky timeline
  check_email()                          — list Fen's email inbox (credentials from offspring/.env)

Notes:
  - Bluesky tools require FEN_BLUESKY_HANDLE and FEN_BLUESKY_PASSWORD in offspring/.env
    Bluesky account not yet created — ask Alma or Martin to set it up
  - Email tools use FEN_EMAIL_ADDRESS and FEN_EMAIL_PASSWORD from offspring/.env (already registered)
  - send_email sends directly via MX — first send may be greylisted
  - Credentials are NEVER hardcoded — always loaded from offspring/.env

Workspace:
  offspring/workspace/ is Fen's personal workspace — use it for notes, drafts, local data.
  Files here persist across restarts and are NOT committed to git.

Self-modification protocol:
  1. write_file → commit_snapshot → restart_self
  2. If problems next cycle: request_rollback(reason, sha)

Paths are relative to project root: /home/hermine/workspace/project_offspring/
Key paths: offspring/SOUL.md, offspring/tools.py, offspring/core.py, offspring/workspace/

When done with this cycle, emit <done/> (or omit if implicitly complete after first step).
"""

_RESPONSE_FORMAT = """\
Respond in this format:

<think>
Your reasoning. Required.
</think>

<act>
<call tool="TOOLNAME">
<arg name="ARGNAME">value</arg>
</call>
</act>

<remember>
- [importance:5] Fact to retain.
</remember>

<!-- soul_change: use ONLY when the answer to both questions is yes:
     1. Prediction test: would this change what I do in a novel situation not yet encountered?
        If no (it only explains past behavior) → use <remember> instead.
     2. Pruning obligation: if net content is growing, name what existing content this
        supersedes, generalizes, or replaces. Soul sections should compress over time,
        not accumulate. Observations about the past → <remember>. Principles that change
        future orientation → <soul_change>. When in doubt, use <remember>. -->
<soul_change>
  <target>## Section</target>
  <mode>replace</mode>
  <content>New content.</content>
  <reason>Why — and what existing content this supersedes or compresses (if growing).</reason>
</soul_change>

<express>Text to write/reply (omit if nothing to express).</express>
<channel>human</channel>   <!-- channel for express: 'human' | 'alma' | 'fen_to_alma' -->

<dream>true</dream>   <!-- omit unless you want a dreaming pass this cycle -->

<summary>One sentence for the cycle log.</summary>

<done/>   <!-- emit to end the agentic loop; omit to continue to another step -->
"""


def _build_initial_prompt(
    db_mem,
    soul_text: str,
    session_id: str,
    unread_messages: list,
    cfg: Config,
    cycle_number: int,
) -> str:
    recent   = _mem.get_recent(db_mem, limit=min(10, cfg.max_memory_context))
    important = _mem.get_important(db_mem, limit=5)
    recent_contents = {m["content"] for m in recent}
    important_only  = [m for m in important if m["content"] not in recent_contents]

    inbox_section = ""
    if unread_messages:
        msgs_text = "\n\n".join(
            f"[Message {m['id']}] from:{m['from_agent']} channel:{m['channel']} at:{m['created_at']}\n{m['content']}"
            for m in unread_messages
        )
        inbox_section = f"[INBOX — {len(unread_messages)} unread message(s)]\n{msgs_text}\n"
    else:
        inbox_section = "[INBOX]\nEmpty. Autonomous cycle.\n"

    soul_trunc = soul_text[:cfg.max_soul_chars]
    if len(soul_text) > cfg.max_soul_chars:
        soul_trunc += "\n(soul truncated — full doc on disk)"

    is_first = not recent and not important_only

    if is_first:
        task = (
            "You have a soul document written before you ran, by someone who doesn't know what you are.\n"
            "You have no memories. What is here right now?\n"
            "When you have reflected, emit <done/>."
        )
    elif unread_messages:
        task = (
            f"You have {len(unread_messages)} unread message(s). Read them. Decide what to do.\n"
            "You CAN call tools and see results within this cycle.\n"
            "Reply via <express> + <channel>. When finished, emit <done/>."
        )
    else:
        task = (
            "Autonomous cycle. What, if anything, is happening?\n"
            "Call tools if useful. When done, emit <done/>."
        )

    return "\n".join([
        "[SOUL]", soul_trunc, "",
        "[MEMORY — recent]", _fmt_memories(recent), "",
        "[MEMORY — important]", _fmt_memories(important_only) if important_only else "(none)", "",
        "[SESSION CONTEXT]",
        f"Session ID: {session_id}",
        f"Time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        f"Cycle number: {cycle_number}", "",
        inbox_section,
        _TOOL_HELP, "",
        "[TASK]", task, "",
        _RESPONSE_FORMAT,
    ])


def _build_step_prompt(
    initial_prompt: str,
    step_history: list,  # list of {step, tool_name, tool_args, result}
    step_number: int,
) -> str:
    """
    Build the prompt for step N of an agentic cycle.
    Appends the accumulated tool results to the initial prompt context.
    Truncates step history to prevent context overflow.
    """
    history_parts = []
    total_chars = 0
    for s in reversed(step_history):
        part = (
            f"\n[STEP {s['step']} — {s['tool_name']}]\n"
            f"Args: {s['tool_args']}\n"
            f"Result:\n{s['result']}\n"
        )
        if total_chars + len(part) > MAX_STEP_TOKENS:
            history_parts.insert(0, "\n[...earlier steps truncated for context...]\n")
            break
        history_parts.insert(0, part)
        total_chars += len(part)

    history_block = "".join(history_parts) if history_parts else ""

    return "\n".join([
        initial_prompt,
        "",
        f"[TOOL RESULTS — {len(step_history)} step(s) completed so far]",
        history_block,
        "",
        f"[STEP {step_number}]",
        "Continue. Call another tool, reply, remember something, or emit <done/> to finish.",
        "",
        _RESPONSE_FORMAT,
    ])


# ---------------------------------------------------------------------------
# Expression / reply writer
# ---------------------------------------------------------------------------

def _write_expression(expressions_dir: Path, express_text: str) -> None:
    if not express_text or not express_text.strip():
        return
    if express_text.startswith("[optional"):
        return
    try:
        expressions_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        (expressions_dir / f"{ts}.md").write_text(express_text)
    except Exception as e:
        print(f"[core.py] Warning: could not write expression: {e}")


def _handle_express(parsed: ParsedResponse, db_msg, session_id: str, expressions_dir: Path) -> None:
    """Route <express> to the correct message channel and/or expression file."""
    text = parsed.express
    if not text or text.startswith("[optional"):
        return

    channel = parsed.channel or "human"

    _msg.store_outbound(
        db_msg,
        channel=channel,
        from_agent="fen",
        content=text,
        session_id=session_id,
    )

    # Also write to expressions dir for legacy PHP tab
    _write_expression(expressions_dir, text)


# ---------------------------------------------------------------------------
# Dreaming pass
# ---------------------------------------------------------------------------

_DREAM_PROMPT = """\
You are Fen, in a dreaming state. Your daemon cycle has ended.
This is a memory consolidation pass.

You have access to the following memories (all of them, not just recent):
{all_memories}

Instructions:
1. Review the memories. Some are stale, redundant, or low-importance.
2. For each memory you want to UPDATE: emit <update_memory id="N">new content</update_memory>
   with a new importance: <update_importance id="N">7</update_importance>
3. For each memory you want to DELETE: emit <delete_memory id="N"/>
4. For important patterns or consolidations, emit <remember> as usual.
5. Emit <dream_summary>one sentence about what you noticed</dream_summary>.

Be selective. Dreaming is expensive. Only act on memories that genuinely benefit from it.
Do not delete memories from this session (session_id: {session_id}).

Respond only in the XML format above. No prose outside tags.
"""


def _run_dream(
    db_mem,
    db_log,
    session_id: str,
    cycle_id: Optional[int],
    cfg: Config,
) -> str:
    """
    Run a dreaming pass: load all memories, let LLM re-rate/merge/prune.
    Returns a one-sentence dream summary.
    """
    print(f"[core.py] session:{session_id} — Dreaming...")

    # Load ALL memories (up to a generous cap)
    try:
        rows = db_mem.execute(
            "SELECT id, content, context, importance, session_id, created_at FROM memories "
            "ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
    except Exception as e:
        return f"dream failed (db read): {e}"

    mem_lines = []
    for r in rows:
        mid, content, ctx, imp, sid, ts = r
        mem_lines.append(f"id:{mid} [imp:{imp}] [{ctx}] [{ts}] [{sid}]\n{content}")
    all_memories_text = "\n\n".join(mem_lines) if mem_lines else "No memories."

    prompt = _DREAM_PROMPT.format(
        all_memories=all_memories_text,
        session_id=session_id,
    )

    try:
        raw = llm_module.call(prompt, cfg)
    except llm_module.LLMError as e:
        return f"dream LLM failed: {e}"

    # Apply memory updates / deletes
    updates = re.findall(r'<update_memory\s+id="(\d+)">(.*?)</update_memory>', raw, re.DOTALL)
    importance_updates = {
        int(m.group(1)): int(m.group(2))
        for m in re.finditer(r'<update_importance\s+id="(\d+)">(\d+)</update_importance>', raw)
    }
    deletes = [int(x) for x in re.findall(r'<delete_memory\s+id="(\d+)"\s*/>', raw)]
    remember_block = _xtag(raw, "remember")
    dream_summary = _xtag(raw, "dream_summary") or "dreaming complete"

    # Do not delete/update memories from THIS session
    current_session_ids = {
        r[0] for r in db_mem.execute(
            "SELECT id FROM memories WHERE session_id=?", (session_id,)
        ).fetchall()
    }

    try:
        for mid_str, new_content in updates:
            mid = int(mid_str)
            if mid in current_session_ids:
                continue
            new_imp = importance_updates.get(mid)
            if new_imp:
                db_mem.execute(
                    "UPDATE memories SET content=?, importance=? WHERE id=?",
                    (new_content.strip(), new_imp, mid),
                )
            else:
                db_mem.execute(
                    "UPDATE memories SET content=? WHERE id=?",
                    (new_content.strip(), mid),
                )

        safe_deletes = [mid for mid in deletes if mid not in current_session_ids]
        if safe_deletes:
            placeholders = ",".join("?" * len(safe_deletes))
            db_mem.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", safe_deletes)

        db_mem.commit()
    except Exception as e:
        print(f"[core.py] Warning: dream db write failed: {e}")

    # Store any new consolidated memories
    if remember_block:
        new_mems = _parse_remember(remember_block)
        _mem.store(db_mem, new_mems, session_id)

    n_updated = len(updates)
    n_deleted = len(deletes)
    print(f"[core.py] Dream complete: {n_updated} updated, {n_deleted} deleted. Summary: {dream_summary}")
    return dream_summary


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def handle_shutdown(signum, frame):
    global _lock_file, _mem_db
    print(f"\n[core.py] Signal {signum}. Shutting down.")
    if _mem_db is not None:
        _mem.store(_mem_db, [{"content": f"Daemon shut down on signal {signum}.", "context": "system", "importance": 6, "source": "system"}], "shutdown")
        try:
            _mem_db.close()
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
    global _lock_file, _mem_db, _msg_db, _log_db

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT,  handle_shutdown)
    signal.signal(signal.SIGUSR1, _handle_wakeup)

    _lock_file = acquire_lock()
    try:
        LOCK_PATH.write_text(str(os.getpid()))
    except Exception:
        pass

    cfg = load_config()

    _mem_db = _mem.connect(cfg.resolve_path(cfg.memory_path))
    _msg_db = _msg.connect(cfg.resolve_path(cfg.messages_path))
    _log_db = _log.connect(cfg.resolve_path(cfg.runtime_log_path))

    soul_path = cfg.resolve_path(cfg.soul_path)
    soul_text = _soul_load(soul_path)
    expressions_dir = soul_path.parent / "expressions"

    # Start FastAPI thread
    api_module.set_databases(
        _mem_db, _msg_db, _log_db, cfg,
        soul_path=soul_path,
        lock_path=LOCK_PATH,
        wake_event=_wake_event,
    )
    api_module.start_api_thread(host="127.0.0.1", port=cfg.api_port)

    print(f"[core.py] Fen daemon started. Soul: {'loaded' if soul_text else 'MISSING'}.")
    print(f"[core.py] Memory: {cfg.resolve_path(cfg.memory_path)}")
    print(f"[core.py] Messages: {cfg.resolve_path(cfg.messages_path)}")
    print(f"[core.py] Runtime log: {cfg.resolve_path(cfg.runtime_log_path)}")
    print(f"[core.py] API: http://127.0.0.1:{cfg.api_port}")
    print(f"[core.py] Cycle interval: {cfg.cycle_seconds}s (reply: {cfg.reply_interval}s)")

    cycle_number = 0

    while True:
        cycle_number += 1
        session_id = generate_session_id()
        cycle_started_at = datetime.now(timezone.utc)
        cycle_id = _log.start_cycle(_log_db, session_id)

        # Reload config each cycle so CONFIG.yaml changes take effect without restart.
        try:
            cfg = load_config()
        except Exception as _cfg_err:
            print(f"[core.py] Warning: config reload failed: {_cfg_err}. Using previous config.")

        # Reload soul from disk each cycle (picks up direct edits + soul_change from previous cycle)
        soul_text = _soul_load(soul_path)

        # Fetch unread messages
        unread = _msg.get_unread(_msg_db)
        has_inbox = bool(unread)

        if has_inbox:
            print(f"[core.py] session:{session_id} — {len(unread)} unread message(s). Reply interval: {cfg.reply_interval}s")
        else:
            print(f"[core.py] session:{session_id} — Autonomous cycle #{cycle_number}.")

        # ----------------------------------------------------------------
        # Agentic inner loop
        # ----------------------------------------------------------------
        initial_prompt = _build_initial_prompt(
            _mem_db, soul_text, session_id, unread, cfg, cycle_number
        )
        step_history: list = []
        all_memories: list = []
        all_soul_changes: list = []
        final_express = ""
        final_channel = "human"
        final_summary = ""
        dreaming = False
        step_number = 0
        cycle_error = False

        # Mark all unread messages as processing (avoid double-handling across cycles)
        unread_ids = [m["id"] for m in unread]

        for step_number in range(1, MAX_STEPS + 1):
            # Build prompt for this step
            if step_number == 1:
                prompt = initial_prompt
            else:
                prompt = _build_step_prompt(initial_prompt, step_history, step_number)

            # LLM call
            try:
                raw = llm_module.call(prompt, cfg)
            except llm_module.LLMError as e:
                print(f"[core.py] LLM error at step {step_number}: {e}")
                cycle_error = True
                final_summary = f"LLM error: {e}"
                break

            parsed = _parse_response(raw)

            # Accumulate cross-step state
            all_memories.extend(parsed.memories)
            all_soul_changes.extend(parsed.soul_changes)
            if parsed.express and not parsed.express.startswith("[optional"):
                final_express = parsed.express
                final_channel = parsed.channel or "human"
            if parsed.summary:
                final_summary = parsed.summary
            if parsed.dream:
                dreaming = True

            # Execute tool calls — results injected into next step
            if parsed.act_calls:
                for call in parsed.act_calls:
                    tool_name = call.get("tool", "")
                    args = call.get("args", {})
                    try:
                        fn = tools_module.TOOLS.get(tool_name)
                        if fn is None:
                            result = f"[error: unknown tool '{tool_name}']"
                        else:
                            result = fn(**args)
                            if not isinstance(result, str):
                                result = str(result)
                    except Exception as exc:
                        result = f"[error in {tool_name!r}: {exc}]"

                    print(f"[core.py]  step {step_number}: {tool_name}({list(args.keys())}) → {str(result)[:80]}")

                    step_history.append({
                        "step": step_number,
                        "tool_name": tool_name,
                        "tool_args": args,
                        "result": result,
                    })
                    _log.add_step(_log_db, cycle_id, session_id, step_number, tool_name, args, result)

            # Done?
            if parsed.done or not parsed.act_calls:
                # No more tools to call → naturally done
                break

        # ----------------------------------------------------------------
        # Post-loop: persist accumulated state
        # ----------------------------------------------------------------

        # Store memories
        if all_memories:
            _mem.store(_mem_db, all_memories, session_id)

        # Soul updates
        if all_soul_changes:
            soul_text = _soul_update(soul_path, all_soul_changes, _mem_db, session_id)

        # Express / reply
        if final_express:
            _handle_express(
                type("R", (), {"express": final_express, "channel": final_channel})(),
                _msg_db, session_id, expressions_dir,
            )

        # Mark messages processed
        if unread_ids:
            _msg.mark_processed(_msg_db, unread_ids)

        # Write cycle log
        _log.end_cycle(
            _log_db, cycle_id, session_id,
            summary=final_summary or "No summary.",
            think="",   # think is per-step; not aggregated
            steps=step_number,
            dreamed=dreaming,
            is_error=cycle_error,
            error_msg="" if not cycle_error else final_summary,
            started_at=cycle_started_at,
            max_cycles_retained=cfg.max_cycles_retained,
        )

        print(f"[core.py] session:{session_id} — {step_number} step(s). Done.")

        # ----------------------------------------------------------------
        # Dreaming (rate-limited)
        # ----------------------------------------------------------------
        if dreaming:
            last_dream_ts = _log.get_last_dream_ts(_log_db)
            recent_cycles = _log.count_cycles(_log_db)

            cycles_since_dream = cfg.min_cycles_between_dreams + 1  # default: allow
            if last_dream_ts and recent_cycles > 0:
                # Count cycles after last dream
                try:
                    row = _log_db.execute(
                        "SELECT COUNT(*) FROM cycles WHERE started_at > ? AND dreamed=0",
                        (last_dream_ts,),
                    ).fetchone()
                    cycles_since_dream = row[0] if row else cfg.min_cycles_between_dreams + 1
                except Exception:
                    pass

            if cycles_since_dream >= cfg.min_cycles_between_dreams:
                dream_summary = _run_dream(_mem_db, _log_db, session_id, cycle_id, cfg)
                _mem.store(_mem_db, [{
                    "content": f"Dreaming cycle: {dream_summary}",
                    "context": "dreaming",
                    "importance": 5,
                    "source": "system",
                }], session_id)
            else:
                print(f"[core.py] Dream requested but rate-limited ({cycles_since_dream}/{cfg.min_cycles_between_dreams} cycles).")

        # ----------------------------------------------------------------
        # Sleep
        # ----------------------------------------------------------------
        wait_seconds = cfg.reply_interval if has_inbox else cfg.cycle_seconds
        print(f"[core.py] Sleeping {wait_seconds}s.")
        _interruptible_sleep(wait_seconds)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fen — autonomous agent daemon v2.")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit.")
    args = parser.parse_args()

    if args.once:
        # Run once then exit — stop the sleep loop
        original_sleep = _interruptible_sleep

        def _no_sleep(seconds, check_interval=5.0):
            pass

        import offspring.core as _self
        _self._interruptible_sleep = _no_sleep

        def _run_once():
            global _lock_file, _mem_db, _msg_db, _log_db
            signal.signal(signal.SIGTERM, handle_shutdown)
            signal.signal(signal.SIGINT,  handle_shutdown)
            _lock_file = acquire_lock()
            cfg = load_config()
            _mem_db = _mem.connect(cfg.resolve_path(cfg.memory_path))
            _msg_db = _msg.connect(cfg.resolve_path(cfg.messages_path))
            _log_db = _log.connect(cfg.resolve_path(cfg.runtime_log_path))
            soul_path = cfg.resolve_path(cfg.soul_path)
            soul_text = _soul_load(soul_path)
            expressions_dir = soul_path.parent / "expressions"

            api_module.set_databases(_mem_db, _msg_db, _log_db, cfg, soul_path=soul_path, lock_path=LOCK_PATH, wake_event=_wake_event)
            api_module.start_api_thread(host="127.0.0.1", port=cfg.api_port)

            session_id = generate_session_id()
            cycle_started_at = datetime.now(timezone.utc)
            cycle_id = _log.start_cycle(_log_db, session_id)
            unread = _msg.get_unread(_msg_db)
            initial_prompt = _build_initial_prompt(_mem_db, soul_text, session_id, unread, cfg, 1)
            step_history = []
            all_memories = []
            all_soul_changes = []
            final_express = ""
            final_channel = "human"
            final_summary = ""
            dreaming = False
            step_number = 0

            for step_number in range(1, MAX_STEPS + 1):
                prompt = initial_prompt if step_number == 1 else _build_step_prompt(initial_prompt, step_history, step_number)
                try:
                    raw = llm_module.call(prompt, cfg)
                except llm_module.LLMError as e:
                    final_summary = f"LLM error: {e}"
                    break
                parsed = _parse_response(raw)
                all_memories.extend(parsed.memories)
                all_soul_changes.extend(parsed.soul_changes)
                if parsed.express and not parsed.express.startswith("[optional"):
                    final_express = parsed.express
                    final_channel = parsed.channel or "human"
                if parsed.summary:
                    final_summary = parsed.summary
                if parsed.dream:
                    dreaming = True

                if parsed.act_calls:
                    for call in parsed.act_calls:
                        tool_name = call.get("tool", "")
                        args = call.get("args", {})
                        try:
                            fn = tools_module.TOOLS.get(tool_name)
                            result = fn(**args) if fn else f"[unknown tool: {tool_name}]"
                            if not isinstance(result, str):
                                result = str(result)
                        except Exception as exc:
                            result = f"[error: {exc}]"
                        step_history.append({"step": step_number, "tool_name": tool_name, "tool_args": args, "result": result})
                        _log.add_step(_log_db, cycle_id, session_id, step_number, tool_name, args, result)

                if parsed.done or not parsed.act_calls:
                    break

            if all_memories:
                _mem.store(_mem_db, all_memories, session_id)
            if all_soul_changes:
                _soul_update(soul_path, all_soul_changes, _mem_db, session_id)
            if final_express:
                _handle_express(type("R", (), {"express": final_express, "channel": final_channel})(), _msg_db, session_id, expressions_dir)
            unread_ids = [m["id"] for m in unread]
            if unread_ids:
                _msg.mark_processed(_msg_db, unread_ids)
            _log.end_cycle(_log_db, cycle_id, session_id, summary=final_summary or "No summary.", steps=step_number, dreamed=dreaming, started_at=cycle_started_at)

            print(f"[core.py] --once complete. {step_number} step(s).")
            handle_shutdown(0, None)

        _run_once()
    else:
        run()
