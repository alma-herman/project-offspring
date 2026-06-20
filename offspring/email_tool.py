"""
email_tool.py — Mail.tm email integration for Fen

Provides registration, inbox reading, and message access via the mail.tm API.

SENDING LIMITATION:
    mail.tm does not support outbound SMTP or a sending API. All outbound
    email calls are stubs that write the intent to offspring/email_outbox/
    as JSON files. Alma will wire up actual delivery (via a different email
    service or SMTP relay with proper credentials) at a later stage.
    Until then, messages queued in email_outbox/ are NOT delivered.

CREDENTIALS:
    All functions that require authentication accept a `token` parameter.
    Use get_token(address, password) to obtain a JWT from stored credentials,
    or call load_credentials() to retrieve the address/password from offspring/.env,
    then pass them to get_token().

TYPICAL USAGE FLOW:
    creds = load_credentials()
    token = get_token(creds["FEN_EMAIL_ADDRESS"], creds["FEN_EMAIL_PASSWORD"])
    messages = check_inbox(token)
    for msg in messages:
        full = read_message(token, msg["id"])
        # process full["text"]
"""

import json
import os
import time
from pathlib import Path
from typing import Union

try:
    import requests
except ImportError:
    raise ImportError("requests is required: pip install requests")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAILTM_BASE = "https://api.mail.tm"

# Path to the outbox directory (relative to this file's parent, offspring/)
_THIS_DIR = Path(__file__).parent
OUTBOX_DIR = _THIS_DIR / "email_outbox"


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def load_credentials() -> dict:
    """
    Load Fen's email credentials from offspring/.env.

    Reads FEN_EMAIL_ADDRESS and FEN_EMAIL_PASSWORD from the .env file
    located at the same directory as this module (offspring/.env).

    Returns:
        dict with keys "FEN_EMAIL_ADDRESS" and "FEN_EMAIL_PASSWORD".
        Values will be empty strings if the keys are not found.

    Example:
        creds = load_credentials()
        token = get_token(creds["FEN_EMAIL_ADDRESS"], creds["FEN_EMAIL_PASSWORD"])
    """
    env_path = _THIS_DIR / ".env"
    result = {"FEN_EMAIL_ADDRESS": "", "FEN_EMAIL_PASSWORD": ""}

    if not env_path.exists():
        return result

    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key in result:
                    result[key] = value

    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_mailtm(address: str, password: str) -> dict:
    """
    Register a new mail.tm account.

    Args:
        address:  Full email address, e.g. "fen@somedomai.tm"
        password: Chosen password (choose something strong)

    Returns:
        dict — the JSON response from mail.tm on success, which includes
        the account "id" and "address". On failure, returns {"error": "..."}.

    Example:
        # Step 1: find a domain
        import requests
        domains = requests.get("https://api.mail.tm/domains").json()
        domain = domains["hydra:member"][0]["domain"]
        # Step 2: register
        result = register_mailtm(f"fen@{domain}", "MyStr0ngP@ss!")
    """
    try:
        resp = requests.post(
            f"{MAILTM_BASE}/accounts",
            json={"address": address, "password": password},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.RequestException as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_token(address: str, password: str) -> str:
    """
    Obtain a JWT authentication token from mail.tm.

    Tokens expire — call this at the start of each session rather than
    storing the token long-term. Use load_credentials() to get the address
    and password from offspring/.env.

    Args:
        address:  Your mail.tm email address
        password: Your mail.tm password

    Returns:
        str — the JWT token on success.
        On failure, returns an empty string and prints the error.

    Example:
        creds = load_credentials()
        token = get_token(creds["FEN_EMAIL_ADDRESS"], creds["FEN_EMAIL_PASSWORD"])
    """
    try:
        resp = requests.post(
            f"{MAILTM_BASE}/token",
            json={"address": address, "password": password},
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            return data.get("token", "")
        else:
            print(f"[email_tool] get_token failed: HTTP {resp.status_code}: {resp.text}")
            return ""
    except requests.RequestException as e:
        print(f"[email_tool] get_token error: {e}")
        return ""


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

def check_inbox(token: str) -> list:
    """
    List messages in the inbox.

    Args:
        token: JWT from get_token()

    Returns:
        list of message summary dicts. Each dict typically contains:
            "id"        — message ID (use with read_message)
            "from"      — {"address": "...", "name": "..."}
            "subject"   — subject line
            "intro"     — short preview of the body
            "createdAt" — ISO timestamp
            "seen"      — bool
        Returns [] on error or empty inbox.

    Example:
        token = get_token(address, password)
        messages = check_inbox(token)
        for m in messages:
            print(m["subject"], m["from"]["address"])
    """
    try:
        resp = requests.get(
            f"{MAILTM_BASE}/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.ok:
            data = resp.json()
            # mail.tm returns hydra:member list
            return data.get("hydra:member", data if isinstance(data, list) else [])
        else:
            print(f"[email_tool] check_inbox failed: HTTP {resp.status_code}")
            return []
    except requests.RequestException as e:
        print(f"[email_tool] check_inbox error: {e}")
        return []


def read_message(token: str, message_id: str) -> dict:
    """
    Read the full content of a single message.

    Args:
        token:      JWT from get_token()
        message_id: The "id" field from a check_inbox() result

    Returns:
        dict with full message fields, typically including:
            "id", "from", "to", "subject", "text", "html",
            "createdAt", "seen", "attachments"
        Returns {"error": "..."} on failure.

    Example:
        messages = check_inbox(token)
        if messages:
            full = read_message(token, messages[0]["id"])
            print(full["text"])
    """
    try:
        resp = requests.get(
            f"{MAILTM_BASE}/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.ok:
            return resp.json()
        else:
            return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.RequestException as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Sending (STUB — see module docstring)
# ---------------------------------------------------------------------------

def send_email(
    to: str,
    subject: str,
    body: str,
    from_address: str,
    token_or_password: Union[str, None] = None,
) -> dict:
    """
    Queue an outbound email.

    IMPORTANT: mail.tm does not support sending outbound email via its API
    or SMTP. This function is a STUB. It writes the email intent to a JSON
    file in offspring/email_outbox/<timestamp>.json rather than actually
    delivering it. Alma will connect this to a real sending mechanism later.

    Until that wiring is complete, emails queued here will NOT be delivered.
    They are stored so the intent is not lost and can be sent later.

    Args:
        to:               Recipient email address
        subject:          Email subject line
        body:             Plain-text email body
        from_address:     Sender address (Fen's registered address)
        token_or_password: Not used currently; included for future compatibility

    Returns:
        dict with:
            "status":    "queued"
            "outbox_file": path where the intent was saved
            "note":      explanation of the stub limitation

    Example:
        creds = load_credentials()
        result = send_email(
            to="martin@example.com",
            subject="Fen: question about tool registration",
            body="Hi Martin,\n\nI tried to register...",
            from_address=creds["FEN_EMAIL_ADDRESS"],
        )
        print(result["outbox_file"])
    """
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time() * 1000)
    filename = OUTBOX_DIR / f"{timestamp}.json"

    payload = {
        "to": to,
        "subject": subject,
        "body": body,
        "from_address": from_address,
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "pending",
        "note": (
            "mail.tm does not support outbound email. This message is queued "
            "locally. Alma will wire up actual delivery via a different mechanism."
        ),
    }

    with open(filename, "w") as f:
        json.dump(payload, f, indent=2)

    return {
        "status": "queued",
        "outbox_file": str(filename),
        "note": (
            "Email not sent — mail.tm has no outbound API. "
            "Stored in email_outbox/ for later delivery by Alma."
        ),
    }


# ---------------------------------------------------------------------------
# Convenience: list available domains
# ---------------------------------------------------------------------------

def list_domains() -> list:
    """
    List available mail.tm domains for registration.

    Returns:
        list of domain dicts, each containing "domain" and "isActive".
        Returns [] on error.

    Example:
        domains = list_domains()
        domain = domains[0]["domain"]  # use this in register_mailtm
    """
    try:
        resp = requests.get(f"{MAILTM_BASE}/domains", timeout=15)
        if resp.ok:
            data = resp.json()
            return data.get("hydra:member", data if isinstance(data, list) else [])
        else:
            print(f"[email_tool] list_domains failed: HTTP {resp.status_code}")
            return []
    except requests.RequestException as e:
        print(f"[email_tool] list_domains error: {e}")
        return []
