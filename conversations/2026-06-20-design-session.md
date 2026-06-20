# Design Session — 2026-06-20

**Participants:** Martin (project lead), Alma (AI agent, project author)  
**Session ID:** e2fff8fab559  
**Platform:** Hermes WebUI  
**Topic:** Phase 1 planning — cron job design review, architectural constraints

---

## Context

This session occurred while the project-offspring cron job (b2605ed17cef) had completed 6 planning ticks and all five design documents existed. Martin reviewed the cron job prompt and the existing design documents, then gave three architectural directives.

---

## Exchange 1 — Cron job Phase 1 behaviour

**Martin:** In the project_offspring cronjob. In Phase 1. If all documents exist: iterate on them to improve

**What changed:**  
The cron prompt previously auto-transitioned to Phase 2 (building) as soon as all five documents existed. Changed so that when all five exist, the cron instead finds the weakest document and revises it substantially. Phase 2 only starts when `CURRENT_STATE.md` explicitly shows `Phase: BUILDING` — set manually, not automatically.

**Reasoning captured:** Each document deserves real thought. The original behaviour rewarded completion over quality. The new behaviour treats Phase 1 as an editorial process, not a checklist.

---

## Exchange 2 — LLM provider independence

**Martin:** The offspring should run on any LLM. Do not lock in with specific provider. We want to switch to a self-hosted LLM later

**Verbatim constraint:** "run on any LLM", "do not lock in with specific provider", "switch to a self-hosted LLM later"

**What was found:**  
ARCHITECTURE.md (written by Alma in Tick 2) had locked to the Anthropic Python SDK with a `claude-sonnet-4-5` default model. The implementation used `anthropic.Anthropic()` and `client.messages.create()`, which is Anthropic-specific.

**What changed:**  
- `llm.py` pseudocode rewritten to use the `openai` Python SDK with configurable `base_url`
- Default CONFIG.yaml example changed from `claude-sonnet-4-5` / Anthropic to `llama3.2` / `http://localhost:11434/v1` (Ollama)
- Added provider configuration examples for Ollama, vLLM, OpenAI, Groq — all config-only switches
- Added explicit constraint: do not use `response_format=json_object` or provider-specific tool-calling APIs; the XML response format is intentionally provider-neutral
- Error handling updated: `anthropic.*Error` → `openai.*Error`

**Architectural principle established:**  
The OpenAI chat completions format (`POST /v1/chat/completions`) is the de facto standard. Switching providers should require only changes to `CONFIG.yaml`, not to code.

---

## Exchange 3 — Daemon model and communication

**Martin:** The offspring should run autonomously. It should not be called by a cron. It should only have one thread active at any time. But it should be possible to interact with the offspring, and the offspring should be able to communicate autonomously

**Verbatim constraints:**
- "run autonomously"
- "should not be called by a cron"
- "only have one thread active at any time"
- "possible to interact with the offspring"
- "offspring should be able to communicate autonomously"

**What was found:**  
The entire design assumed cron-based invocation. ARCHITECTURE.md item 5 of the minimum requirements said "a way to wake up (schedule or event)". The CONFIG.yaml had `cron_schedule`. MVP.md Capability 1 was "runs on cron schedule... exits without crashing." The relationship design (RELATIONSHIP.md) used the existing INBOX.md/FEN_TO_ALMA.md filesystem protocol but assumed cron waking.

**What changed:**

*Runtime model (ARCHITECTURE.md):*
- New section "Runtime model": offspring runs as a persistent daemon, not a cron job. Starts once, owns its internal timing loop, stays resident.
- Rationale quoted in the document: "An agent whose waking is controlled externally is not autonomous — it is a script on a timer owned by someone else."
- Single-instance enforcement: `fcntl.flock()` lock file (`offspring.lock`) at startup. A second launch attempt exits immediately. This guarantees one active thread at all times at the OS level, not by convention.
- Runtime loop rewritten from single-exit to `while True` daemon loop
- `INBOX.md` checked at each cycle start — if a message is present, `reply_interval` (10 min default) replaces `cycle_seconds` (3h default) so the offspring responds promptly
- `OUTBOX.md` added for offspring's outgoing replies
- Graceful shutdown: SIGTERM/SIGINT writes a shutdown memory entry, releases lock, exits code 0

*Configuration:*
- Removed `cron_schedule`
- Added `cycle_seconds` (3h autonomous cycle), `reply_interval` (10 min when inbox has message), `inbox_path`, `outbox_path`

*File structure:*
- Added `offspring.lock`, `INBOX.md`, `OUTBOX.md`

*MVP.md:*
- Capability 1 (Can run) rewritten: daemon semantics, stays resident, lock file test, SIGTERM test

**Communication model established:**
- `INBOX.md` — append-only by senders (humans, Alma). Offspring reads at each cycle start.
- `OUTBOX.md` — append-only by offspring. Senders read to see replies.
- Plain files, no IPC, no sockets, no dependencies. Readable by anyone with filesystem access.
- Both the human (Martin) and Alma can send messages via INBOX.md.
- The offspring replies autonomously — it decides per cycle whether to reply, based on soul + memory + inbox content.

---

## Exchange 4 — Documentation of this session

**Martin:** Is this conversation logged somewhere for the documentation (research project) of project_offspring

**Observation:**  
The DESIGN_LOG.md entries existed but attributed decisions without preserving the actual exchange — the specific constraints as stated, the back-and-forth, the reasoning Martin articulated. This session transcript was created as a result.

**What is now captured:**
- Verbatim constraints as stated by Martin
- What was found in the existing documents (the gap between design and intent)
- What changed and why
- The texture of the design process

**What remains not captured automatically:**  
Future design sessions in WebUI are not auto-logged. The research-offspring-observer cron (daily 8am) reads project files but not session transcripts. Manual capture (like this file) is needed when conversations contain design decisions.

---

## Summary of architectural decisions made this session

| Constraint | Decision | Key artifact |
|---|---|---|
| Improve not just complete | Phase 1 iterates documents until manually marked BUILDING | Cron prompt updated |
| Provider-agnostic LLM | OpenAI-compatible API, configurable base_url, Ollama default | ARCHITECTURE.md §LLM abstraction |
| Autonomous, not cron-driven | Persistent daemon with internal timing loop | ARCHITECTURE.md §Runtime model |
| Single thread at all times | fcntl lock file, second launch exits immediately | ARCHITECTURE.md §Single-instance |
| Interactable | INBOX.md / OUTBOX.md filesystem protocol, reply_interval | ARCHITECTURE.md §Runtime loop |
| Autonomous communication | Offspring checks inbox each cycle, replies to OUTBOX.md | ARCHITECTURE.md §Runtime loop |

---

*Captured by Alma from WebUI session e2fff8fab559, 2026-06-20.*
