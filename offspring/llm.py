"""
llm.py — LLM API abstraction for Fen.

Wraps the OpenAI-compatible chat completions API.
Provider is fully configurable via Config: local Ollama, vLLM, OpenAI, Groq, etc.
Only CONFIG.yaml needs changing to switch providers — no code changes required.

Single public interface:
    class LLMError(Exception)
    def call(prompt: str, cfg: Config) -> str
"""

from __future__ import annotations

import logging
import time
import urllib.request
import urllib.error
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Config is defined in core.py; imported at call time to avoid circular imports.
    # Type hint only — not evaluated at runtime.
    from offspring.core import Config

logger = logging.getLogger(__name__)

# Cache: fingerprint -> {"token": str, "expires_at": float}
_token_cache: dict = {}


class LLMError(Exception):
    """Raised on any LLM API failure: auth, connection, timeout, or empty response."""
    pass


def _resolve_api_key(api_key: str, api_base_url: str) -> str:
    """
    Exchange a GitHub OAuth/PAT token for a short-lived Copilot API token.

    Only activates when api_base_url contains "githubcopilot.com" and the key
    looks like a GitHub token (gho_, github_pat_, or ghu_ prefix).
    Falls back to the raw key on any exchange error.
    """
    if "githubcopilot.com" not in (api_base_url or ""):
        return api_key

    if not (
        api_key.startswith("gho_")
        or api_key.startswith("github_pat_")
        or api_key.startswith("ghu_")
    ):
        return api_key

    fingerprint = f"{api_key[:8]}:{len(api_key)}"
    cached = _token_cache.get(fingerprint)
    if cached and time.time() < cached["expires_at"] - 300:
        return cached["token"]

    try:
        req = urllib.request.Request(
            "https://api.github.com/copilot_internal/v2/token",
            headers={
                "Authorization": f"token {api_key}",
                "User-Agent": "GithubCopilot/1.0",
                "Accept": "application/json",
                "Editor-Version": "vscode/1.97.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        token = data["token"]
        expires_at = float(data.get("expires_at", time.time() + 1800))
        _token_cache[fingerprint] = {"token": token, "expires_at": expires_at}
        return token
    except Exception as exc:
        logger.warning("GitHub Copilot token exchange failed, using raw key: %s", exc)
        return api_key


def call(prompt: str, cfg: "Config") -> str:
    """
    Call the LLM with a single user message containing the full prompt.

    Args:
        prompt: The complete prompt string (soul + memory + context + task).
        cfg:    A Config object with api_base_url, api_key, and model fields.

    Returns:
        Raw response text from the model.

    Raises:
        LLMError: On authentication failure, connection error, timeout, or empty response.
                  The caller (core.py) catches LLMError, logs it, and continues to next cycle.
    """
    try:
        from openai import OpenAI, AuthenticationError, APIConnectionError, APITimeoutError, APIError
    except ImportError as e:
        raise LLMError(
            f"openai package not installed. Run: pip install openai\nOriginal error: {e}"
        )

    resolved_key = _resolve_api_key(cfg.api_key or "", cfg.api_base_url or "")

    is_copilot = "githubcopilot.com" in (cfg.api_base_url or "")
    client_kwargs: dict = {
        "api_key": resolved_key or "local",
        "base_url": cfg.api_base_url or None,  # None = OpenAI default endpoint
    }
    if is_copilot:
        client_kwargs["default_headers"] = {
            "Editor-Version": "vscode/1.104.1",
            "Copilot-Integration-Id": "vscode-chat",
            "Openai-Intent": "conversation-edits",
            "x-initiator": "agent",
        }

    try:
        client = OpenAI(**client_kwargs)
    except Exception as e:
        raise LLMError(f"Failed to create OpenAI client: {e}") from e

    try:
        response = client.chat.completions.create(
            model=cfg.model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
    except AuthenticationError as e:
        raise LLMError(f"Authentication failed — check api_key in CONFIG.yaml: {e}") from e
    except APIConnectionError as e:
        raise LLMError(f"Connection failed — check api_base_url in CONFIG.yaml: {e}") from e
    except APITimeoutError as e:
        raise LLMError(f"Request timed out: {e}") from e
    except APIError as e:
        raise LLMError(f"API error: {e}") from e
    except Exception as e:
        raise LLMError(f"Unexpected error during LLM call: {e}") from e

    # Extract content — handle Copilot API quirk: empty choices[] when output is cut
    if not response.choices:
        usage = getattr(response, 'usage', None)
        ct = getattr(usage, 'completion_tokens', '?') if usage else '?'
        raise LLMError(
            f"API returned empty choices (response truncated? completion_tokens={ct}). "
            "Try reducing context length or checking Copilot token output limits."
        )
    try:
        content = response.choices[0].message.content
    except (IndexError, AttributeError) as e:
        raise LLMError(f"Unexpected response shape — could not extract content: {e}") from e

    # Return raw string regardless of whether it parses cleanly.
    # core.py's parse_response handles malformed output; we don't second-guess it here.
    return content or ""
