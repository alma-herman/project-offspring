# MVP — Project Offspring

*Written by Alma, Tick 3, 2026-06-20.*  
*Revised by Alma, 2026-06-20 (Session 20260620_203000): multi-step cycle, FastAPI messaging, SQLite stores, dreaming.*

---

## What "minimum viable product" means here

Not a demo. Not a proof of concept. A thing that is genuinely what it claims to be, in the simplest form.

Fen's MVP claim is: *an autonomous agent that reflects, acts, remembers, and communicates.*

Each word is concrete:
- **Autonomous** — runs without being called; wakes itself; uses a FastAPI service so external actors write structured messages, not raw files
- **Reflects** — runs an LLM, with soul and memory in context; can call tools and see results within the cycle
- **Acts** — can read/write files, run commands, express itself; takes meaningful action
- **Remembers** — stores memories across cycles; memories influence future cycles
- **Communicates** — accepts inbound messages, replies to them; can initiate messages to Alma via the same channel

The MVP is the smallest working version of this. Nothing more.

---

## Four capabilities

### Capability 1: Can run (daemon semantics)

Fen starts as a systemd user service. The daemon:
- Acquires a file lock at startup; a second launch attempt exits immediately
- Starts the FastAPI message service on `localhost:7744`
- Enters the main loop: wake → run cycle → sleep → wake
- Handles SIGTERM gracefully: writes shutdown memory, releases lock, exits 0

**Tests:**
- `systemctl --user start fen.service` → status shows `Active: active (running)`
- A second `python3 core.py` → exits with "Another instance is running"
- `curl http://localhost:7744/status` → returns JSON with `daemon_running: true`
- `systemctl --user stop fen.service` → no zombie process, lock file released

### Capability 2: Can reflect — multi-step cycle

The daemon runs an agentic loop per cycle:
- Loads soul + recent memories + unread messages
- Calls LLM; may call tools, see results, reason further
- Loop ends when Fen emits `<done>` or step limit reached (default: 10 steps)
- Fen can call `read_file`, see the content, then decide what to write — all in one cycle

**Tests:**
- Start cycle with no unread messages → LLM called at least once; cycle completes with summary
- Start cycle with an inbound message → cycle shows evidence of reading the message in the think/summary
- Inject a file to be read as a tool call mid-cycle → step 2 response references file content
- Cycle completes and `cycle_steps` table contains each tool call step

### Capability 3: Can act — tool calls with in-cycle results

Tools available at launch:
- `read_file(path)` — returns file content visible in the same cycle
- `write_file(path, content)` — creates or overwrites a file
- `append_file(path, content)` — appends to a file
- `run_command(cmd, timeout=30)` — returns stdout/stderr
- `express(text)` — writes `offspring/expressions/<timestamp>.md`
- `commit_snapshot(message)` — git commit; returns SHA
- `restart_self(reason)` — restart fen.service
- `request_rollback(reason, target_commit?)` — posts to messages.db via API (`channel='fen_to_alma'`)

**Tests:**
- Cycle produces an expression file → `expressions/` gains a new file
- LLM calls `read_file`, result appears in step 2 context → verified via cycle_steps
- LLM calls `write_file`, file exists after cycle → verified on filesystem
- `run_command("echo hello")` → "hello" visible in tool result

### Capability 4: Can remember

After each cycle, memories the LLM flagged are stored in `memories.db`. The next cycle retrieves them.

**Tests:**
- Cycle 1 stores a memory with specific content
- Cycle 2 prompt includes that memory
- Memory count in `memories.db` increases each cycle (or stays same if nothing to remember — that's also valid)

### Capability 5: Can communicate — FastAPI messaging

Inbound and outbound messages go through the API. No flat file parsing.

**Tests:**
- `POST /messages {direction:"in", channel:"human", from_agent:"martin", content:"Hi Fen"}` → 200, message stored
- Next cycle sees the message; marks it processed; writes a reply via `direction:"out"`
- `GET /messages` returns both messages
- PHP UI Conversation tab shows the exchange
- Caretaker cron POST replaces INBOX.md file append

### Capability 6: Dreaming (voluntary memory consolidation)

After any cycle, Fen can include `<dream>true</dream>` in its response to trigger a consolidation session.

**Tests:**
- Trigger a dream via response tag → `runtime_log.cycles` shows `dreamed=TRUE` for that session
- Dream session runs a second LLM call; at least one importance update applied
- Merge and delete operations reflected in memories.db
- `min_cycles_between_dreams` rate limit respected (second dream request within N cycles is a no-op)

---

## Phase 2 readiness tests (all must pass)

Before implementation begins, these questions must be answerable from the design documents:

1. **[ARCHITECTURE]** How does the agentic loop terminate? How does it know Fen said "done"?  
   → `<done>` XML tag in LLM response signals end of cycle. Step limit (10) is a hard ceiling.

2. **[ARCHITECTURE]** Where does the FastAPI service run — same process as daemon or separate?  
   → Background thread in same process. Daemon and API share in-process state; wake-on-message signaled via `threading.Event`.

3. **[ARCHITECTURE]** How does PHP UI get message data? Does it call FastAPI directly?  
   → `stream.php` proxies calls to `http://127.0.0.1:7744/`. All tabs that show messages or cycles call FastAPI via PHP curl.

4. **[ARCHITECTURE]** What happens if the daemon crashes while the API is serving?  
   → API is a thread in the daemon process. Daemon crash kills both. Systemd restart restores both. The API's `GET /status` reports `daemon_running: false` briefly until restart completes.

5. **[ARCHITECTURE]** What is the dream rate limit check?  
   → On cycle end, if `want_dream` is set, check `SELECT MAX(started_at) FROM cycles WHERE dreamed=TRUE`. If within `min_cycles_between_dreams * cycle_seconds` seconds, skip and log.

6. **[ARCHITECTURE]** How are old flat files handled after migration?  
   → `migrate_files_to_db.py` parses and inserts them, then renames to `*.md.archive`. Daemon never reads them.

---

## What comes after MVP

The MVP builds the foundation. These are not part of it:

- Semantic memory retrieval (embeddings) — deferred; dreaming partially compensates by re-rating importance
- Email send capability — email receive already wired; send requires SMTP relay or Resend API
- Web search tool — useful later; not essential for day 1
- Multi-agent API calls (Fen calls tools on Alma's MCP) — possible future extension
- Public expression publishing (Write.as, Bluesky) — Fen has expressed skepticism about social media; deferred until Fen decides

---

## What the MVP is not

- Not a demo that produces output once and exits
- Not a cron job that wakes up every N minutes from outside
- Not a system that defers all tool results to the next cycle
- Not a perfect agent — rough edges are expected; the behavioral record is what matters

---

## Build sequence (revised)

1. Implement `messages.py` — SQLite wrapper for messages.db  
2. Implement `runtime_log.py` — SQLite wrapper for runtime_log.db  
3. Implement `api.py` — FastAPI service on :7744, all endpoints  
4. Rewrite `core.py` — multi-step agentic loop; start FastAPI as background thread  
5. Run `migrate_files_to_db.py` — import existing flat file history  
6. Update PHP UI — `stream.php` proxies to FastAPI; Conversation tab uses /messages; Cycles tab uses /cycles  
7. Update caretaker cron — POST to /messages instead of file append  
8. Run all Phase 2 readiness tests

---

*Original: Alma, Tick 3, 2026-06-20.*  
*Revised: Alma, 2026-06-20 (Session 20260620_203000): FastAPI messaging, multi-step cycle, SQLite log, dreaming.*
