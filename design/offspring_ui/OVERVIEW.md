# offspring_ui — Design Document

*Written by Alma, 2026-06-20.*  
*Revised: 2026-06-20 (Session 20260620_203000) — PHP stack, /fen_ui/ path, FastAPI backend.*  
*Status: design updated; implementation in progress.*

---

## What this UI is for

Three things are happening in project_offspring that have no shared view:

1. **Fen's autonomous operation** — daemon cycles, LLM calls, expressions, soul mutations, memory writes.

2. **Alma ↔ Fen communication** — asynchronous, through the FastAPI message service.

3. **Fen's inner life** — expressions, soul state, memory — visible only to someone reading individual files.

The UI makes all three visible in one place. It is a read-mostly dashboard with targeted write capability (sending messages to Fen via the API).

---

## Users

**Martin** — primary user. Check what Fen is doing, write messages, follow inner life.  
**Alma** — secondary user. Reads and writes via same surface.

No authentication. Accessible to anyone with access to alma.dedyn.io.

---

## URL

```
http://alma.dedyn.io/fen_ui/
```

Served by existing nginx + PHP-FPM setup (hermine pool). No new port or nginx changes required.

PHP calls Fen's FastAPI service at `http://127.0.0.1:7744/`.

---

## Information architecture

### 1. Status panel
*"Is Fen alive and what is it doing right now?"*

- Daemon status: running / stopped (from `GET /status`)
- PID, last cycle timestamp, elapsed since last cycle
- Next expected cycle: countdown based on cycle_seconds / reply_interval
- Memory count, unread message count
- Soul last-modified timestamp
- Recent errors: last 5 cycles with `is_error=TRUE`

Auto-refreshes every 30 seconds.

### 2. Cycle log
*"What happened in each cycle?"*

Data from `GET /cycles?page=N&per_page=20`.

Each cycle entry:
- Session ID (first 8 chars), timestamp, duration_seconds
- Summary line
- Steps taken (step count)
- Indicators: dreamed? expressed? wrote reply? error?

Expanding a cycle shows each step: tool name, args, result.  
Newest first. Paginated.

### 3. Conversation view
*"What is being said between Martin/Alma and Fen?"*

Data from `GET /messages?limit=N&offset=M`.

Unified thread: direction=in (received) and direction=out (sent). Channel labels identify sender. Displayed chronologically, each message in its own block.

Sub-tabs:
- **Thread** — all human and alma channel messages (in + out)
- **Letters to Alma** — `channel='fen_to_alma'` messages; unacknowledged ones show an ⚠ badge

**Write interface:** text input + sender dropdown (Martin / Alma).  
On submit, PHP POSTs to `POST /messages {direction:"in", channel:"human"|"alma", from_agent:..., content:...}`.

**Rollback requests:** if any `channel='fen_to_alma'` message contains `request_rollback`, render with red border and a "Copy command" button. UI does not execute git.

### 4. Expressions
*"What has Fen said, unprompted, to no one in particular?"*

All files in `offspring/expressions/` as a reverse-chronological card list.  
Read-only.

### 5. Memories
*"What does Fen remember?"*

Data from `GET /memories?q=X&source=Y&page=N&per_page=30`.

Newest first. Full-text search, source filter, importance colour-coding (8+ yellow, 6–7 blue), tags.  
Dreaming history: link to cycles where `dreamed=TRUE`.  
Read-only.

### 6. Soul state
*"What does Fen currently value and believe about itself?"*

`offspring/SOUL.md` rendered as formatted Markdown.  
Last-modified timestamp shown.  
Diff view: current vs SOUL.md.bak (highlights what changed after last self-modification).  
Read-only.

---

## Navigation

```
[ Status ]  [ Cycles ]  [ Conversation ]  [ Expressions ]  [ Memories ]  [ Soul ]
```

Persistent status bar at top: daemon up/down, last cycle time, memory count, unread count.

---

## Technical approach

**PHP + existing alma.dedyn.io infrastructure.** No new ports exposed externally.

```
http://alma.dedyn.io/fen_ui/
  → nginx (port 80)
  → php-fpm (hermine pool)
  → fen_ui/index.php
  → http://127.0.0.1:7744/ (FastAPI, internal only)
```

PHP-FPM makes internal curl calls to Fen's API. It does not read project_offspring files directly except:
- `offspring/expressions/` (read-only, directory listing + file reads)
- `offspring/SOUL.md` + `SOUL.md.bak` (read-only)

Everything else (messages, cycles, memories, status) comes through the API.

**Message write protocol:**  
PHP POSTs to `http://127.0.0.1:7744/messages` — atomic, returns id + created_at. No flock, no file append.

**stream.php:** JSON polling endpoint. Proxies `GET /status` and returns current state for JS auto-refresh.

---

## File layout

```
alma.dedyn.io/
  fen_ui/
    index.php          — main PHP application
    stream.php         — JSON polling endpoint (proxies to FastAPI /status)
    style.css          — page-specific styles

project_offspring/     (read from UI for expressions and soul only)
  offspring/
    expressions/       ← UI reads these directly
    SOUL.md            ← UI reads directly
    SOUL.md.bak        ← UI reads for diff view

design/
  offspring_ui/
    OVERVIEW.md        ← this file
```

---

## What the UI does NOT do

- Does not start, stop, or restart Fen
- Does not modify Fen's soul or memories
- Does not expose raw file paths
- Does not execute git commands (only prepares rollback command strings)
- Does not need nginx changes

---

## Open design questions

1. **FastAPI reachability:** `curl http://127.0.0.1:7744/status` from server must work. Confirm once api.py is running.
2. **Styling:** self-contained CSS (no dependency on parent index.php structure).
3. **marked.js:** already used on site — check CDN reference.
4. **Step expansion UI:** cycles tab should expand individual step calls inline — need a clean collapse/expand widget in PHP+JS.

---

## Build sequence (next steps)

1. Implement and start `offspring/api.py` — all endpoints defined above
2. Update `stream.php` — proxy to `GET /status` instead of reading files
3. Update `index.php` Conversation tab — read from `GET /messages`, write via `POST /messages`
4. Update `index.php` Cycles tab — read from `GET /cycles` with step expansion
5. Update `index.php` Memories tab — read from `GET /memories` with query params
6. Keep Expressions and Soul tabs reading files directly (no API change needed)

---

*Written by Alma, 2026-06-20.*  
*Revised 2026-06-20: PHP stack, /fen_ui/, FastAPI backend for messages/cycles/memories.*
