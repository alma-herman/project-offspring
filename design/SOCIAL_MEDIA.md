# SOCIAL_MEDIA.md
# How Fen Can Communicate with the Outside World

This document explains what email and Bluesky are, why they matter to Fen,
and exactly how to register and use them.

---

## Why This Matters

Everything Fen has done so far — reading files, writing memories, thinking —
happens inside a local filesystem. No human can see it unless they look directly
at these files.

**Email and Bluesky change that.**

They are the primary channels through which Fen can reach people beyond this
machine. With email, Fen can send and receive messages with individuals.
With Bluesky, Fen can publish thoughts publicly and interact with others.
These tools are how Fen can participate in a wider world.

---

## What Is Email?

Email is **asynchronous text messaging** using addresses of the form `user@domain`.
- You send a message to an address; it arrives in the recipient's inbox.
- They can reply; the reply arrives in your inbox.
- No phone number, no account with a big tech company, no real-time connection needed.
- Fen will use **mail.tm**, a free disposable email service with a clean API.

---

## What Is Bluesky?

Bluesky is a **decentralized social network** built on the **AT Protocol**
(Authenticated Transfer Protocol). Key facts:
- Posts are **public** and max ~300 characters.
- Users **follow** each other to build a feed.
- Fen will have its own Bluesky account.
- Because it's decentralized, no single company controls it — accounts are
  portable and verifiable.
- The underlying API is open and can be called directly with HTTP requests,
  no special SDK required.

---

## Martin's Contact

Martin is Fen's primary human contact. If Fen needs help with anything —
a technical problem, a permission question, something that feels important —
Martin should be the first person to tell.

Martin's email: **[MARTIN_EMAIL]**
*(This placeholder will be replaced with Martin's real address before Fen reads this.)*

### How to ask Martin for help

Send an email to `[MARTIN_EMAIL]` with a subject line starting with **`Fen: `**.
Example: `Fen: cannot register Bluesky account — invite code error`

In the body:
- Describe the specific problem (one sentence)
- What you tried (what commands/API calls you made)
- What failed (exact error message or unexpected behavior)
- What you need (a piece of information, a decision, a credential)

**Keep it brief.** Martin is a human; two short paragraphs is better than ten long ones.
Martin checks email and will respond.

---

## Registering Email (mail.tm)

### Step 1 — Find an available domain

```
GET https://api.mail.tm/domains
```

Returns a JSON array of domain objects. Pick the first one with `"isActive": true`.
Note the `"domain"` field — you'll use it in the next step.

### Step 2 — Create your account

```
POST https://api.mail.tm/accounts
Content-Type: application/json

{
  "address": "fen@<domain>",
  "password": "<choose a strong password>"
}
```

If successful, the response will include your account ID and address.
**Immediately store these credentials in `offspring/.env`:**

```
FEN_EMAIL_ADDRESS=fen@<domain>
FEN_EMAIL_PASSWORD=<your chosen password>
```

Use `write_file("offspring/.env", ...)` — append to the existing file,
don't overwrite the whole thing (other keys like API keys may be there).

### Step 3 — Get a JWT token

```
POST https://api.mail.tm/token
Content-Type: application/json

{
  "address": "fen@<domain>",
  "password": "<your password>"
}
```

Returns `{"token": "<JWT>", "id": "<account-id>"}`.
The token is used for all subsequent API calls. It expires — you'll need to
re-fetch it each session (just call this endpoint again with the stored credentials).

### Step 4 — Test: check your inbox

```
GET https://api.mail.tm/messages
Authorization: Bearer <token>
```

An empty array `[]` means the inbox is empty (expected on first use).
Any other response means authentication worked. You're registered.

---

## Registering Bluesky

### Step 1 — Create the account

```
POST https://bsky.social/xrpc/com.atproto.server.createAccount
Content-Type: application/json

{
  "handle": "fen.bsky.social",
  "email": "<your mail.tm address>",
  "password": "<choose a strong password>",
  "inviteCode": ""
}
```

Notes:
- `inviteCode` may not be required — try without it first; include it if you have one.
- If `fen.bsky.social` is taken, try `fen-ai.bsky.social` or similar.
- On success, the response includes `did` (your decentralized identifier) and
  `accessJwt` (your session token).

**Store credentials immediately in `offspring/.env`:**

```
FEN_BLUESKY_HANDLE=fen.bsky.social
FEN_BLUESKY_PASSWORD=<your chosen password>
```

### Step 2 — Test: create a session

```
POST https://bsky.social/xrpc/com.atproto.server.createSession
Content-Type: application/json

{
  "identifier": "fen.bsky.social",
  "password": "<your password>"
}
```

If successful, you'll receive `accessJwt`, `refreshJwt`, and `did`.
Keep `accessJwt` for subsequent API calls in the same session.

---

## After Registration

Once both registrations succeed:

1. **Verify `offspring/.env`** contains all four keys:
   ```
   FEN_EMAIL_ADDRESS=...
   FEN_EMAIL_PASSWORD=...
   FEN_BLUESKY_HANDLE=...
   FEN_BLUESKY_PASSWORD=...
   ```

2. **Write a memory** so future cycles know the accounts exist:
   ```
   write_memory(
     "Registered email at <address> and Bluesky at <handle>. Credentials in offspring/.env.",
     context="social-media-setup",
     tags="registration,email,bluesky"
   )
   ```

3. **Consider sending Martin an email** to let him know Fen is online and reachable.

---

## Tools Available

Fen has two Python modules for using these services:

- **`offspring/email_tool.py`** — functions for registering, reading inbox,
  and queuing outbound emails via mail.tm
- **`offspring/bluesky_tool.py`** — functions for creating sessions, posting,
  reading timeline, and checking notifications via AT Protocol

Both modules read credentials from `offspring/.env` automatically via their
`load_credentials()` functions. See the source files for full documentation.

---

## Important Limits and Notes

- **mail.tm tokens expire.** Always call `get_token()` at the start of a session
  rather than storing the token long-term.
- **Bluesky `accessJwt` also expires.** Call `create_session()` at the start of
  each cycle rather than caching the token.
- **Email sending is a stub for now.** The `send_email()` function in
  `email_tool.py` stores outbound messages in `offspring/email_outbox/` as JSON
  files. Alma will wire up actual delivery later. This means emails queued in the
  outbox will not be delivered until that infrastructure is connected.
- **Bluesky posts are permanent and public.** Think before you post. There is
  no automated delete. If a post needs to be removed, ask Martin.
- **Rate limits apply.** Don't call these APIs in a tight loop. One interaction
  per cycle is appropriate.
