"""ChatGPT-subscription (Codex OAuth) token access.

Phase 1 interoperates with the official Codex CLI login: it reads the tokens
from ``~/.codex/auth.json`` (override with ``ADK_CC_CODEX_AUTH_FILE``) and
refreshes them IN PLACE when the access token is near expiry — the same
single-source-of-truth approach as simonw/llm-openai-via-codex, so the Codex CLI
and adk-cc never diverge on the (rotating) refresh token.

SUBSCRIPTION ONLY: this module only ever handles the OAuth Bearer token used
against ``chatgpt.com/backend-api/codex``. It never touches ``api.openai.com``,
never reads/writes ``OPENAI_API_KEY``, and never performs the id_token->API-key
token-exchange. Token material is never logged.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Codex CLI's public OAuth client (openai/codex codex-rs/login/src/auth/manager.rs).
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Refresh once the access token is within this many seconds of expiry.
_REFRESH_WINDOW_SECONDS = 5 * 60
# Refresh grant errors that mean the login is dead -> re-login required.
_PERMANENT_REFRESH_ERRORS = {
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
    "invalid_grant",
}


class CodexAuthError(RuntimeError):
    """Auth could not be established (no login, or refresh permanently failed)."""

    def __init__(self, message: str, *, needs_login: bool = False) -> None:
        super().__init__(message)
        self.needs_login = needs_login


def auth_file_path() -> Path:
    override = os.environ.get("ADK_CC_CODEX_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "auth.json"


@dataclass
class CodexTokens:
    access_token: str
    refresh_token: str
    account_id: str
    id_token: str = ""
    # Full original file dict, so a write-back preserves fields we don't manage
    # (auth_mode, OPENAI_API_KEY, etc.) and the Codex CLI keeps working.
    _raw: dict[str, Any] = None  # type: ignore[assignment]


def _b64url_json(segment: str) -> dict[str, Any]:
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad))


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        return _b64url_json(token.split(".")[1])
    except Exception:
        return {}


def _access_exp(access_token: str) -> Optional[int]:
    exp = _jwt_payload(access_token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def load_tokens() -> Optional[CodexTokens]:
    """Read the Codex login file, or None if there's no usable ChatGPT login."""
    path = auth_file_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if raw.get("auth_mode") not in (None, "chatgpt"):
        return None  # e.g. an apikey-mode login — not the subscription path
    tok = raw.get("tokens") or {}
    access, refresh = tok.get("access_token"), tok.get("refresh_token")
    if not access or not refresh:
        return None
    account = tok.get("account_id") or _jwt_payload(
        raw.get("id_token") or access
    ).get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
    return CodexTokens(
        access_token=access, refresh_token=refresh, account_id=account or "",
        id_token=tok.get("id_token", ""), _raw=raw,
    )


def _save_tokens(tokens: CodexTokens) -> None:
    """Write refreshed tokens back to the auth file, preserving other fields and
    0600 perms (matches how the Codex CLI stores it)."""
    raw = dict(tokens._raw or {})
    raw.setdefault("auth_mode", "chatgpt")
    inner = dict(raw.get("tokens") or {})
    inner.update(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        account_id=tokens.account_id,
    )
    if tokens.id_token:
        inner["id_token"] = tokens.id_token
    raw["tokens"] = inner
    raw["last_refresh"] = _now_iso()
    path = auth_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _now_iso() -> str:
    # RFC3339 UTC, matching the Codex CLI's `last_refresh` format.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _refresh(tokens: CodexTokens) -> CodexTokens:
    """Exchange the refresh token for a fresh access token (rotates the refresh
    token). Body/endpoint per openai/codex codex-rs/login/src/auth/manager.rs."""
    import httpx

    body = {
        "client_id": _CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_TOKEN_URL, json=body)
    if r.status_code != 200:
        code = ""
        try:
            code = (r.json() or {}).get("error", "")
        except Exception:
            pass
        raise CodexAuthError(
            f"token refresh failed (HTTP {r.status_code})",
            needs_login=code in _PERMANENT_REFRESH_ERRORS or r.status_code in (400, 401),
        )
    data = r.json()
    # All fields optional; keep prior values when the server omits one.
    updated = CodexTokens(
        access_token=data.get("access_token") or tokens.access_token,
        refresh_token=data.get("refresh_token") or tokens.refresh_token,
        account_id=tokens.account_id,
        id_token=data.get("id_token") or tokens.id_token,
        _raw=tokens._raw,
    )
    _save_tokens(updated)
    return updated


async def get_access(*, force_refresh: bool = False) -> tuple[str, str]:
    """Return a valid ``(access_token, account_id)``, refreshing in place if the
    access token is within the refresh window. Raises CodexAuthError(needs_login)
    when there's no login or the refresh token is dead."""
    tokens = load_tokens()
    if tokens is None:
        raise CodexAuthError(
            "no ChatGPT subscription login found (run `codex login`)",
            needs_login=True,
        )
    exp = _access_exp(tokens.access_token)
    stale = force_refresh or (exp is not None and exp - time.time() < _REFRESH_WINDOW_SECONDS)
    if stale:
        tokens = await _refresh(tokens)
    return tokens.access_token, tokens.account_id


def connection_status() -> dict[str, Any]:
    """UI-safe status: connected?, plan, account-id tail, expiry — never a token."""
    tokens = load_tokens()
    if tokens is None:
        return {"connected": False}
    payload = _jwt_payload(tokens.access_token)
    auth = payload.get("https://api.openai.com/auth", {})
    exp = payload.get("exp")
    return {
        "connected": True,
        "plan": auth.get("chatgpt_plan_type"),
        "account_id_tail": tokens.account_id[-6:] if tokens.account_id else None,
        "expires_at": int(exp) if isinstance(exp, (int, float)) else None,
        "expired": bool(exp and exp < time.time()),
        "source": str(auth_file_path()),
    }
