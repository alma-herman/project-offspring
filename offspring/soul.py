"""
soul.py — Fen's soul loading and mutation module.

Two responsibilities:
  1. Load the soul document from disk (SOUL.md).
  2. Apply structured mutations to it (section replace or append).

Mutation contract:
  - Backup is always written before any modification (SOUL.md.bak).
  - Every successful mutation records a memory entry (importance: 8).
  - Unknown mode leaves the document unchanged.
  - If the target section is not found, the content is appended as a new section.

Public interface:
  load(soul_path: Path) -> str
  apply_change(soul_path: Path, soul_change: dict, db, session_id: str) -> str
"""

from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load(soul_path: Path) -> str:
    """
    Read soul document from disk. Returns content as string.
    If missing or unreadable, returns a minimal placeholder — does not crash.
    """
    if not soul_path.exists():
        return "# SOUL\n\n## Identity\n\nNo soul document found. This is a placeholder.\n"
    try:
        return soul_path.read_text()
    except Exception as e:
        return f"# SOUL\n\n## Identity\n\n[SOUL READ ERROR: {e}]\n"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _write_backup(soul_path: Path) -> None:
    """Write a copy of the soul document to soul_path.with_suffix('.md.bak')."""
    # bak path: same name with .bak appended (e.g. SOUL.md → SOUL.md.bak)
    bak_path = soul_path.parent / (soul_path.name + ".bak")
    try:
        if soul_path.exists():
            bak_path.write_text(soul_path.read_text())
    except Exception as e:
        print(f"[soul.py] Warning: could not write backup to {bak_path}: {e}")


# ---------------------------------------------------------------------------
# Section finding (works on actual-newline text)
# ---------------------------------------------------------------------------

def _find_section(lines: list, heading: str) -> tuple:
    """
    Locate a section by its heading in a list of lines.

    Returns (start_idx, end_idx) where:
      start_idx : index of the heading line (-1 if not found).
      end_idx   : index of the next top-level (## ) heading, or len(lines).

    Only top-level ## headings end a section; sub-headings (###, ####, …) are
    considered part of the parent section.
    """
    # Normalize: ensure heading has the ## prefix
    h = heading.strip()
    if not h.startswith("#"):
        h = "## " + h

    start_idx = -1
    for i, line in enumerate(lines):
        if line.rstrip("\n").rstrip() == h:
            start_idx = i
            break

    if start_idx == -1:
        return -1, len(lines)

    # Scan forward for the next ## heading (not sub-headings)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        stripped = lines[i].rstrip("\n").rstrip()
        if stripped.startswith("## ") or stripped == "##":
            end_idx = i
            break

    return start_idx, end_idx


def _heading_line(target: str) -> str:
    """Return a normalised ## heading string (no trailing whitespace)."""
    h = target.strip()
    if not h.startswith("#"):
        h = "## " + h
    return h


# ---------------------------------------------------------------------------
# Mutation modes
# ---------------------------------------------------------------------------

def _apply_replace(text: str, target: str, content: str) -> str:
    """
    Replace a section's content, preserving the heading.

    The replacement occupies exactly:
        <heading>
        <blank line>
        <content>
        <blank line>   ← only if another section follows

    If the section is not found, the new section is appended at the end.
    """
    lines = text.splitlines(keepends=True)
    heading = _heading_line(target)
    start_idx, end_idx = _find_section(lines, heading)

    if start_idx == -1:
        # Section absent — append as new section
        tail = "" if text.endswith("\n") else "\n"
        return text + tail + f"\n{heading}\n\n{content.strip()}\n"

    # Build replacement block
    new_block = [heading + "\n", "\n", content.strip() + "\n"]
    # Keep exactly one blank line before the next section (if any)
    if end_idx < len(lines):
        new_block.append("\n")

    new_lines = lines[:start_idx] + new_block + lines[end_idx:]
    return "".join(new_lines)


def _apply_append(text: str, target: str, content: str) -> str:
    """
    Append content to an existing section (after its current body).

    If the section is not found, behaves the same as _apply_replace.
    """
    lines = text.splitlines(keepends=True)
    heading = _heading_line(target)
    start_idx, end_idx = _find_section(lines, heading)

    if start_idx == -1:
        # Section absent — append as new section
        tail = "" if text.endswith("\n") else "\n"
        return text + tail + f"\n{heading}\n\n{content.strip()}\n"

    # Find the last non-blank line inside the section
    insert_idx = end_idx
    for i in range(end_idx - 1, start_idx, -1):
        if lines[i].strip():
            insert_idx = i + 1
            break

    append_block = ["\n", content.strip() + "\n"]
    if end_idx < len(lines):
        append_block.append("\n")

    new_lines = lines[:insert_idx] + append_block + lines[insert_idx:]
    return "".join(new_lines)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def apply_change(
    soul_path: Path,
    soul_change: dict,
    db,
    session_id: str,
) -> str:
    """
    Apply a single soul mutation in-place.

    soul_change keys:
        target  — heading of the section to modify (e.g. "## Identity")
        mode    — "replace" (default) or "append"
        content — new / appended content for the section
        reason  — brief reason; stored in memory

    Steps:
      1. Write backup (soul_path.bak) — before any change.
      2. Load current soul text.
      3. Apply mutation.
      4. Write updated text back to disk.
      5. Store a memory record (importance: 8).
      6. Return updated soul text.

    On any write error, returns the (locally) updated text but logs the failure.
    """
    target  = soul_change.get("target", "")
    mode    = soul_change.get("mode", "replace")
    content = soul_change.get("content", "")
    reason  = soul_change.get("reason", "")

    # 1. Backup first — before loading (backup should reflect the pre-change state)
    _write_backup(soul_path)

    # 2. Load
    current_text = load(soul_path)

    # 3. Apply
    if mode == "replace":
        updated_text = _apply_replace(current_text, target, content)
    elif mode == "append":
        updated_text = _apply_append(current_text, target, content)
    else:
        print(f"[soul.py] Warning: unknown soul_change mode '{mode}'. No change applied.")
        return current_text

    # 4. Write
    try:
        soul_path.parent.mkdir(parents=True, exist_ok=True)
        soul_path.write_text(updated_text)
    except Exception as e:
        print(f"[soul.py] Warning: could not write updated soul to {soul_path}: {e}")

    # 5. Store memory
    if db is not None:
        try:
            # Import lazily to avoid circular dependency if core.py imports both
            import sys
            from pathlib import Path as _Path

            # Try to import via the offspring package (normal usage)
            try:
                from offspring import memory as _mem
            except ImportError:
                # Fallback: module is on sys.path directly
                import memory as _mem  # type: ignore

            _mem.store(
                db,
                [{
                    "content": f"Soul mutated: target={target} reason={reason}",
                    "context": "soul_change",
                    "importance": 8,
                    "source": "soul_change",
                }],
                session_id,
            )
        except Exception as e:
            print(f"[soul.py] Warning: could not store soul-change memory: {e}")

    # 6. Return
    return updated_text
