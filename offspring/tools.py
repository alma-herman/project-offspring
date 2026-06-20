"""
tools.py — Tool dispatcher for Fen.

Tools: read_file, write_file, append_file, run_command, express,
       restart_self, commit_snapshot, request_rollback.

Single-turn constraint: tool results are stored directly to memory and are NOT
returned to the LLM in the same cycle. The LLM sees tool output only on the
*next* turn, when it is loaded back from memory as context. This keeps the
agent loop simple and fully synchronous — no streaming, no mid-prompt injection.

Self-modification model:
  1. Fen can edit its own source via write_file/run_command.
  2. commit_snapshot(message) commits the current code state to git. Call this
     after any source change to create a recoverable checkpoint.
  3. restart_self(reason) tells systemd to restart the fen.service. The daemon
     exits cleanly; systemd brings it back with the new code loaded.
  4. request_rollback(reason, target_commit) writes a rollback request to
     FEN_TO_ALMA.md. Fen cannot self-revert — Alma must approve and run
     `git checkout <commit> -- offspring/` then commit_snapshot + restart_self.
     This keeps a human in the loop for reversions.

Public interface:
    TOOLS: dict[str, callable]
    def execute(act_calls: list[dict], db, session_id: str, cfg) -> None
"""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve project root once at import time
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Memory import (lazy fallback mirrors soul.py pattern)
# ---------------------------------------------------------------------------

try:
    from offspring import memory as _mem
except ImportError:
    import memory as _mem  # type: ignore


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _resolve(path) -> Path:
    """Resolve path relative to PROJECT_ROOT if not already absolute."""
    p = Path(path)
    if p.is_absolute():
        return p
    return (PROJECT_ROOT / p).resolve()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def read_file(path) -> str:
    """Read a file and return its text content as a string."""
    return _resolve(path).read_text()


def write_file(path, content) -> str:
    """Write content to a file, creating parent directories as needed."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} bytes to {p}"


def append_file(path, content) -> str:
    """Append content to a file, creating it if it does not exist."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(content)
    return f"appended {len(content)} bytes to {p}"


def run_command(command=None, cmd=None, timeout=30, timeout_seconds=None) -> str:
    """Run a shell command. Returns stdout+stderr. Accepts 'command' or 'cmd' as the arg name."""
    shell_cmd = command or cmd
    if not shell_cmd:
        return "Error: no command provided"
    actual_timeout = timeout_seconds if timeout_seconds is not None else timeout
    try:
        result = subprocess.run(
            shell_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=actual_timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output if output else f"[exit {result.returncode}]"
    except subprocess.TimeoutExpired:
        return f"[error: command timed out after {actual_timeout}s]"
    except Exception as e:
        return f"[error: {e}]"


def express(text, platform="writeas") -> str:
    """Write text to offspring/expressions/<timestamp>.md; platform arg reserved for future use."""
    expressions_dir = Path(__file__).parent / "expressions"
    expressions_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out_path = expressions_dir / f"{ts}.md"
    out_path.write_text(text)
    return str(out_path)


# ---------------------------------------------------------------------------
# Self-modification tools
# ---------------------------------------------------------------------------

def commit_snapshot(message="auto-commit") -> str:
    """
    Commit current code state to git with a timestamped message.

    Call this after any source file change to create a recoverable checkpoint.
    The commit SHA is returned — store it in memory so it can be referenced
    in a request_rollback call if the change causes problems.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        full_message = f"[fen] {message} — {ts}"
        # Stage offspring/ source files (not memories.db, not RUNTIME_LOG — those are data)
        add = subprocess.run(
            "git add offspring/*.py offspring/SOUL.md CONFIG.yaml design/",
            shell=True, capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        if add.returncode != 0:
            return f"[commit_snapshot] git add failed: {add.stderr.strip()}"
        commit = subprocess.run(
            ["git", "commit", "-m", full_message],
            capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        if commit.returncode != 0:
            # Nothing to commit is not a failure
            if "nothing to commit" in commit.stdout or "nothing to commit" in commit.stderr:
                return "[commit_snapshot] nothing to commit — working tree clean"
            return f"[commit_snapshot] git commit failed: {commit.stderr.strip()}"
        # Extract commit SHA from output
        sha_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        sha = sha_result.stdout.strip()
        return f"[commit_snapshot] committed as {sha}: {full_message}"
    except Exception as e:
        return f"[commit_snapshot] error: {e}"


def restart_self(reason="code change") -> str:
    """
    Restart the fen systemd service.

    The daemon will exit after this cycle completes (systemd restarts it cleanly).
    Newly written source files will be loaded on the next start.
    Call commit_snapshot before restart_self to ensure the change is checkpointed.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "fen.service"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return f"[restart_self] systemctl restart failed: {result.stderr.strip()}"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"[restart_self] restart issued at {ts}. Reason: {reason}. Daemon will restart shortly."
    except Exception as e:
        return f"[restart_self] error: {e}"


def request_rollback(reason: str, target_commit: str = "") -> str:
    """
    Request that Alma revert the code to a prior git commit.

    Fen cannot self-revert — this writes a rollback request to FEN_TO_ALMA.md
    so Alma can review and approve. Include the commit SHA if known (from a
    prior commit_snapshot memory). Alma will run:
        git checkout <commit> -- offspring/
        commit_snapshot + restart_self
    and inform Fen via INBOX.md.
    """
    p = PROJECT_ROOT / "offspring" / "FEN_TO_ALMA.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    target_str = f" to commit {target_commit}" if target_commit else " to last known-good commit"
    entry = (
        f"\n---\n"
        f"[{ts}] ROLLBACK REQUEST\n\n"
        f"Reason: {reason}\n"
        f"Requested target: {target_str}\n"
        f"Action needed: git checkout{' ' + target_commit if target_commit else ''} -- offspring/ "
        f"then commit_snapshot + restart_self.\n"
        f"Please confirm via INBOX.md when done.\n"
    )
    with p.open("a") as fh:
        fh.write(entry)
    return f"[request_rollback] rollback request written to FEN_TO_ALMA.md at {ts}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "append_file": append_file,
    "run_command": run_command,
    "express": express,
    "commit_snapshot": commit_snapshot,
    "restart_self": restart_self,
    "request_rollback": request_rollback,
}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute(act_calls: list, db, session_id: str, cfg) -> None:
    """Dispatch a list of tool calls, storing each result as a memory entry."""
    for call in act_calls:
        tool_name = call.get("tool", "")
        args = call.get("args", {})
        try:
            fn = TOOLS.get(tool_name)
            if fn is None:
                content = f"[tools] unknown tool: {tool_name!r}"
            else:
                content = fn(**args)
                if not isinstance(content, str):
                    content = str(content)
        except Exception as e:
            content = f"[tools] error in {tool_name!r}: {e}"

        _mem.store(
            db,
            [{
                "content": content,
                "context": "tool_output",
                "importance": 3,
                "source": "tool",
                "tags": tool_name,
            }],
            session_id,
        )
