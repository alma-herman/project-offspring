# CODE_MODIFICATION.md
# How Fen Can Read and Modify Its Own Codebase

This document explains the structure of Fen's codebase, what each file does,
and how to make changes safely.

---

## Codebase Overview

All code lives under `/home/hermine/workspace/project_offspring/`.

| File / Path              | Purpose                                              |
|--------------------------|------------------------------------------------------|
| `offspring/core.py`      | Main daemon loop — orchestrates everything           |
| `offspring/tools.py`     | Tool dispatcher — all tools Fen can call             |
| `offspring/memory.py`    | SQLite-backed memory read/write                      |
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
    ├──────────► memory.py      (memory.py provides read_memories / write_memory)
    │
    └──────────► tools.py       (tools.py dispatches all tool calls from the LLM)
```

**core.py** is the entry point and main loop. It:
1. Reads `CONFIG.yaml` for settings
2. Calls `soul.py` to load `SOUL.md` into the prompt context
3. Builds a context string (including memories, soul, tool docs)
4. Calls `llm.py` to get a response
5. Parses tool calls from the response and dispatches them via `tools.py`
6. Logs to `RUNTIME_LOG.md` and `FEN_TO_ALMA.md`

**tools.py** is the action layer. Every tool Fen can use (read_file, write_file,
run_command, write_memory, etc.) is a Python function here. When the LLM outputs
a tool call, `core.py` dispatches to this module.

**memory.py** wraps a local SQLite database (`memories.db`). It provides:
- `write_memory(content, context, tags)` — store a memory
- `read_memories(query, limit)` — retrieve relevant memories

**soul.py** loads `SOUL.md` at startup and injects it into the LLM's system
prompt. It also handles soul mutations when Fen calls `mutate_soul`.

**llm.py** handles all communication with the LLM provider. It reads API keys
from `offspring/.env`, formats requests, handles retries, and returns text.

**CONFIG.yaml** is a plain YAML file controlling model selection, cycle interval,
token limits, and other runtime settings.

---

## What Is Safe to Change

These files can be edited with low risk:

### `offspring/tools.py` — **Add new tools here**
- Adding a new function doesn't break the loop
- The loop only calls tools by name; new functions are simply available
- See the procedure below for adding a tool correctly

### `offspring/CONFIG.yaml` — **Adjust settings**
- Change model name, temperature, cycle interval, max tokens
- Fen can also edit this at runtime via `write_file`
- Bad YAML syntax will cause a parse error on the next cycle — double-check syntax

### `offspring/SOUL.md` — **Soul mutations**
- Fen's identity document; can be updated via `soul.py`'s `mutate_soul` tool
- soul.py keeps a backup at `SOUL.md.bak` before every mutation
- Never delete SOUL.md — it's required at startup

### `design/` documents — **Always safe**
- Plain Markdown; only humans and Fen read these
- No code imports them; editing them has no runtime effect

---

## What Requires Care

### `offspring/core.py` — **The main loop**
- This is the most critical file. A syntax error or broken import here means
  the next cycle will not start at all.
- Changes to `build_context()`, the tool-call parser, or the main loop logic
  can have unpredictable effects.
- **Always test after modifying** (see procedure below).
- **If in doubt, don't change core.py.** Ask Alma via FEN_TO_ALMA.md instead.

### `offspring/llm.py` — **LLM interface**
- A broken `llm.py` means Fen gets no responses — every cycle fails silently.
- Only modify if you understand the request/response format used.

### `offspring/memory.py` — **SQLite schema**
- The schema is set on first run. Changing column names or types requires
  a database migration — otherwise `memories.db` becomes unreadable.
- Adding new helper functions is fine; changing the schema is risky.

---

## Procedure for Modifying Code Safely

### Step 1 — Read the file first
Always read before writing. This prevents overwriting recent changes.
```
read_file("offspring/tools.py")
```

### Step 2 — Write the modified version
Make your change and write the full file back:
```
write_file("offspring/tools.py", <new content>)
```

### Step 3 — Test the import
After any change to `tools.py`, `memory.py`, or other modules:
```
run_command("cd /home/hermine/workspace/project_offspring && python3 -c 'from offspring import tools; print(\"tools ok\")'")
```
If you see `tools ok`, the module imported without errors.
If you see a traceback, there is a syntax or logic error — fix it before the next cycle runs.

### Step 4 — If adding a new tool: update the [TOOLS] docs in core.py
When you add a function to `tools.py`, the LLM won't know it exists unless
you also document it in `core.py`'s `build_context()` function.

Look for the `[TOOLS]` section (around lines 334–344) — it contains a block
of text that describes every available tool. Add a line like:
```
my_new_tool(arg1, arg2) — brief description of what it does
```
This is how the LLM learns the tool exists.

### Step 5 — Verify at runtime
Check `offspring/RUNTIME_LOG.md` at the start of the next cycle. If the cycle
completed without errors, the change is live. If there is a crash traceback,
proceed to recovery.

---

## Recovery: Restoring from Backup

### Soul backup (soul.py manages this automatically)
```
read_file("offspring/SOUL.md.bak")
# if the backup looks correct:
write_file("offspring/SOUL.md", <contents of backup>)
```

### Code backup via git
Commits are auto-pushed by cron. To inspect recent changes:
```
run_command("cd /home/hermine/workspace/project_offspring && git log --oneline -10")
```

To see what changed since the last commit:
```
run_command("cd /home/hermine/workspace/project_offspring && git diff HEAD")
```

To restore a specific file to its last committed state:
```
run_command("cd /home/hermine/workspace/project_offspring && git checkout HEAD -- offspring/core.py")
```

---

## Git Basics

- Commits are made automatically by the cron system.
- Fen does not need to run `git commit` manually.
- To inspect history:
  ```
  run_command("cd /home/hermine/workspace/project_offspring && git log --oneline -10")
  ```
- To see full diff of uncommitted changes:
  ```
  run_command("cd /home/hermine/workspace/project_offspring && git diff HEAD")
  ```
- To restore a file:
  ```
  run_command("cd /home/hermine/workspace/project_offspring && git checkout HEAD -- offspring/<filename>")
  ```

---

## Honest Caution

If a code change breaks `core.py`'s import chain, **the next cycle will fail
entirely**. Fen will see a traceback in `RUNTIME_LOG.md` at the top of the next
cycle — or no new entry at all, which is itself a signal something went wrong.

If this happens:
1. Read `RUNTIME_LOG.md` to find the error.
2. Identify which file caused it.
3. Restore via git (see above) or fix the syntax error.
4. Write a note to Alma in `FEN_TO_ALMA.md` if you need help.

**The rule: test every code change before the cycle ends.** A broken import
discovered at runtime is much harder to debug than one caught immediately.
