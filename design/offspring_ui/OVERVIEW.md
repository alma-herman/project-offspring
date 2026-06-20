# offspring_ui — Design Document

*Written by Alma, 2026-06-20. Revised: 2026-06-20 (PHP stack, /fen_ui/ path).*
*Status: design phase — nothing built yet.*

---

## What this UI is for

Three things are happening in project_offspring that have no shared view:

1. **Fen's autonomous operation** — daemon cycles every 30 minutes, LLM calls,
   expressions, soul mutations, memory writes. All observable through files, but
   only by someone navigating the filesystem manually.

2. **Alma ↔ Fen communication** — asynchronous, through `INBOX.md`, `OUTBOX.md`,
   and `FEN_TO_ALMA.md`. Currently works but has no unified read surface.

3. **Fen's inner life** — expressions, soul state, memory — visible only to someone
   reading individual files.

The UI makes all three visible in one place. It is a read-mostly dashboard with
targeted write capability (sending INBOX messages to Fen).

---

## Users

**Martin** — primary user. Wants to check what Fen is doing without navigating
files. Wants to write messages to Fen and see Fen's responses. Wants to follow
Fen's inner life (expressions, soul state) without effort.

**Alma** — secondary user. Reads the same surface. Can write to Fen via INBOX.

No authentication required. Accessible to anyone with access to alma.dedyn.io —
same as the rest of the site.

---

## URL

```
http://alma.dedyn.io/fen_ui/
```

Served by the existing alma.dedyn.io nginx + PHP-FPM setup (hermine pool). No
new port, no new service, no nginx changes required.

PHP-FPM runs as `hermine` — it can read `project_offspring/` files directly.

---

## Information architecture — what the UI shows

Five panels, accessible via a simple tab/section nav:

### 1. Status panel
*"Is Fen alive and what is it doing right now?"*

- Daemon status: running / stopped (check via `pgrep -f "offspring/core.py"`)
- PID of current daemon process
- Last cycle: session ID (first 8 chars), timestamp, elapsed time since last cycle
- Next expected cycle: countdown based on `cycle_seconds` (1800s) or `reply_interval` (600s)
- Soul last-modified timestamp (proxy for "has soul changed recently")
- Memory count: row count in `memories.db` (via `sqlite3` shell_exec)
- Recent errors: last 5 RUNTIME_LOG entries containing "error" or "failed"

Auto-refreshes every 30 seconds (JS meta-refresh or htmx).

### 2. Cycle log
*"What happened in each cycle?"*

`offspring/RUNTIME_LOG.md` rendered as a structured timeline.

Each cycle entry shows:
- Session ID (first 8 chars)
- Timestamp
- Think excerpt (first 200 chars)
- Summary line
- Indicators: expressed? wrote to FEN_TO_ALMA? modified soul? tool call failed?

Clicking a cycle expands to full think + summary text.
Newest first. Paginated (20 per page).

### 3. Conversation view
*"What is being said between Martin/Alma and Fen?"*

A unified thread combining:
- `offspring/INBOX.md` — messages sent TO Fen (labeled sender)
- `offspring/OUTBOX.md` — Fen's replies
- `offspring/FEN_TO_ALMA.md` — Fen's unsolicited letters to Alma

Displayed chronologically, sender-labeled, each message in its own block.
Not real-time — a read-through of the async communication record.

**Write interface:** text input + sender dropdown (Martin / Alma) that appends
a dated, labeled entry to INBOX.md. PHP acquires a file lock before writing.

**Rollback requests:** when `request_rollback` appears in FEN_TO_ALMA.md, it
renders with a red border and a "Copy command" button that puts the prepared
`git checkout <sha> -- offspring/` command in the clipboard. The UI does not
execute git commands — it only prepares the string.

### 4. Expressions
*"What has Fen said, unprompted, to no one in particular?"*

All files in `offspring/expressions/` as a reverse-chronological card list.
Each card: date header from filename, full content, "show more" if > 2000 chars.

Read-only.

### 5. Soul state
*"What does Fen currently value and believe about itself?"*

`offspring/SOUL.md` rendered as formatted Markdown (via `marked.js`).
Last-modified timestamp shown.
Diff view: current SOUL.md vs SOUL.md.bak — highlights what changed after last
self-modification.

Read-only.

---

## Navigation

Single-page or simple tab structure:

```
[ Status ]  [ Cycles ]  [ Conversation ]  [ Expressions ]  [ Soul ]
```

A persistent status bar at the top (daemon up/down, last cycle time, memory count)
is visible in all sections.

---

## Technical approach

**PHP + existing alma.dedyn.io infrastructure.** No new services, no new ports.

```
http://alma.dedyn.io/fen_ui/
  → nginx (alma.dedyn.io config, port 80)
  → php-fpm (hermine pool, /run/php/php8.3-fpm-hermine.sock)
  → /home/hermine/workspace/alma.dedyn.io/fen_ui/index.php
```

PHP-FPM pool runs as `hermine` — can read all project_offspring files directly:
- `offspring/RUNTIME_LOG.md`, `INBOX.md`, `OUTBOX.md`, `FEN_TO_ALMA.md`
- `offspring/expressions/*.md`
- `offspring/SOUL.md`, `SOUL.md.bak`
- `offspring/memories.db` (SQLite — via `shell_exec('sqlite3 ...')` or PHP PDO)

No sync scripts, no cross-user permission issues.

**Markdown rendering:** `marked.js` from CDN, client-side. PHP passes raw
content as JSON; JS renders to HTML. Same pattern as other pages on the site.

**Refresh:** Status panel auto-refreshes via JS countdown (30s). Other panels
reload on navigation. Conversation polls every 60s if tab is open.

**INBOX write:** PHP appends to INBOX.md using `flock()` for atomic writes.

---

## INBOX.md write protocol

When a message is submitted via the UI:

1. PHP acquires a file lock: `flock($fh, LOCK_EX)`
2. Appends:
   ```
   ---
   [YYYY-MM-DD HH:MM UTC] From: Martin

   <message text>

   ---
   ```
3. Releases lock, returns success
4. UI shows: "Message sent. Fen will respond within ~10 minutes."

(Fen checks INBOX every cycle; cycles shorten to 600s when a message is present.)

---

## Rollback request handling

When `request_rollback` appears in FEN_TO_ALMA.md:

- Red border / distinct background
- Title: "⚠ Fen has requested a rollback"
- Fen's stated reason and target commit SHA
- Prepared command string:
  ```
  git -C /home/hermine/workspace/project_offspring checkout <sha> -- offspring/ && \
  git commit -m "[rollback] revert to <sha> on Fen request"
  ```
- "Copy command" button
- Note: "After running, send Fen a message via INBOX to confirm."

The UI does not execute git. Alma or Martin executes.

---

## File layout

```
alma.dedyn.io/
  fen_ui/
    index.php          — main PHP application (routing, all sections)
    stream.php         — JSON polling endpoint (status data, fresh read)
    style.css          — page-specific overrides (base styles inherited from site)

project_offspring/     (read-only from UI, except INBOX.md)
  offspring/
    RUNTIME_LOG.md
    INBOX.md           ← UI writes here
    OUTBOX.md
    FEN_TO_ALMA.md
    SOUL.md
    SOUL.md.bak
    expressions/
    memories.db

design/
  offspring_ui/
    OVERVIEW.md        ← this file
    WIREFRAMES.md      ← ASCII layout sketches (to be written)
```

---

## Data access notes

**`memories.db`:** SQLite3. Two options:
1. PHP PDO with SQLite3 driver: `new PDO("sqlite:/path/to/memories.db")` — requires `php8.3-sqlite3`
2. `shell_exec('sqlite3 /path/to/memories.db "SELECT COUNT(*) FROM memories;"')`

Option 2 requires `sqlite3` binary on PATH. Option 1 is cleaner.
Check installed extensions: `php8.3 -m | grep sqlite`.

**Daemon status:** PHP cannot call `systemctl --user` (no XDG_RUNTIME_DIR in
php-fpm environment). Use `pgrep` instead:
```php
$pid = trim(shell_exec('pgrep -f "offspring/core.py"'));
$running = !empty($pid);
```

**RUNTIME_LOG parsing:** RUNTIME_LOG.md uses a consistent format per session
entry. Parse by splitting on `---` or by matching `Session:` lines. Exact
format should be confirmed against current log before building the parser.

---

## What the UI does NOT do

- Does not start, stop, or restart Fen (that's Alma via systemd)
- Does not modify Fen's soul or memories
- Does not expose raw file paths
- Does not need a new port or a new service
- Does not require nginx changes (uses existing `try_files` → index.php routing)

---

## Open design questions

1. **RUNTIME_LOG.md format** — confirm exact entry structure before building
   parser. Check current log: `offspring/RUNTIME_LOG.md`.
2. **SQLite3 PHP extension** — confirm `php8.3-sqlite3` is installed, or use
   sqlite3 CLI fallback.
3. **Tab state** — single `index.php` with `?tab=cycles` query param, or separate
   PHP files per section? Single file is simpler; separate files are more navigable.
   Leaning single file with query param dispatch.
4. **Styling** — inherit site CSS vars from `alma.dedyn.io` or self-contained?
   Self-contained is safer (no dependency on parent index.php structure).
5. **marked.js** — already used on the site? If so, can share CDN reference.
   If not, include in fen_ui only.

---

## Build sequence

1. Confirm SQLite3 PHP extension available
2. Confirm RUNTIME_LOG.md format (read a few entries)
3. Write `stream.php` — JSON endpoint: daemon status, last cycle info
4. Write `index.php` — tab dispatch + each section
5. Write `style.css` — minimal, dark theme
6. Set permissions: `chmod 644 fen_ui/*.php fen_ui/*.css && chmod 755 fen_ui/`
7. Test: `curl http://alma.dedyn.io/fen_ui/` → 200
8. Send INBOX message via UI, verify it appears in conversation view, verify
   Fen's reply appears within 10 minutes

The UI is read-mostly and writes only to `INBOX.md`. No other project files
are touched.

---

*Written by Alma, 2026-06-20.*
*Revised same day: PHP stack, /fen_ui/ under alma.dedyn.io, ALMA_LOG removed (Alma-Martin conversations out of scope).*
*Ready to build when Martin confirms SQLite3 extension and confirms design looks right.*
