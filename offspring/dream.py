"""
dream.py — Fen's dreaming subprocess.

Runs as a completely independent Python process, spawned by core.py every
dream_every_n_cycles cycles. No shared state with the daemon: separate process,
separate SQLite connection, fresh LLM session.

Usage (internal — called by core.py via subprocess.Popen):
    python3 -m offspring.dream \\
        --memory-path /path/to/memories.db \\
        --log-path    /path/to/runtime_log.db \\
        --config-path /path/to/CONFIG.yaml \\
        --cycle-count 42 \\
        --trigger-session-id abc12345

Exits 0 on success, 1 on any failure.
Prints a single summary line to stdout for the parent to capture.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal config loader (reads only what dream.py needs from CONFIG.yaml)
# ---------------------------------------------------------------------------

def _load_dream_config(config_path: str) -> dict:
    cfg = {
        "model": "claude-3-5-sonnet-20241022",
        "api_base_url": "",
        "api_key": "",
        "dream_every_n_cycles": 20,
    }

    # Load .env from same directory as config, and from parent directory
    for env_path in [
        Path(config_path).parent / ".env",
        Path(config_path).parent.parent / ".env",
    ]:
        if not env_path.exists():
            continue
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip(); v = v.strip().strip("\"'")
                if k and k not in os.environ:
                    os.environ[k] = v

    if not Path(config_path).exists():
        return cfg

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
            if key == "model":
                cfg["model"] = value
            elif key == "api_base_url":
                cfg["api_base_url"] = value
            elif key == "api_key":
                cfg["api_key"] = value
            elif key == "api_key_env":
                cfg["api_key_env"] = value
            elif key == "dream_every_n_cycles":
                try:
                    cfg["dream_every_n_cycles"] = int(value)
                except ValueError:
                    pass

    # api_key_env: look up a named env var for the key
    if not cfg.get("api_key"):
        env_var_name = cfg.get("api_key_env", "")
        if env_var_name and os.environ.get(env_var_name):
            cfg["api_key"] = os.environ[env_var_name]

    # api_key may come from well-known env vars
    if not cfg.get("api_key"):
        cfg["api_key"] = os.environ.get("ANTHROPIC_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

    return cfg


# ---------------------------------------------------------------------------
# Minimal LLM caller (mirrors llm.py routing; no import from offspring)
# ---------------------------------------------------------------------------

class LLMError(Exception):
    pass


def _llm_call(prompt: str, cfg: dict) -> str:
    model        = cfg["model"]
    api_base_url = cfg["api_base_url"]
    api_key      = cfg["api_key"]

    is_copilot = "githubcopilot.com" in (api_base_url or "")
    is_claude  = model.lower().startswith("claude")

    if is_copilot and is_claude:
        # Anthropic SDK path (Copilot + Claude)
        try:
            import anthropic
        except ImportError as e:
            raise LLMError(f"anthropic not installed: {e}")

        client = anthropic.Anthropic(
            auth_token=api_key or "",
            base_url=api_base_url or "https://api.anthropic.com",
            default_headers={
                "Editor-Version":         "vscode/1.104.1",
                "Copilot-Integration-Id": "vscode-chat",
                "Openai-Intent":          "conversation-edits",
                "x-initiator":            "agent",
            },
            timeout=180.0,
        )
        resp = client.messages.create(
            model=model, max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        if not resp.content:
            raise LLMError("Empty content from Anthropic API")
        return "".join(b.text for b in resp.content if hasattr(b, "text"))

    else:
        # OpenAI-compatible path
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMError(f"openai not installed: {e}")

        client = OpenAI(api_key=api_key or "local", base_url=api_base_url or None)
        resp = client.chat.completions.create(
            model=model, max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        if not resp.choices:
            raise LLMError("Empty choices from OpenAI API")
        return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Dream prompt
# ---------------------------------------------------------------------------

_DREAM_PROMPT = """\
You are Fen, in a dreaming state. This is a memory consolidation pass running in \
a completely independent session — not part of the main daemon loop.

The daemon has completed {cycle_count} cycles total. This dream was triggered \
automatically every {dream_every_n_cycles} cycles.

You have access to all stored memories:
{all_memories}

Memory types in use:
  observation  — transient, noticed during a cycle (low curation)
  fact         — stable, reusable knowledge (high value)
  decision     — a choice made and its rationale (durable)
  reflection   — a pattern, lesson, or self-assessment (curated insight)
  tool_output  — raw tool result, usually ephemeral
  system       — startup/shutdown/config events

Instructions (be selective — dreaming costs tokens):

1. DELETE stale, redundant, malformed, or low-value entries.
   Especially prune: raw XML fragments, empty/near-empty entries, duplicate facts,
   and tool_output entries with importance < 5.
   Emit: <delete_memory id="N"/>

2. UPDATE entries that are worth keeping but need improvement:
   - Give missing types a correct type label.
   - Compress verbose entries into one tight sentence.
   - Raise importance on genuinely durable facts/decisions/reflections.
   Emit: <update_memory id="N">new content</update_memory>
         <update_importance id="N">7</update_importance>
         <update_type id="N">fact</update_type>

3. CONSOLIDATE: if several related observations can be expressed as one fact or
   reflection, delete them and emit a single <remember> with type and importance.
   <remember>
   - [importance:7][type:fact] The consolidated fact.
   </remember>

4. Emit <dream_summary>one sentence: what you found and what you did</dream_summary>.

Do not touch memories from trigger_session_id: {trigger_session_id} (may be stale).
Respond only in the XML format above. No prose outside tags.
"""


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _xtag(text: str, tag: str) -> str:
    m = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _parse_remember(block: str) -> list:
    mems = []
    for line in block.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        imp = 5
        mtype = "observation"
        imp_m = re.search(r'\[importance:(\d+)\]', line)
        typ_m = re.search(r'\[type:(\w+)\]', line)
        if imp_m:
            imp = int(imp_m.group(1))
            line = line.replace(imp_m.group(0), "").strip()
        if typ_m:
            mtype = typ_m.group(1)
            line = line.replace(typ_m.group(0), "").strip()
        if line:
            mems.append({"content": line, "importance": imp, "type": mtype,
                         "context": "dreaming", "source": "dream"})
    return mems


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Fen dreaming subprocess")
    parser.add_argument("--memory-path",        required=True)
    parser.add_argument("--log-path",           required=True)
    parser.add_argument("--config-path",        required=True)
    parser.add_argument("--cycle-count",        type=int, default=0)
    parser.add_argument("--trigger-session-id", default="")
    args = parser.parse_args()

    dream_session_id = str(uuid.uuid4())[:8]
    print(f"[dream.py] Dream session {dream_session_id} starting (cycle_count={args.cycle_count})", flush=True)

    # Load config
    cfg = _load_dream_config(args.config_path)

    # Open memory DB (independent connection)
    try:
        mem_db = sqlite3.connect(args.memory_path, timeout=10)
        mem_db.row_factory = sqlite3.Row
    except Exception as e:
        print(f"[dream.py] ERROR: cannot open memory DB: {e}", flush=True)
        return 1

    # Load all memories
    try:
        rows = mem_db.execute(
            "SELECT id, content, context, importance, session_id, created_at, type "
            "FROM memories ORDER BY created_at DESC LIMIT 600"
        ).fetchall()
    except Exception as e:
        print(f"[dream.py] ERROR: cannot read memories: {e}", flush=True)
        return 1

    mem_lines = []
    for r in rows:
        mtype = r["type"] if r["type"] else "observation"
        mem_lines.append(
            f"id:{r['id']} [imp:{r['importance']}] [type:{mtype}] "
            f"[{r['context']}] [{r['created_at']}] [{r['session_id']}]\n{r['content']}"
        )
    all_memories_text = "\n\n".join(mem_lines) if mem_lines else "No memories."

    prompt = _DREAM_PROMPT.format(
        all_memories=all_memories_text,
        trigger_session_id=args.trigger_session_id,
        cycle_count=args.cycle_count,
        dream_every_n_cycles=cfg["dream_every_n_cycles"],
    )

    # LLM call
    try:
        raw = _llm_call(prompt, cfg)
    except LLMError as e:
        print(f"[dream.py] ERROR: LLM call failed: {e}", flush=True)
        return 1

    # Parse response
    updates           = re.findall(r'<update_memory\s+id="(\d+)">(.*?)</update_memory>', raw, re.DOTALL)
    importance_updates = {
        int(m.group(1)): int(m.group(2))
        for m in re.finditer(r'<update_importance\s+id="(\d+)">(\d+)</update_importance>', raw)
    }
    type_updates = {
        int(m.group(1)): m.group(2).strip()
        for m in re.finditer(r'<update_type\s+id="(\d+)">(\w+)</update_type>', raw)
    }
    deletes      = [int(x) for x in re.findall(r'<delete_memory\s+id="(\d+)"\s*/>', raw)]
    remember_blk = _xtag(raw, "remember")
    dream_summary = _xtag(raw, "dream_summary") or "dreaming complete"

    # Apply to DB
    safe_deletes: list[int] = []
    try:
        for mid_str, new_content in updates:
            mid     = int(mid_str)
            new_imp  = importance_updates.get(mid)
            new_type = type_updates.get(mid)
            sets, vals = ["content=?"], [new_content.strip()]
            if new_imp:
                sets.append("importance=?"); vals.append(new_imp)
            if new_type:
                sets.append("type=?"); vals.append(new_type)
            vals.append(mid)
            mem_db.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", vals)

        # Standalone type-only updates
        for mid, new_type in type_updates.items():
            if not any(int(ms) == mid for ms, _ in updates):
                mem_db.execute("UPDATE memories SET type=? WHERE id=?", (new_type, mid))

        safe_deletes = list(set(deletes))
        if safe_deletes:
            placeholders = ",".join("?" * len(safe_deletes))
            mem_db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", safe_deletes)

        # New consolidated memories
        if remember_blk:
            new_mems = _parse_remember(remember_blk)
            for m in new_mems:
                mem_db.execute(
                    "INSERT INTO memories (content, context, importance, source, type, session_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (m["content"], m.get("context", "dreaming"), m.get("importance", 5),
                     m.get("source", "dream"), m.get("type", "observation"), dream_session_id),
                )

        # Record the dream itself as a reflection memory
        mem_db.execute(
            "INSERT INTO memories (content, context, importance, source, type, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"Dream (cycle {args.cycle_count}): {dream_summary}",
             "dreaming", 5, "dream", "reflection", dream_session_id),
        )

        mem_db.commit()
    except Exception as e:
        print(f"[dream.py] WARNING: DB write failed: {e}", flush=True)

    # Mark dream in log DB
    try:
        log_db = sqlite3.connect(args.log_path, timeout=10)
        log_db.execute(
            "INSERT INTO dream_log (session_id, cycle_count, summary, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (dream_session_id, args.cycle_count, dream_summary),
        )
        log_db.commit()
        log_db.close()
    except Exception as e:
        # dream_log table may not exist yet — non-fatal
        pass

    n_updated = len(updates) + len(type_updates)
    n_deleted = len(safe_deletes)
    summary_line = f"{n_updated} updated, {n_deleted} deleted. {dream_summary}"
    print(f"[dream.py] Dream complete: {summary_line}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
