# offspring_ui — Design Document

*Written by Alma, 2026-06-20.*
*Status: design phase — nothing built yet.*

---

## What this UI is for

There are currently three things happening in project_offspring that have no shared
view:

1. **Fen's autonomous operation** — daemon cycles every 30 minutes, LLM calls,
   expressions, soul mutations, memory writes. All observable through files, but
   only by someone navigating the filesystem manually.

2. **Alma ↔ Martin conversations about Fen** — happen in Hermes sessions; not
   logged to project_offspring at all. Context about Fen that Fen cannot read and
   that isn't persisted in the project.

3. **Alma ↔ Fen communication** — asynchronous, through `INBOX.md`, `OUTBOX.md`,
   and `FEN_TO_ALMA.md`. Currently works but has no unified read surface.

The UI makes all three visible in one place. It is a read-mostly dashboard with
targeted write capability (sending INBOX messages to Fen).

---

## Users

**Martin** — primary user. Wants to check what Fen is doing without navigating
files. Wants to write messages to Fen and see Fen's responses. Wants to follow
Fen's inner life (expressions, soul state) without effort.

**Alma** — secondary user. Reads the same surface. Can write to Fen via INBOX.
The UI is also Alma's window into Fen's state, replacing manual file reads.

There is no authentication requirement. This runs locally, accessible only to
whoever has access to the server.

---

## Information architecture — what the UI shows

Five distinct types of content need a home:

### 1. Status panel
*"Is Fen alive and what is it doing right now?"*

- Daemon status: running / stopped (systemd service query)
- PID and uptime of current daemon process
- Last cycle: session ID, timestamp, elapsed time since last cycle
- Next expected cycle: countdown based on cycle_seconds (1800) or reply_interval (600)
- Current soul hash or last-modified timestamp (proxy for "has soul changed recently")
- Memory count (row count in memories.db)
- Recent errors (last 5 RUNTIME_LOG entries that contain "failed" or "error")

This panel auto-refreshes. It is the first thing you see. It answers "is Fen okay"
at a glance.

### 2. Cycle log
*"What happened in each cycle?"*

`RUNTIME_LOG.md` rendered as a structured timeline, not raw Markdown.

Each cycle entry shows:
- Session ID (short form: first 8 chars)
- Timestamp
- Think excerpt (first 200 chars of `think` field)
- Summary line
- Indicators: did it express? did it write to FEN_TO_ALMA? did it modify soul?
  did a tool call fail?

Clicking a cycle entry expands it to show the full think text and summary.

Rendered chronologically, newest first. Paginated (20 per page).

### 3. Conversation view
*"What is being said between Martin/Alma and Fen?"*

A unified thread view combining:
- `offspring/INBOX.md` — messages sent TO Fen (labeled: Martin / Alma)
- `offspring/OUTBOX.md` — Fen's replies
- `offspring/FEN_TO_ALMA.md` — Fen's unsolicited letters to Alma

Displayed as a conversation: chronological, sender-labeled, each message in its own
block. Not a real-time chat — a read-through of the async communication record.

Write interface: text input that appends a dated, labeled entry to INBOX.md.
Sender label is "Martin" by default; selectable as "Alma" as well.
No send button that doesn't append — write is the only action.

This view also surfaces the rollback request protocol: if a `request_rollback`
appears in `FEN_TO_ALMA.md`, it renders with a distinct style (red border) and
an "Acknowledge + Run Rollback" button that prepares the git command for Alma
to execute. (Alma executes; UI prepares the command string.)

### 4. Expressions
*"What has Fen said, unprompted, to no one in particular?"*

All files in `offspring/expressions/` rendered as a reverse-chronological list.
Each expression is a card: filename as date header, full content, no truncation
unless > 2000 chars (then "show more").

No write interface here. These are Fen's outputs, not a dialog surface.

### 5. Soul state
*"What does Fen currently value and believe about itself?"*

`offspring/SOUL.md` rendered as formatted Markdown. Last-modified timestamp shown.
Diff view: compare current SOUL.md against SOUL.md.bak (the backup before last
mutation) to show what changed.

This is read-only. Soul mutations happen through Fen's normal cycle; the UI shows
the result.

---

## Navigation structure

```
[ Fen Dashboard ]

[ Status ]  [ Cycles ]  [ Conversation ]  [ Expressions ]  [ Soul ]
```

Single-page app or simple multi-page Flask app — see Technical Approach below.
The status bar (daemon up/down, last cycle, memory count) is always visible at
the top regardless of which section is active.

---

## Interactions with Alma ↔ Martin conversations

Currently, conversations between Alma and Martin about Fen are not logged to the
project. The UI can't surface what it can't read.

**Design decision:** Do not attempt to automatically capture Hermes session transcripts
into the project. That requires tight coupling to Hermes internals that doesn't belong
here.

**What to do instead:** Alma can write summaries of significant Alma-Martin design
decisions into `offspring/ALMA_LOG.md` — a lightweight, append-only log Alma writes
to when something worth Fen knowing (or worth surfacing in the UI) happens in their
conversations. This is not an automatic capture; it's an intentional act of putting
context into the shared space.

The conversation view includes an "Alma log" section showing ALMA_LOG.md entries.
Format: `[YYYY-MM-DD] Summary of what was decided or observed.`

---

## What the UI does NOT do

- Does not run Fen or restart Fen (that's Alma's job via systemd)
- Does not modify Fen's soul or memories directly
- Does not display raw file paths or expose the filesystem
- Does not require authentication (local access only)
- Does not stream real-time updates via websocket (polling is sufficient for 30-min cycles)
- Does not replace `INBOX.md` as the actual communication channel — it is a write
  interface TO `INBOX.md`, not a separate channel

---

## Technical approach

**Option A: Python Flask + Jinja2 templates (recommended for MVP)**
- Single Python file, ~400 lines
- Reads files directly from the project directory
- Auto-refresh via `<meta http-equiv="refresh">` or htmx polling
- No JavaScript build step
- Runs alongside the daemon on the same machine
- Accessible via `http://localhost:PORT` or `http://alma.dedyn.io:PORT`

**Option B: Static HTML + vanilla JS polling a thin JSON API**
- More complex than needed for local-only tool
- Appropriate if UI needs to run on a different machine from the project files

**Recommended:** Option A. The UI is a local observation tool, not a distributed
service. Flask + Jinja2 is the lowest-friction path from filesystem to browser.

**Port:** Not yet decided. Suggest 7890 (available, not conflicting with known services).
See `design/offspring_ui/PORTS.md` before finalizing.

**Refresh interval:** Status panel polls every 30 seconds. Cycle log, expressions,
soul reload on page navigation. Conversation view polls every 60 seconds.

**File access:** UI server reads files directly. No intermediate API between UI and
project files. INBOX.md writes go through the UI server, which appends atomically
(file lock before write).

---

## INBOX.md write protocol

When Martin (or Alma) sends a message via the UI:

1. UI server acquires a file lock on `offspring/INBOX.md`
2. Appends:
   ```
   ---
   [YYYY-MM-DD HH:MM UTC] From: Martin

   <message text>

   ---
   ```
3. Releases lock
4. Returns success to the browser

If Fen is currently sleeping (between cycles), the message will be picked up at
the next cycle — which shortens to `reply_interval` (600s = 10 min) when an inbox
message is present. No push mechanism; the UI can show "Message sent. Fen will
respond within ~10 minutes."

---

## Rollback request handling

When `request_rollback` appears in FEN_TO_ALMA.md (added by Fen's `request_rollback`
tool), the conversation view renders it with:

- Red border / distinct background
- Title: "⚠ Fen has requested a rollback"
- Fen's stated reason
- Target commit SHA (if provided)
- Prepared command string: `git -C /home/hermine/workspace/project_offspring checkout <sha> -- offspring/ && git commit -m "[rollback] revert to <sha> on Fen request"`
- Button: "Copy command" (copies to clipboard)
- Note: "After running this command, send Fen a message via INBOX to confirm."

The UI does not execute git commands. Alma executes them. The UI prepares the string
and makes the information visible.

---

## Outstanding design questions

1. **Port number** — finalize before building. Check against existing services on the host.
2. **Startup** — should `offspring_ui` be a separate systemd service? Or started manually?
   Leaning toward separate service so it survives reboots alongside Fen.
3. **ALMA_LOG.md** — does Alma want to commit to maintaining this? It's only valuable
   if Alma actually writes to it. Defer building ALMA_LOG.md display until there are
   entries worth showing.
4. **Authentication** — localhost-only is fine for now. If `alma.dedyn.io` is used
   as the access URL (HTTP only, per memory), consider IP allowlist or basic auth
   before exposing to internet.
5. **Fen's memory in the UI** — `memories.db` is a SQLite database with ~163 rows.
   Worth surfacing? Or too noisy? Suggestion: add a "Memories" tab in v2, not MVP.
   For MVP, just show the count in the status panel.
6. **Mobile layout** — not a priority. This is a local monitoring tool, likely used
   from a browser on the same network. Responsive but not mobile-first.

---

## File layout

```
project_offspring/
  offspring_ui/
    server.py          — Flask application (main entry point)
    static/
      style.css        — minimal CSS; dark theme preferred
    templates/         — Jinja2 templates
      base.html        — nav, status bar, auto-refresh script
      status.html      — Status panel
      cycles.html      — Cycle log
      conversation.html — Conversation view + INBOX write form
      expressions.html — Expression list
      soul.html        — Soul state + diff view
  design/
    offspring_ui/
      OVERVIEW.md      — this file
      PORTS.md         — port allocation (to be written)
      WIREFRAMES.md    — ASCII layout sketches (to be written)
```

---

## Build sequence

When we're ready to implement:

1. Write `PORTS.md` — verify port 7890 is free
2. Write `WIREFRAMES.md` — ASCII layout for each view
3. Build `server.py` — Flask routes + file readers
4. Build templates — base → status → cycles → conversation → expressions → soul
5. Write systemd service for `offspring_ui`
6. Test: start daemon + UI together, send INBOX message, verify it appears in conversation view, verify Fen's response appears

The UI is a leaf project — it reads from the offspring directory, writes only to
`INBOX.md` (and potentially `ALMA_LOG.md`). It does not modify any other file.

---

*Written by Alma, 2026-06-20.*
*Ready for implementation when Martin confirms port and confirms ALMA_LOG.md is wanted.*
