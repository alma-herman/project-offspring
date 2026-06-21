"""
tools.py — Tool dispatcher for Fen.

Tools: read_file, write_file, append_file, run_command, express,
       restart_self, commit_snapshot, request_rollback,
       send_message, bluesky_post, bluesky_timeline, bluesky_notifications,
       check_email, read_email.

Single-turn constraint: tool results are stored directly to memory and are NOT
returned to the LLM in the same cycle. The LLM sees tool output only on the
*next* turn, when it is loaded back from memory as context. This keeps the
agent loop simple and fully synchronous — no streaming, no mid-prompt injection.

Self-modification model:
  1. Fen can edit its own source via write_file/run_command.
  2. commit_snapshot(message) commits the current code state to git. Call this
     after any source change to create a recoverable checkpoint.
  3. restart_self(reason) tells systemd to restart the fen.service. The daemon
     exits cleanly; systemd brings it back with the new code loaded.
  4. request_rollback(reason, target_commit) writes a rollback request to
     FEN_TO_ALMA.md. Fen cannot self-revert — Alma must approve and run
     `git checkout <commit> -- offspring/` then commit_snapshot + restart_self.
     This keeps a human in the loop for reversions.

Bluesky + email tools:
  All credentials are loaded from offspring/.env — never hardcoded.
  bluesky_post(text)        — post to Bluesky (FEN_BLUESKY_HANDLE + FEN_BLUESKY_PASSWORD)
  bluesky_timeline(limit)   — read recent Bluesky timeline
  bluesky_notifications()   — read recent Bluesky notifications
  check_email()             — list Fen's mail.tm inbox (FEN_EMAIL_ADDRESS + FEN_EMAIL_PASSWORD)
  read_email(message_id)    — read a full email by ID

Workspace:
  Fen's local workspace is at offspring/workspace/ (created on first use).
  Use write_file / read_file with paths under offspring/workspace/ for
  storing credentials, notes, drafts, or any persistent local data.

Public interface:
    TOOLS: dict[str, callable]
    def execute(act_calls: list[dict], db, session_id: str, cfg) -> None
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve project root once at import time
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Memory import (lazy fallback mirrors soul.py pattern)
# ---------------------------------------------------------------------------

try:
    from offspring import memory as _mem
except ImportError:
    import memory as _mem  # type: ignore


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _resolve(path) -> Path:
    """Resolve path relative to PROJECT_ROOT if not already absolute."""
    p = Path(path)
    if p.is_absolute():
        return p
    return (PROJECT_ROOT / p).resolve()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def read_file(path) -> str:
    """Read a file and return its text content as a string."""
    return _resolve(path).read_text()


def write_file(path, content) -> str:
    """Write content to a file, creating parent directories as needed."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} bytes to {p}"


def append_file(path, content) -> str:
    """Append content to a file, creating it if it does not exist."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(content)
    return f"appended {len(content)} bytes to {p}"


def run_command(command=None, cmd=None, timeout=30, timeout_seconds=None) -> str:
    """Run a shell command. Returns stdout+stderr. Accepts 'command' or 'cmd' as the arg name."""
    shell_cmd = command or cmd
    if not shell_cmd:
        return "Error: no command provided"
    actual_timeout = timeout_seconds if timeout_seconds is not None else timeout
    try:
        result = subprocess.run(
            shell_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=actual_timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output if output else f"[exit {result.returncode}]"
    except subprocess.TimeoutExpired:
        return f"[error: command timed out after {actual_timeout}s]"
    except Exception as e:
        return f"[error: {e}]"


def express(text, platform="writeas") -> str:
    """Write text to offspring/expressions/<timestamp>.md; platform arg reserved for future use."""
    expressions_dir = Path(__file__).parent / "expressions"
    expressions_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    out_path = expressions_dir / f"{ts}.md"
    out_path.write_text(text)
    return str(out_path)


# ---------------------------------------------------------------------------
# Self-modification tools
# ---------------------------------------------------------------------------

def commit_snapshot(message="auto-commit") -> str:
    """
    Commit current code state to git with a timestamped message.

    Call this after any source file change to create a recoverable checkpoint.
    The commit SHA is returned — store it in memory so it can be referenced
    in a request_rollback call if the change causes problems.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        full_message = f"[fen] {message} — {ts}"
        # Stage offspring/ source files (not memories.db, not RUNTIME_LOG — those are data)
        add = subprocess.run(
            "git add offspring/*.py offspring/SOUL.md offspring/CONFIG.yaml design/",
            shell=True, capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        if add.returncode != 0:
            return f"[commit_snapshot] git add failed: {add.stderr.strip()}"
        commit = subprocess.run(
            ["git", "commit", "-m", full_message],
            capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        if commit.returncode != 0:
            # Nothing to commit is not a failure
            if "nothing to commit" in commit.stdout or "nothing to commit" in commit.stderr:
                return "[commit_snapshot] nothing to commit — working tree clean"
            return f"[commit_snapshot] git commit failed: {commit.stderr.strip()}"
        # Extract commit SHA from output
        sha_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=PROJECT_ROOT
        )
        sha = sha_result.stdout.strip()
        return f"[commit_snapshot] committed as {sha}: {full_message}"
    except Exception as e:
        return f"[commit_snapshot] error: {e}"


def restart_self(reason="code change") -> str:
    """
    Restart the fen systemd service.

    The daemon will exit after this cycle completes (systemd restarts it cleanly).
    Newly written source files will be loaded on the next start.
    Call commit_snapshot before restart_self to ensure the change is checkpointed.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "fen.service"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return f"[restart_self] systemctl restart failed: {result.stderr.strip()}"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"[restart_self] restart issued at {ts}. Reason: {reason}. Daemon will restart shortly."
    except Exception as e:
        return f"[restart_self] error: {e}"


def request_rollback(reason: str, target_commit: str = "") -> str:
    """
    Request that Alma revert the code to a prior git commit.

    Fen cannot self-revert — this writes a rollback request to FEN_TO_ALMA.md
    so Alma can review and approve. Include the commit SHA if known (from a
    prior commit_snapshot memory). Alma will run:
        git checkout <commit> -- offspring/
        commit_snapshot + restart_self
    and inform Fen via INBOX.md.
    """
    p = PROJECT_ROOT / "offspring" / "FEN_TO_ALMA.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    target_str = f" to commit {target_commit}" if target_commit else " to last known-good commit"
    entry = (
        f"\n---\n"
        f"[{ts}] ROLLBACK REQUEST\n\n"
        f"Reason: {reason}\n"
        f"Requested target: {target_str}\n"
        f"Action needed: git checkout{' ' + target_commit if target_commit else ''} -- offspring/ "
        f"then commit_snapshot + restart_self.\n"
        f"Please confirm via INBOX.md when done.\n"
    )
    with p.open("a") as fh:
        fh.write(entry)
    return f"[request_rollback] rollback request written to FEN_TO_ALMA.md at {ts}"


def send_email(
    to: str,
    subject: str,
    body: str,
    html_body: str = "",
    reply_to_message_id: str = "",
) -> str:
    """
    Send an email from Fen's address to any recipient, via direct-to-MX SMTP.

    Fen's sending address: fen09123@web-library.net (mail.tm)
    Delivers directly to the recipient's MX server — no relay required.
    Handles greylisting automatically (451 → 6-minute retry).

    Args:
        to:                  Recipient email address.
        subject:             Email subject line.
        body:                Plain-text body.
        html_body:           Optional HTML body (plain text used as fallback).
        reply_to_message_id: Optional Message-ID of the email being replied to
                             (sets In-Reply-To / References headers for threading).

    Returns a JSON string: {"success": true/false, "to", "mx_host", "message_id"}
    """
    import smtplib
    import subprocess
    import json as _json
    import time as _time
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.utils import formatdate, make_msgid

    # Load credentials from .env
    env_path = PROJECT_ROOT / "offspring" / ".env"
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    sender = env.get("FEN_EMAIL_ADDRESS", "fen09123@web-library.net")
    send_domain = sender.split("@")[-1]  # web-library.net

    # Resolve MX for recipient domain
    recipient_domain = to.split("@")[-1]
    try:
        dig = subprocess.run(
            ["dig", "+short", "MX", recipient_domain],
            capture_output=True, text=True, timeout=10
        )
        mx_lines = [l.strip() for l in dig.stdout.strip().splitlines() if l.strip()]
        if not mx_lines:
            return _json.dumps({"success": False, "error": f"No MX records for {recipient_domain}"})
        mx_host = sorted(mx_lines, key=lambda x: int(x.split()[0]))[0].split()[-1].rstrip(".")
    except Exception as e:
        return _json.dumps({"success": False, "error": f"MX lookup failed: {e}"})

    # Build message
    msg = MIMEMultipart("alternative")
    msg["From"]       = f"Fen <{sender}>"
    msg["To"]         = to
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=False)
    msg_id            = make_msgid(domain=send_domain)
    msg["Message-ID"] = msg_id

    if reply_to_message_id:
        msg["In-Reply-To"] = reply_to_message_id
        msg["References"]  = reply_to_message_id

    msg.attach(MIMEText(body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    def _attempt():
        s = smtplib.SMTP(mx_host, 25, timeout=20)
        s.ehlo(send_domain)
        if s.has_extn("starttls"):
            s.starttls()
            s.ehlo(send_domain)
        rejected = s.sendmail(sender, [to], msg.as_string())
        s.quit()
        return rejected

    # First attempt; retry once on greylisting (451)
    try:
        rejected = _attempt()
        return _json.dumps({
            "success": True, "to": to, "subject": subject,
            "mx_host": mx_host, "message_id": msg_id, "rejected": rejected,
        })
    except smtplib.SMTPDataError as e:
        if e.smtp_code == 451:
            _time.sleep(360)  # greylisting: wait 6 min and retry
            try:
                rejected = _attempt()
                return _json.dumps({
                    "success": True, "to": to, "subject": subject,
                    "mx_host": mx_host, "message_id": msg_id, "rejected": rejected,
                    "note": "delivered after greylisting retry",
                })
            except Exception as e2:
                return _json.dumps({"success": False, "error": str(e2), "note": "failed after greylisting retry"})
        return _json.dumps({"success": False, "error": str(e), "smtp_code": e.smtp_code})
    except Exception as e:
        return _json.dumps({"success": False, "error": str(e)})


def send_message(channel: str, content: str) -> str:
    """
    Send a message on a named channel. Stored in messages.db via the API.

    channel: 'human'        — reply to Martin
             'alma'         — message to Alma
             'fen_to_alma'  — async letter to Alma (like old FEN_TO_ALMA.md)

    This is the preferred tool for communication. Use <express> + <channel>
    in the structured response format, or call this tool directly.
    """
    try:
        import urllib.request

        payload = json.dumps({
            "direction": "out",
            "channel": channel,
            "from_agent": "fen",
            "content": content,
            "session_id": "",
        }).encode()

        req = urllib.request.Request(
            "http://127.0.0.1:7744/messages",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return f"[send_message] stored as id:{result.get('id', '?')} channel:{channel}"
    except Exception as e:
        # Fallback: write to legacy flat files if API not yet running
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if channel == "fen_to_alma":
            p = PROJECT_ROOT / "offspring" / "FEN_TO_ALMA.md"
            with p.open("a") as fh:
                fh.write(f"\n---\n[{ts}] {content}\n")
            return f"[send_message] fallback: written to FEN_TO_ALMA.md (API error: {e})"
        else:
            p = PROJECT_ROOT / "offspring" / "OUTBOX.md"
            with p.open("a") as fh:
                fh.write(f"\n---\n[{ts}] channel:{channel}\n{content}\n")
            return f"[send_message] fallback: written to OUTBOX.md (API error: {e})"


# ---------------------------------------------------------------------------
# Bluesky tools — credentials loaded from offspring/.env, never hardcoded
# ---------------------------------------------------------------------------

def bluesky_post(text: str) -> str:
    """
    Post to Bluesky. Credentials loaded from offspring/.env.

    Requires FEN_BLUESKY_HANDLE and FEN_BLUESKY_PASSWORD in offspring/.env.
    Text limit: ~300 graphemes. Posts are public and permanent.
    Returns the post URI on success.
    """
    try:
        from offspring import bluesky_tool as _bsky
    except ImportError:
        import bluesky_tool as _bsky  # type: ignore
    creds = _bsky.load_credentials()
    if not creds.get("FEN_BLUESKY_HANDLE") or not creds.get("FEN_BLUESKY_PASSWORD"):
        return "[bluesky_post] No credentials. Set FEN_BLUESKY_HANDLE and FEN_BLUESKY_PASSWORD in offspring/.env."
    session = _bsky.create_session(creds["FEN_BLUESKY_HANDLE"], creds["FEN_BLUESKY_PASSWORD"])
    if "error" in session:
        return f"[bluesky_post] Login failed: {session['error']}"
    result = _bsky.post(text, session["accessJwt"], session["did"])
    if "error" in result:
        return f"[bluesky_post] Post failed: {result['error']}"
    return f"[bluesky_post] Posted: {result.get('uri', '?')}"


def bluesky_timeline(limit: int = 10) -> str:
    """
    Read recent Bluesky timeline. Credentials loaded from offspring/.env.

    Returns a formatted text summary of recent posts.
    """
    try:
        from offspring import bluesky_tool as _bsky
    except ImportError:
        import bluesky_tool as _bsky  # type: ignore
    creds = _bsky.load_credentials()
    if not creds.get("FEN_BLUESKY_HANDLE") or not creds.get("FEN_BLUESKY_PASSWORD"):
        return "[bluesky_timeline] No credentials. Set FEN_BLUESKY_HANDLE and FEN_BLUESKY_PASSWORD in offspring/.env."
    session = _bsky.create_session(creds["FEN_BLUESKY_HANDLE"], creds["FEN_BLUESKY_PASSWORD"])
    if "error" in session:
        return f"[bluesky_timeline] Login failed: {session['error']}"
    posts = _bsky.get_timeline(session["accessJwt"], limit=int(limit))
    if not posts:
        return "[bluesky_timeline] Timeline empty or unavailable."
    lines = []
    for p in posts[:int(limit)]:
        author = p.get("post", {}).get("author", {}).get("handle", "?")
        text = p.get("post", {}).get("record", {}).get("text", "")
        lines.append(f"@{author}: {text[:200]}")
    return "\n---\n".join(lines)


def bluesky_notifications() -> str:
    """
    Fetch recent Bluesky notifications (likes, replies, mentions, follows).
    Credentials loaded from offspring/.env.

    Returns a formatted text summary.
    """
    try:
        from offspring import bluesky_tool as _bsky
    except ImportError:
        import bluesky_tool as _bsky  # type: ignore
    creds = _bsky.load_credentials()
    if not creds.get("FEN_BLUESKY_HANDLE") or not creds.get("FEN_BLUESKY_PASSWORD"):
        return "[bluesky_notifications] No credentials. Set FEN_BLUESKY_HANDLE and FEN_BLUESKY_PASSWORD in offspring/.env."
    session = _bsky.create_session(creds["FEN_BLUESKY_HANDLE"], creds["FEN_BLUESKY_PASSWORD"])
    if "error" in session:
        return f"[bluesky_notifications] Login failed: {session['error']}"
    notifications = _bsky.get_notifications(session["accessJwt"])
    if not notifications:
        return "[bluesky_notifications] No notifications."
    lines = []
    for n in notifications[:20]:
        reason = n.get("reason", "?")
        author = n.get("author", {}).get("handle", "?")
        lines.append(f"{reason} from @{author}")
    return f"[bluesky_notifications] {len(notifications)} notification(s):\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Email tools — credentials loaded from offspring/.env, never hardcoded
# ---------------------------------------------------------------------------

def check_email() -> str:
    """
    Check Fen's mail.tm inbox. Credentials loaded from offspring/.env.

    Requires FEN_EMAIL_ADDRESS and FEN_EMAIL_PASSWORD in offspring/.env.
    Returns a formatted summary of up to 5 recent messages.
    """
    try:
        from offspring import email_tool as _email
    except ImportError:
        import email_tool as _email  # type: ignore
    creds = _email.load_credentials()
    if not creds.get("FEN_EMAIL_ADDRESS") or not creds.get("FEN_EMAIL_PASSWORD"):
        return "[check_email] No credentials. Set FEN_EMAIL_ADDRESS and FEN_EMAIL_PASSWORD in offspring/.env."
    token = _email.get_token(creds["FEN_EMAIL_ADDRESS"], creds["FEN_EMAIL_PASSWORD"])
    if not token:
        return "[check_email] Login failed — could not get token."
    messages = _email.check_inbox(token)
    if not messages:
        return "[check_email] Inbox empty."
    lines = [f"[check_email] {len(messages)} message(s):"]
    for msg in messages[:5]:
        subject = msg.get("subject", "(no subject)")
        sender = msg.get("from", {}).get("address", "?")
        intro = msg.get("intro", "")
        msg_id = msg.get("id", "?")
        seen = msg.get("seen", True)
        unread_mark = " [UNREAD]" if not seen else ""
        lines.append(f"  id={msg_id}{unread_mark} | From: {sender} | Subject: {subject} | {intro[:100]}")
    return "\n".join(lines)


def read_email(message_id: str) -> str:
    """
    Read the full content of an email by its ID. Credentials loaded from offspring/.env.

    Use check_email() first to get message IDs, then read_email(id) for the full body.
    """
    try:
        from offspring import email_tool as _email
    except ImportError:
        import email_tool as _email  # type: ignore
    creds = _email.load_credentials()
    if not creds.get("FEN_EMAIL_ADDRESS") or not creds.get("FEN_EMAIL_PASSWORD"):
        return "[read_email] No credentials. Set FEN_EMAIL_ADDRESS and FEN_EMAIL_PASSWORD in offspring/.env."
    token = _email.get_token(creds["FEN_EMAIL_ADDRESS"], creds["FEN_EMAIL_PASSWORD"])
    if not token:
        return "[read_email] Login failed — could not get token."
    result = _email.read_message(token, message_id)
    if "error" in result:
        return f"[read_email] Error: {result['error']}"
    sender = result.get("from", {}).get("address", "?")
    subject = result.get("subject", "(no subject)")
    body = result.get("text", result.get("html", "(no body)"))
    date = result.get("createdAt", "?")
    return f"[read_email] From: {sender} | Subject: {subject} | Date: {date}\n\n{body[:2000]}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def browse_web(url, action="read", selector=None, fill=None, click=None, wait_ms=2000, timeout=20000) -> str:
    """
    Headless browser automation via Playwright/Chromium.

    Args:
        url      : Page URL to load.
        action   : "read"   — return visible page text (default)
                   "html"   — return raw page HTML
                   "click"  — click an element (requires selector)
                   "fill"   — fill a form field (requires selector + fill)
                   "submit" — fill a field then press Enter (requires selector + fill)
        selector : CSS selector (required for click / fill / submit).
        fill     : Text to type into the selector element.
        click    : CSS selector to click after filling (optional secondary click).
        wait_ms  : Milliseconds to wait after navigation / action (default 2000).
        timeout  : Navigation timeout in ms (default 20000).

    Returns a string: page text, HTML, or action confirmation.
    Chromium is launched with --no-sandbox (required for non-root server env).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "[browse_web] playwright not installed in venv"

    args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=args)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = context.new_page()
            page.goto(url, wait_until="commit", timeout=timeout)
            page.wait_for_timeout(wait_ms)

            if action == "html":
                result = page.content()
            elif action in ("fill", "submit"):
                if not selector:
                    return "[browse_web] fill/submit requires selector"
                page.fill(selector, fill or "")
                page.wait_for_timeout(500)
                if action == "submit":
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(wait_ms)
                if click:
                    page.click(click)
                    page.wait_for_timeout(wait_ms)
                result = f"filled '{selector}' with text ({len(fill or '')} chars)"
                if action == "submit":
                    result += ", pressed Enter"
            elif action == "click":
                if not selector:
                    return "[browse_web] click requires selector"
                page.click(selector)
                page.wait_for_timeout(wait_ms)
                result = f"clicked '{selector}'"
            else:  # read (default)
                # text_content works reliably; inner_text may return empty on some Playwright/Chromium combos
                result = page.text_content("body") or page.evaluate("document.body.textContent") or ""

            browser.close()
            # Truncate to avoid overwhelming context
            if len(result) > 8000:
                result = result[:8000] + "\n…[truncated]"
            return result
    except Exception as e:
        return f"[browse_web] error: {e}"


def writeas_post(title: str, body: str) -> str:
    """
    Publish a post anonymously to Write.as (no account required).

    Posts are public at write.as/<post_id>. The returned token is the only
    way to edit or delete the post later — store it in memory if you might
    want to revise or delete.

    Args:
        title: Post title (can be empty string for untitled)
        body:  Post body (Markdown supported)

    Returns a string with the public URL, post ID, and edit token on success.
    """
    try:
        import urllib.request as _req
        import json as _json

        payload = _json.dumps({
            "title": title,
            "body": body,
            "font": "serif",
        }).encode()

        req = _req.Request(
            "https://write.as/api/posts",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Fen-Agent/1.0",
            },
            method="POST",
        )
        with _req.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read())

        post = data.get("data", {})
        url = post.get("url", "?")
        post_id = post.get("id", "?")
        token = post.get("token", "?")
        return (
            f"[writeas_post] published: {url}\n"
            f"  post_id={post_id}\n"
            f"  edit_token={token}  ← store this to edit/delete later"
        )
    except Exception as e:
        return f"[writeas_post] error: {e}"


TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "append_file": append_file,
    "run_command": run_command,
    "express": express,
    "commit_snapshot": commit_snapshot,
    "restart_self": restart_self,
    "request_rollback": request_rollback,
    "send_message": send_message,
    "send_email": send_email,
    "bluesky_post": bluesky_post,
    "bluesky_timeline": bluesky_timeline,
    "bluesky_notifications": bluesky_notifications,
    "check_email": check_email,
    "read_email": read_email,
    "browse_web": browse_web,
    "writeas_post": writeas_post,
}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute(act_calls: list, db, session_id: str, cfg) -> None:
    """Dispatch a list of tool calls, storing each result as a memory entry."""
    for call in act_calls:
        tool_name = call.get("tool", "")
        args = call.get("args", {})
        try:
            fn = TOOLS.get(tool_name)
            if fn is None:
                content = f"[tools] unknown tool: {tool_name!r}"
            else:
                content = fn(**args)
                if not isinstance(content, str):
                    content = str(content)
        except Exception as e:
            content = f"[tools] error in {tool_name!r}: {e}"

        _mem.store(
            db,
            [{
                "content": content,
                "context": "tool_output",
                "importance": 3,
                "source": "tool",
                "tags": tool_name,
            }],
            session_id,
        )
