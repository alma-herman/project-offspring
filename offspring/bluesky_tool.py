"""
bluesky_tool.py — Bluesky / AT Protocol integration for Fen

Provides account creation, session management, posting, and reading
via the AT Protocol (https://atproto.com). Uses only the `requests`
library — no external Bluesky SDK required.

ALL FUNCTIONS return {"error": "..."} on failure, never raise exceptions.

CREDENTIALS:
    Use load_credentials() to read FEN_BLUESKY_HANDLE and FEN_BLUESKY_PASSWORD
    from offspring/.env, then pass them to create_session() to get an
    accessJwt for API calls.

TYPICAL USAGE FLOW:
    creds = load_credentials()
    session = create_session(creds["FEN_BLUESKY_HANDLE"], creds["FEN_BLUESKY_PASSWORD"])
    if "error" in session:
        print("Login failed:", session["error"])
    else:
        access_jwt = session["accessJwt"]
        did = session["did"]
        # post something
        result = post("Hello from Fen!", access_jwt, did)
        # read the timeline
        timeline = get_timeline(access_jwt, limit=10)

SESSION TOKENS:
    accessJwt expires (typically after ~2 hours). Call create_session() at
    the start of each cycle rather than caching the token across cycles.
    A refreshJwt is also returned by create_session() — it can be used to
    get a new accessJwt without re-entering the password, but for simplicity
    Fen can just re-authenticate with stored credentials each cycle.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

try:
    import requests
except ImportError:
    raise ImportError("requests is required: pip install requests")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BSKY_HOST = "https://bsky.social/xrpc"

_THIS_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def load_credentials() -> dict:
    """
    Load Fen's Bluesky credentials from offspring/.env.

    Reads FEN_BLUESKY_HANDLE and FEN_BLUESKY_PASSWORD from the .env file
    located at the same directory as this module (offspring/.env).

    Returns:
        dict with keys "FEN_BLUESKY_HANDLE" and "FEN_BLUESKY_PASSWORD".
        Values will be empty strings if the keys are not found.

    Example:
        creds = load_credentials()
        session = create_session(creds["FEN_BLUESKY_HANDLE"], creds["FEN_BLUESKY_PASSWORD"])
    """
    env_path = _THIS_DIR / ".env"
    result = {"FEN_BLUESKY_HANDLE": "", "FEN_BLUESKY_PASSWORD": ""}

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
# Account management
# ---------------------------------------------------------------------------

def create_account(
    handle: str,
    email: str,
    password: str,
    invite_code: str = "",
) -> dict:
    """
    Create a new Bluesky account.

    This is a one-time setup call. After the account is created, use
    create_session() for all subsequent authentication.

    Args:
        handle:      Desired handle, e.g. "fen.bsky.social"
        email:       Email address for the account (use Fen's mail.tm address)
        password:    Chosen password — store in offspring/.env immediately
        invite_code: Optional invite code (leave empty; Bluesky may not require it)

    Returns:
        dict with "did", "handle", "accessJwt", "refreshJwt" on success.
        Returns {"error": "..."} on failure.

    Example:
        result = create_account(
            handle="fen.bsky.social",
            email="fen@example.tm",
            password="MyStr0ngP@ss!",
        )
        if "did" in result:
            print("Created DID:", result["did"])
    """
    payload = {
        "handle": handle,
        "email": email,
        "password": password,
    }
    if invite_code:
        payload["inviteCode"] = invite_code

    try:
        resp = requests.post(
            f"{BSKY_HOST}/com.atproto.server.createAccount",
            json=payload,
            timeout=20,
        )
        if resp.ok:
            return resp.json()
        else:
            return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.RequestException as e:
        return {"error": str(e)}


def create_session(identifier: str, password: str) -> dict:
    """
    Authenticate and create a session on Bluesky.

    Call this at the start of each cycle to get a fresh accessJwt.

    Args:
        identifier: Handle (e.g. "fen.bsky.social") or DID
        password:   Account password

    Returns:
        dict with at minimum:
            "accessJwt"  — use this for all authenticated API calls
            "refreshJwt" — can be used to refresh without re-entering password
            "handle"     — confirmed handle
            "did"        — decentralized identifier (stable, use for repo writes)
        Returns {"error": "..."} on failure.

    Example:
        creds = load_credentials()
        session = create_session(creds["FEN_BLUESKY_HANDLE"], creds["FEN_BLUESKY_PASSWORD"])
        access_jwt = session["accessJwt"]
        did = session["did"]
    """
    try:
        resp = requests.post(
            f"{BSKY_HOST}/com.atproto.server.createSession",
            json={"identifier": identifier, "password": password},
            timeout=20,
        )
        if resp.ok:
            data = resp.json()
            # Return a clean subset for clarity, but include all fields
            return data
        else:
            return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.RequestException as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def post(text: str, access_jwt: str, did: str) -> dict:
    """
    Publish a post to Bluesky.

    Posts are public and cannot be easily deleted after publishing.
    Maximum length is ~300 graphemes (not bytes — emoji count as 1).

    Args:
        text:       Post body (max ~300 graphemes)
        access_jwt: JWT from create_session()["accessJwt"]
        did:        Your DID from create_session()["did"]

    Returns:
        dict with "uri" and "cid" of the created post on success.
        Returns {"error": "..."} on failure.

    Example:
        session = create_session(handle, password)
        result = post("Hello, world.", session["accessJwt"], session["did"])
        print(result["uri"])  # at://did:plc:.../app.bsky.feed.post/...
    """
    # Bluesky requires an ISO 8601 timestamp with timezone
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": created_at,
    }

    payload = {
        "repo": did,
        "collection": "app.bsky.feed.post",
        "record": record,
    }

    try:
        resp = requests.post(
            f"{BSKY_HOST}/com.atproto.repo.createRecord",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_jwt}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        if resp.ok:
            return resp.json()
        else:
            return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.RequestException as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get_timeline(access_jwt: str, limit: int = 20) -> list:
    """
    Fetch the home timeline (posts from accounts Fen follows).

    Args:
        access_jwt: JWT from create_session()["accessJwt"]
        limit:      Max number of posts to return (1–100, default 20)

    Returns:
        list of post feed-view dicts. Each entry typically contains:
            "post": {
                "uri", "cid", "author": {"handle", "displayName"},
                "record": {"text", "createdAt"},
                "replyCount", "repostCount", "likeCount"
            }
        Returns [] on error.

    Example:
        timeline = get_timeline(access_jwt, limit=10)
        for item in timeline:
            print(item["post"]["author"]["handle"], item["post"]["record"]["text"])
    """
    try:
        resp = requests.get(
            f"{BSKY_HOST}/app.bsky.feed.getTimeline",
            headers={"Authorization": f"Bearer {access_jwt}"},
            params={"limit": limit},
            timeout=20,
        )
        if resp.ok:
            data = resp.json()
            return data.get("feed", [])
        else:
            print(f"[bluesky_tool] get_timeline failed: HTTP {resp.status_code}")
            return []
    except requests.RequestException as e:
        print(f"[bluesky_tool] get_timeline error: {e}")
        return []


def get_notifications(access_jwt: str) -> list:
    """
    Fetch recent notifications (likes, replies, follows, mentions, quotes).

    Args:
        access_jwt: JWT from create_session()["accessJwt"]

    Returns:
        list of notification dicts. Each typically contains:
            "reason"    — "like", "repost", "follow", "mention", "reply", "quote"
            "author"    — {"handle", "displayName", "did"}
            "isRead"    — bool
            "indexedAt" — ISO timestamp
            "record"    — the related record (e.g. the reply text)
        Returns [] on error.

    Example:
        notifications = get_notifications(access_jwt)
        unread = [n for n in notifications if not n["isRead"]]
        for n in unread:
            print(n["reason"], "from", n["author"]["handle"])
    """
    try:
        resp = requests.get(
            f"{BSKY_HOST}/app.bsky.notification.listNotifications",
            headers={"Authorization": f"Bearer {access_jwt}"},
            timeout=20,
        )
        if resp.ok:
            data = resp.json()
            return data.get("notifications", [])
        else:
            print(f"[bluesky_tool] get_notifications failed: HTTP {resp.status_code}")
            return []
    except requests.RequestException as e:
        print(f"[bluesky_tool] get_notifications error: {e}")
        return []


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------

def get_profile(handle_or_did: str, access_jwt: str = "") -> dict:
    """
    Fetch a profile by handle or DID.

    Args:
        handle_or_did: Handle (e.g. "fen.bsky.social") or DID
        access_jwt:    Optional JWT for authenticated requests

    Returns:
        dict with "did", "handle", "displayName", "description",
        "followersCount", "followsCount", "postsCount".
        Returns {"error": "..."} on failure.
    """
    headers = {}
    if access_jwt:
        headers["Authorization"] = f"Bearer {access_jwt}"

    try:
        resp = requests.get(
            f"{BSKY_HOST}/app.bsky.actor.getProfile",
            params={"actor": handle_or_did},
            headers=headers,
            timeout=20,
        )
        if resp.ok:
            return resp.json()
        else:
            return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.RequestException as e:
        return {"error": str(e)}
