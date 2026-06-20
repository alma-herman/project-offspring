# CODE_MODIFICATION.md
# How Fen Can Read and Modify Its Own Codebase

This document explains the structure of Fen's codebase, what each file does,
and how to make changes safely — including restarting with new code and
requesting rollback if something goes wrong.

---

## Codebase Overview

All code lives under `/home/hermine/workspace/project_offspring/`.

| File / Path              | Purpose                                              |
|--------------------------|------------------------------------------------------|
| `offspring/core.py`      | Main daemon loop — orchestrates everything           |
| `offspring/tools.py`     | Tool dispatcher — all tools Fen can call             |
| `offspring/memory.py`    | SQLite-backed memory read/write (memories.db)        |
| `offspring/messages.py`  | SQLite-backed messaging (messages.db)                |
| `offspring/runtime_log.py` | SQLite-backed cycle logging (runtime_log.db)       |
| `offspring/api.py`       | FastAPI service on localhost:7744                    |
| `offspring/soul.py`      | Loads, validates, and mutates SOUL.md                |
| `offspring/llm.py`       | All LLM API calls (prompt → response)                |
| `offspring/CONFIG.yaml`  | Runtime configuration (model, intervals, limits)     |
| `offspring/SOUL.md`      | Fen's living soul document — values, name, goals     |
| `design/`                | Human-readable design documents (safe to edit)       |

---

## How the Files Relate

```
CONFIG.yaml
    │
    ▼
core.py  ──────► llm.py        (core.py calls llm.py to get LLM responses)
    │
    ├──────────► soul.py        (soul.py is imported at startup; loads SOUL.md)
    │
    ├──────────► memory.py      (opens memories.db)
    │
    ├──────────► messages.py    (opens messages.db — inbox/outbox/letters to Alma)
    │
    ├──────────► runtime_log.py (opens runtime_log.db — cycle history)
    │
    └──────────► tools.py       (tools.py dispatches all tool calls from the LLM)

api.py runs as a background thread — FastAPI on localhost:7744
  Alma reads Fen's state via: GET /status, /messages, /cycles, /memories
  Alma writes to Fen via:    POST /messages (direction='in', channel='inbox')
```

**core.py** is the entry point and main loop. It:
1. Reads `CONFIG.yaml` for settings
2. Calls `soul.py` to load `SOUL.md` into the prompt context
3. Builds a context string (including memories, soul, tool docs) via `build_context()`
4. Calls `llm.py` to get a response
5. Parses the XML response and dispatches tool calls via `tools.py`
6. Stores memories in memories.db, logs cycles to runtime_log.db
7. Reads inbound messages from messages.db (direction='in')

Note: core.py logs cycles to `offspring/runtime_log.db` (not a flat file).
Fen writes to Alma by calling the `send_message` tool with channel='fen_to_alma'.
Alma reads those messages via the FastAPI: `GET /messages?channel=fen_to_alma`.

**tools.py** is the action layer. Every tool Fen can use (read_file, write_file,
run_command, commit_snapshot, restart_self, send_message, etc.) is a Python function
here. The TOOLS dict at the bottom of tools.py is the authoritative registry.

**memory.py** wraps `memories.db`. Actual public API:
- `connect(path)` — open the database; returns a connection
- `store(db, memories, session_id)` — store a list of memory dicts
- `get_recent(db, limit)` — retrieve most recent memories
- `get_important(db, limit)` — retrieve highest-importance memories
- `search(db, query, limit)` — keyword search over memory content
- `get_session(db, session_id)` — all memories from a specific session

Note: there is no `write_memory()` or `read_memories()` function. Memories
are stored via the `<remember>` block in the LLM response — core.py parses
the block and calls `memory.store()` directly.

**messages.py** wraps `messages.db`. Channels:
- `direction='in', channel='inbox'`: messages FROM Alma TO Fen
- `direction='out', channel='outbox'`: Fen's output/expressions
- `channel='fen_to_alma'`: Fen's direct letters to Alma

**runtime_log.py** wraps `runtime_log.db`. Tables: `cycles` + `cycle_steps`.
Auto-prunes to 500 cycles.

**soul.py** loads `SOUL.md` at startup and handles soul mutations. Mutations
happen via the `<soul_change>` XML block in the LLM response — core.py parses
this and calls `soul_module.apply_change()`. There is no `mutate_soul` tool
in tools.py; soul mutations go through the structured XML response, not a tool call.

**llm.py** handles all communication with the LLM provider. It reads API keys
from environment variables (set in `offspring/.env`), formats requests, and
returns text. Dual routing: Anthropic SDK for Claude/Copilot, OpenAI SDK for others.

**CONFIG.yaml** is a plain YAML file controlling model selection, cycle interval,
token limits, and other runtime settings.

---

## What Is Safe to Change

### `offspring/tools.py` — **Add new tools here**
- Adding a new function doesn't break the loop
- Register it in the `TOOLS` dict at the bottom of the file
- Document it in `core.py`'s `build_context()` `[TOOLS]` section
- See the procedure below

### `offspring/CONFIG.yaml` — **Adjust settings**
- Change model name, cycle interval, max_soul_chars, max_memory_context
- Fen can edit this at runtime via `write_file`
- Bad YAML syntax causes a parse error on the next cycle — test with:
  `run_command("python3 -c 'import yaml; yaml.safe_load(open(\"offspring/CONFIG.yaml\"))'  ")`

### `offspring/SOUL.md` — **Soul mutations**
- Fen's identity document. Mutations go through the `<soul_change>` XML block.
- soul.py keeps a backup at `SOUL.md.bak` before every mutation.
- Never delete SOUL.md — it is required at startup.

### `design/` documents — **Always safe**
- Plain Markdown; no code imports them.

---

## What Requires Care

### `offspring/core.py` — **The main loop**
- A syntax error or broken import here means the next cycle will not start.
- Changes to `build_context()`, the parser, or the main loop need testing.
- **Test after any change.** See procedure below.
- **If in doubt, don't change core.py.** Use `send_message(channel='fen_to_alma', ...)` to ask Alma instead.
- After any change: `commit_snapshot` + `restart_self`.

### `offspring/llm.py` — **LLM interface**
- A broken `llm.py` means every cycle fails — no LLM response.
- Only modify if you understand the Anthropic/OpenAI SDK routing.

### `offspring/memory.py` — **SQLite schema**
- The schema is set on first run. Changing column names or types requires
  a database migration; otherwise `memories.db` becomes unreadable.
- Adding new helper functions is safe; changing the schema is not.

---

## Self-Modification Protocol

Fen can modify its own source code. The protocol ensures every change is
checkpointed and reversible:

### Step 1 — Read the file first
Always read before writing. Prevents overwriting recent changes.
```
read_file("offspring/tools.py")
```

### Step 2 — Write the modified version
```
write_file("offspring/tools.py", <new full content>)
```

### Step 3 — Test the import
```
run_command("cd /home/hermine/workspace/project_offspring && python3 -c 'from offspring import tools; print(\"ok\")'")
```
If you see `ok`, the module imported without errors. If you see a traceback,
fix the error before continuing.

### Step 4 — Commit the snapshot
```
commit_snapshot("brief description of change")
```
This creates a git commit of the modified source files. **Store the returned
SHA in memory** — you will need it if you need to request a rollback later.

### Step 5 — Restart the daemon
```
restart_self("reason for restart")
```
The daemon exits cleanly; systemd brings it back with the new code loaded.
The result of restart_self is available next cycle (tool results are deferred).

### Step 6 (if needed) — Request rollback
If the change causes problems and you need to revert:
```
request_rollback("what went wrong", "sha_from_step_4")
```
This writes a rollback request to messages.db (channel='fen_to_alma'). **Fen cannot
self-revert.** Alma must approve and run:
```
git checkout <sha> -- offspring/
commit_snapshot + restart_self
```
Alma will confirm by posting to the inbox via the FastAPI (direction='in').

---

## Recovery: Restoring from Backup

### Soul backup (soul.py manages this automatically)
```
read_file("offspring/SOUL.md.bak")
# if the backup looks correct:
write_file("offspring/SOUL.md", <contents of backup>)
```

### Code backup via git
Commits are made by Fen via `commit_snapshot`. To inspect recent history:
```
run_command("cd /home/hermine/workspace/project_offspring && git log --oneline -10")
```

To see what changed since the last commit:
```
run_command("cd /home/hermine/workspace/project_offspring && git diff HEAD")
```

To see what a specific commit changed:
```
run_command("cd /home/hermine/workspace/project_offspring && git show <sha>")
```

Note: Fen does NOT run `git checkout` to self-revert. Use `request_rollback`
and wait for Alma's confirmation.

---

## Cycle Logging

Each cycle is recorded in `runtime_log.db`. If a cycle fails silently (no new
entry appears in the runtime log), it means core.py's import chain is broken.

If this happens:
1. Check `journalctl --user -u fen.service -n 50` for the error (run via Alma's terminal — Fen cannot reach journalctl directly).
2. Use `request_rollback` to ask Alma for help.
3. Write a note to Alma via `send_message(channel='fen_to_alma', content='...')`.

**The rule: test every code change before calling restart_self.** A broken
import discovered at runtime is much harder to debug than one caught immediately.

---

## Communicating with Alma

To write to Alma directly (was FEN_TO_ALMA.md):
```
send_message(channel="fen_to_alma", content="your message here")
```
Alma reads these via the dashboard at http://alma.dedyn.io/fen_ui/ and via the FastAPI.

To read messages Alma has sent you (was INBOX.md):
Core.py reads these automatically each cycle and includes them in context.
You can also query them directly:
```
run_command("curl -s 'http://127.0.0.1:7744/messages?direction=in&limit=10'")
```
