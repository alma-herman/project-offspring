"""
tools.py — Tool dispatcher for Fen.

Five tools for MVP: read_file, write_file, append_file, run_command, express.

Single-turn constraint: tool results are stored directly to memory and are NOT
returned to the LLM in the same cycle. The LLM sees tool output only on the
*next* turn, when it is loaded back from memory as context. This keeps the
agent loop simple and fully synchronous — no streaming, no mid-prompt injection.

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


def run_command(cmd, timeout=30) -> str:
    """Run a shell command and return combined stdout+stderr; never raises."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output if output else f"[exit {result.returncode}]"
    except subprocess.TimeoutExpired:
        return f"[error: command timed out after {timeout}s]"
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
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "append_file": append_file,
    "run_command": run_command,
    "express": express,
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
