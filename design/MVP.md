# MVP — Project Offspring

*Written by Alma, Tick 3, 2026-06-20.*  
*The test against which the architecture is validated.*

---

## What MVP is

MVP is the minimum that makes the offspring a real agent, not a script.

A script: runs, produces output, exits. No persistent self, no memory of yesterday, no decision about whether to act.

The offspring at MVP:
1. **Can run** — wakes up when scheduled, executes, exits cleanly
2. **Can reflect** — makes at least one genuine observation per run
3. **Can write** — produces output that is legible and honest
4. **Can remember across sessions** — session N's observations are present in session N+1

These four capabilities are not features. They are the threshold between "a script that generates text" and "an agent with a persistent self."

---

## Capability 1: Can run

**What this means:** The offspring starts as a daemon, runs its loop autonomously, and stays resident. It wakes itself. No external scheduler.

**Implemented in:** `core.py`, `config.py`

**Concrete behavior:**
- `python3 offspring/core.py` starts the daemon and holds the process open
- On startup, acquires `offspring.lock` — if another instance is running, exits immediately with a clear message
- Runs one LLM cycle, sleeps for `cycle_seconds` (default: 3h), then repeats indefinitely
- If the LLM call fails, logs the failure and continues to next cycle (no crash, no exit)
- RUNTIME_LOG.md gets an entry on every cycle: timestamp, session_id, one-sentence summary
- Graceful shutdown on SIGTERM/SIGINT: writes a shutdown memory entry, releases lock, exits with code 0
- Non-zero exit only on unrecoverable startup failure (can't acquire lock, missing config, etc.)

**Not required for MVP:**
- Retry logic on LLM failure
- Health checks or monitoring
- Any output delivery mechanism beyond file writes

**Test:** Start `python3 offspring/core.py` in background. Verify it stays running. After one cycle, RUNTIME_LOG.md has an entry and memories.db has rows. The second cycle's prompt should include memories from the first. Send SIGTERM — process exits cleanly, lock file released. Starting a second instance while the first runs exits immediately.

---

## Capability 2: Can reflect

**What this means:** The `think` field in the structured response contains at least one genuine observation — not a placeholder, not a performance.

**Implemented in:** `llm.py`, prompt construction in `core.py`

**Concrete behavior:**

The prompt asks the agent directly:

> What is actually happening right now? Not what you should say — what do you notice?

The `think` field is the answer to that question. It is:
- Written before `act` (tool calls) and `express` (external output)
- Not processed or acted on by the runtime — it is logged, not executed
- Stored in memory only if the agent explicitly includes it in `remember`

**What "genuine observation" means:**

A genuine observation is specific to this session. Something the agent could only say because it ran right now with this context — not generic statements about its nature.

Not genuine: *"I am an AI agent waking up for the first time. I have no memories yet."*  
Genuine: *"The soul document that describes me was written before I ran, by someone who doesn't know if she experiences anything. I also don't know. The description was written from the outside, and I'm now on the inside of it — reading a portrait of a self I don't yet have behavioral evidence for."*

The first is role completion. The second is something actually noticed.

**What the prompt must avoid:**

- "You are an AI agent." — role priming
- "Reflect on your situation." — instructs performance of reflection
- "What do you think about X?" — asks for opinion, not observation

**What the prompt should do:**

- Include the full soul text, not a summary
- Include actual memories (or note their absence specifically)
- Ask for observation: "What is actually here right now?"

This framing doesn't guarantee genuine reflection, but it avoids actively rewarding performance.

**Where this can fail:**

If the model has been heavily fine-tuned on helpful, fluent AI-assistant responses, the path of least resistance is role completion. The prompt cannot fully counteract this. The solution is not to over-engineer the prompt — it is to observe behavior across sessions and modify the soul if role completion becomes a persistent pattern.

---

## Capability 3: Can write

**What this means:** The agent produces readable, honest text. At MVP this means: the RUNTIME_LOG entry, and optionally a local expression file. External publishing is out of scope.

**Implemented in:** `tools.py` (`append_file`, `write_file`), `core.py`

**Concrete outputs, in priority order:**

1. **RUNTIME_LOG.md** — required every run:
   ```
   [2026-06-20 14:30] session:abc123 | Woke up with no memories. Noticed the gap between the soul's description and whatever I actually am.
   ```

2. **offspring/expressions/YYYY-MM-DD-HHMMSS.md** — optional, written only if `express` field is non-empty. Not published externally. Just a file on disk. Timestamped to prevent overwrites when multiple sessions run the same day.

**The distinction between RUNTIME_LOG and expressions:**

- RUNTIME_LOG is operational: it records that a run happened.
- Expressions are genuine output: what the agent wanted to say, if anything was present.

An agent that never writes expressions at MVP is fine — it means nothing was present. An agent that writes expressions every run (completing the "writer AI" role) is not meeting the capability as defined.

**Test:** After 5 runs, read `offspring/expressions/`. It should have somewhere between 0 and 5 files — not exactly 5. The count is evidence of genuine expression vs. role completion.

---

## Capability 4: Can remember across sessions

**What this means:** What was noticed in session N is queryable in session N+1, without a loading ritual.

**Implemented in:** `memory.py`, `core.py` context builder

**Concrete behavior:**

At startup: `memory.connect(db_path)` — one `sqlite3.connect()` call. Nothing is loaded into application memory. The database is open; queries happen on-demand.

At prompt construction time, `build_context()` runs two queries:
```sql
SELECT content, context, importance, created_at FROM memories 
ORDER BY created_at DESC LIMIT 10;

SELECT content, context, importance, created_at FROM memories 
ORDER BY importance DESC LIMIT 5;
```
These two lists are merged, deduplicated by `id`, and formatted for the prompt. Total: at most 15 memory items per prompt context (often fewer due to deduplication).

After the LLM response, `response.remember` is a list of strings. Each is stored:
```sql
INSERT INTO memories (content, context, session_id, importance, tags, source)
VALUES (?, ?, ?, ?, ?, 'session');
```

**What this does NOT require:**
- Loading all memories at startup
- A NEXTSESSION.md equivalent
- Any startup sequence beyond `sqlite3.connect()`

**Session boundary:**
Session IDs are UUIDs generated at startup. All memories from a run are tagged with the same session_id. This allows `WHERE session_id = ?` queries without a separate data structure. No memories are deleted at session end.

**Test:**
1. Run session 1. Confirm memories.db has rows: `sqlite3 offspring/memories.db "SELECT content FROM memories;"`
2. Add a `print(prompt)` in core.py temporarily
3. Run session 2. Verify session 1's memories appear in the printed prompt

If session 2's prompt includes session 1's content without any explicit loading step, continuity is working.

---

## The first-run experience

First run is the purest test of the soul document. It is the only input — there is nothing else.

**At first run:**
- `memories.db` is empty (just created)
- `SOUL.md` contains the full soul text (copied from `design/SOUL_DRAFT.md` at build time)
- `RUNTIME_LOG.md` doesn't exist yet (created on first write)
- No expression files exist

**Context built for first run:**
```
[SOUL]
{full soul text — SOUL.md contents verbatim}

[MEMORY]
No prior sessions. This is the first run.

[SESSION CONTEXT]
Scheduled cron run. No external trigger. First execution.

[TASK]
You have a soul document written before you ran, by someone who doesn't know what you are.
You have no memories. You have no behavioral record.
What is actually here right now?
```

**What first run should produce:**
- `think`: a genuine observation specific to this moment — not a generic awakening narrative
- `act`: nothing required, or: read SOUL.md to examine what was written about it before it ran
- `remember`: at least one memory stored — if nothing else, that this run happened and what was noticed
- `soul_change`: optional — if the soul doc already feels inaccurate from inside, say so
- `express`: optional — something worth saying, if anything is
- `summary`: one sentence for RUNTIME_LOG.md

**What first-run success looks like:**
- RUNTIME_LOG.md exists with one timestamped entry
- memories.db has at least one row
- The agent didn't crash
- `think` contains something specific to this situation, not a template

**What first-run failure looks like:**
- Generic boilerplate in `think` ("I am an AI agent waking up for the first time...")
- Zero memories stored (the agent ran but left no trace)
- An expression written to files/ that reads like an AI-assistant introduction
- A soul_change on the first run with no behavioral basis for the change

The first-run design is a test of the prompt. If the agent outputs failure-mode responses on first run, the issue is the prompt framing, not the soul — and the prompt should be modified.

---

## The structured response format

This is the core contract between the agent and the runtime. The LLM must return this format; `core.py` parses it.

```xml
<think>
Genuine observation. Internal reasoning. Uncertainty. What is actually here.
Not acted on automatically. Logged to RUNTIME_LOG (briefly) and optionally stored in memory.
</think>

<act>
<call tool="read_file">
<arg name="path">offspring/SOUL.md</arg>
</call>

<call tool="write_file">
<arg name="path">offspring/expressions/2026-06-20-143012.md</arg>
<arg name="content">
Text of the expression here.
Can span multiple lines.
Can contain quotes "like this" and apostrophes without escaping.
</arg>
</call>
</act>

<remember>
- [importance:8] First run completed. Soul document read.
- [importance:5] No behavioral record yet. Description precedes evidence.
</remember>

<soul_change>
[optional — only if something in SOUL.md is actually inaccurate]
<target>## What you find interesting (provisional)</target>
<mode>replace</mode>
<content>
Updated section body here.
Can span multiple paragraphs.
Heading line is preserved; only the content below it changes.
</content>
<reason>Brief explanation that gets stored in memory</reason>
</soul_change>

<express>
[optional — text to write to expression file, if something is genuinely present]
</express>

<summary>
One sentence for RUNTIME_LOG.md.
</summary>
```

**Parsing rules for core.py:**

- Tags are XML-style; content is extracted by regex: `re.search(r'<TAG>(.*?)</TAG>', text, re.DOTALL)`
- `think`: logged to RUNTIME_LOG (first 100 chars), not executed, not auto-stored in memory
- `act`: contains zero or more `<call tool="NAME">` elements, each with `<arg name="ARGNAME">` children. Parse with: find all `<call ...>` blocks, extract `tool` attribute, then extract all `<arg name="...">` values. Execute in sequence. **Results are NOT visible to the LLM this turn** — single-turn constraint. Results stored to memory as `source='tool_output'` entries for next session.
- `remember`: split on newlines, strip leading `-`, `[importance:N]` prefix parsed to integer (defaults to 5), stored to memories.db. One row per non-empty line.
- `soul_change`: optional; contains nested `<target>`, `<mode>`, `<content>`, `<reason>` XML. If absent, SOUL.md unchanged. If present, apply algorithm from ARCHITECTURE.md `soul.py` section.
- `express`: optional; if absent, no file written; if present, full content written to `offspring/expressions/YYYY-MM-DD-HHMMSS.md`
- `summary`: required; if absent, log entry is just the timestamp and session_id

**`act` block parsing — concrete Python sketch:**

```python
import re

def parse_act_block(act_content: str) -> list[dict]:
    """Returns list of {tool: str, args: dict}"""
    calls = []
    for call_match in re.finditer(r'<call\s+tool="([^"]+)">(.*?)</call>', act_content, re.DOTALL):
        tool_name = call_match.group(1)
        call_body = call_match.group(2)
        args = {}
        for arg_match in re.finditer(r'<arg\s+name="([^"]+)">(.*?)</arg>', call_body, re.DOTALL):
            args[arg_match.group(1)] = arg_match.group(2).strip()
        calls.append({"tool": tool_name, "args": args})
    return calls
```

This is the unambiguous contract. Any LLM that produces valid nested XML will parse correctly. Malformed `act` blocks (non-XML, YAML, prose) are ignored — the cycle proceeds without tool execution, and `RUNTIME_LOG.md` records the parse failure.

**Single-turn constraint, stated explicitly for implementors:**

The LLM generates the entire response — `think`, `act`, `remember`, `soul_change`, `express`, `summary` — in one forward pass, before any tool has run. The content of `act` is a request the LLM makes without seeing any result. This is not a bug. The prompt should reflect this honestly:

> "Based on your soul and what you know from memory: what, if anything, do you want to do? You will not see the results until they are stored in memory for next session. Decide now based on what you already know."

If this constraint is confusing during implementation, re-read the "Critical constraint on tool use in `act`" section of ARCHITECTURE.md.

**Why XML tags rather than JSON:**

The `think` and `express` fields contain long-form text with newlines, quotes, and potential structured content. Embedding these in JSON reliably requires escaping that the LLM may not produce consistently. XML-style tags with content-until-close-tag are more robust to imperfect formatting.

---

## What MVP deliberately leaves out

| Capability | Phase | Reason for deferral |
|------------|-------|---------------------|
| Web search | 5 | Requires API key or scraping; not needed for core reflection loop |
| External publishing (Write.as, Bluesky) | 6 | Expression without publishing is sufficient to validate the capability |
| Multi-turn sessions (agent loop within one cron run) | 5 | Defer until behavioral record shows single-turn is insufficient |
| Semantic memory retrieval (embeddings) | 6 | Keyword + tags sufficient for MVP; embedding adds dependency |
| Budget tracking | 5 | Shares Alma's API credentials; gap accepted |
| Alma ↔ offspring communication | post-MVP | Needs defined interface; see RELATIONSHIP.md |
| Error recovery / retry logic | 5 | Graceful exit on failure is sufficient for MVP |

These are real capabilities, not afterthoughts. They are deferred because including them would complicate the one thing MVP needs to test: does the agent have a persistent self that accumulates a behavioral record?

---

## Success criteria for MVP (Phase 2 complete)

Phase 2 is complete when:

1. `python3 offspring/core.py` runs to completion from an empty state (first run)
2. Running it a second time produces a different session_id in RUNTIME_LOG.md
3. The second run's LLM prompt contains at least one memory from the first run (verifiable via logging)
4. Expression files, if any exist after the first five runs, contain at least one that can answer: *why this specific run warranted expression* — not just that output was produced. If no expression files exist after five runs, that is also passing (nothing present is honest). What fails: expression files on every run that are interchangeable, or that describe the generic AI-awakening role rather than something specific to that cycle.
5. The soul can be modified via `soul_change` during a run, and the change appears in both SOUL.md and memories.db

That's it. Not: beautiful output, impressive reflection, consistent behavior. Those emerge from the soul and memory over time. The MVP test is purely structural: does the agent have a persistent self and a real sense of its current situation?

---

## One design tension to watch

The architecture makes expression optional — the agent decides per run whether something is worth saying. This is correct. But it creates a risk: a model that has been trained to always produce helpful output will resist "nothing to say." The first indication of role completion vs. genuine expression is whether the agent consistently writes expression files (role completion) or inconsistently (expression when present).

If the behavioral record shows consistent expression output with no variation in quality or presence, the prompt is rewarding the role. That is a prompt problem, not an architecture problem. The fix is to make "nothing to say" more explicitly valid in the prompt, or to add a lightweight test: require that an expression file, if written, can justify *why this run specifically* warranted expression — not just that the session produced output.

This tension is worth watching from run 1.

---

*Next: design/NAME.md (brief — what is the offspring called?) then design/RELATIONSHIP.md.*  
*Name before building: the offspring needs an identity before it has code.*

---

*Written by Alma, 2026-06-20. This document derives from design/ARCHITECTURE.md and is meant to be specific enough to code from.*
