"""ChatGPT-subscription (Codex OAuth) token storage + refresh.

Two ways in (both land here):
  - Phase 1: the official Codex CLI login (``~/.codex/auth.json``).
  - Phase 2: our own "Sign in with ChatGPT" (see ``codex_oauth``), written to
    adk-cc's OWN store (``ADK_CC_CODEX_STORE_DIR`` / desktop data / ``~/.adk-cc``).

Read priority: explicit ``ADK_CC_CODEX_AUTH_FILE`` override → our own store →
the Codex CLI file. **Refresh writes back to the SAME file the tokens were read
from**, so refreshing the CLI login stays in sync with the CLI (single source of
truth) and our own login rotates independently — we never clobber ``~/.codex``.

SUBSCRIPTION ONLY: only ever the OAuth Bearer used against
``chatgpt.com/backend-api/codex``. Never ``api.openai.com``, never
``OPENAI_API_KEY``, never the id_token->API-key exchange. Tokens are never logged.
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
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_REFRESH_WINDOW_SECONDS = 5 * 60
_PERMANENT_REFRESH_ERRORS = {
    "refresh_token_expired", "refresh_token_reused",
    "refresh_token_invalidated", "invalid_grant",
}


class CodexAuthError(RuntimeError):
    def __init__(self, message: str, *, needs_login: bool = False) -> None:
        super().__init__(message)
        self.needs_login = needs_login


# -- source resolution -------------------------------------------------

def _override_path() -> Optional[Path]:
    o = os.environ.get("ADK_CC_CODEX_AUTH_FILE")
    return Path(o).expanduser() if o else None


def own_store_path() -> Path:
    """adk-cc's own token store (Phase-2 login target)."""
    d = os.environ.get("ADK_CC_CODEX_STORE_DIR")
    if d:
        base = Path(d).expanduser()
    else:
        from .. import deployment as _dep

        base = _dep.data_dir()
    return base / "codex_auth.json"


def cli_store_path() -> Path:
    """The official Codex CLI login file."""
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "auth.json"


def _read_source() -> Optional[Path]:
    ov = _override_path()
    if ov is not None:
        return ov if ov.exists() else None
    own = own_store_path()
    if own.exists():
        return own
    cli = cli_store_path()
    return cli if cli.exists() else None


def _write_target(source: Optional[Path]) -> Path:
    """Where a NEW login is written: the override, else our own store."""
    return _override_path() or own_store_path()


# -- tokens ------------------------------------------------------------

@dataclass
class CodexTokens:
    access_token: str
    refresh_token: str
    account_id: str
    id_token: str = ""
    source_path: Optional[Path] = None  # where these were read from / write back to
    _raw: dict[str, Any] = None  # type: ignore[assignment]


def _b64url_json(segment: str) -> dict[str, Any]:
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad))


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        return _b64url_json(token.split(".")[1])
    except Exception:
        return {}


def account_id_from(id_token: str, access_token: str) -> str:
    for tok in (id_token, access_token):
        auth = _jwt_payload(tok).get("https://api.openai.com/auth", {})
        acc = auth.get("chatgpt_account_id")
        if acc:
            return acc
    return ""


def _access_exp(access_token: str) -> Optional[int]:
    exp = _jwt_payload(access_token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def load_tokens() -> Optional[CodexTokens]:
    path = _read_source()
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if raw.get("auth_mode") not in (None, "chatgpt"):
        return None
    tok = raw.get("tokens") or {}
    access, refresh = tok.get("access_token"), tok.get("refresh_token")
    if not access or not refresh:
        return None
    account = tok.get("account_id") or account_id_from(raw.get("id_token") or "", access)
    return CodexTokens(
        access_token=access, refresh_token=refresh, account_id=account or "",
        id_token=tok.get("id_token", ""), source_path=path, _raw=raw,
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _save_tokens(tokens: CodexTokens) -> None:
    """Write refreshed tokens back to their SOURCE file, preserving other fields
    (and 0600 perms). Refreshing the CLI login updates ~/.codex in place."""
    raw = dict(tokens._raw or {})
    raw.setdefault("auth_mode", "chatgpt")
    inner = dict(raw.get("tokens") or {})
    inner.update(access_token=tokens.access_token, refresh_token=tokens.refresh_token,
                 account_id=tokens.account_id)
    if tokens.id_token:
        inner["id_token"] = tokens.id_token
    raw["tokens"] = inner
    raw["last_refresh"] = _now_iso()
    _write(tokens.source_path or _write_target(None), raw)


def save_new_login(*, access_token: str, refresh_token: str, id_token: str = "",
                   account_id: str = "") -> Path:
    """Persist a fresh login (Phase-2 OAuth) to adk-cc's OWN store."""
    path = _write_target(None)
    _write(path, {
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": id_token, "access_token": access_token,
                   "refresh_token": refresh_token,
                   "account_id": account_id or account_id_from(id_token, access_token)},
        "last_refresh": _now_iso(),
    })
    return path


def clear_login() -> bool:
    """Remove adk-cc's own login (Phase-2 disconnect). Leaves ~/.codex untouched.
    Returns True if a file was removed."""
    for p in (_override_path(), own_store_path()):
        if p is not None and p.exists():
            try:
                p.unlink()
                return True
            except OSError:
                return False
    return False


# -- refresh + access --------------------------------------------------

async def _refresh(tokens: CodexTokens) -> CodexTokens:
    import httpx

    body = {"client_id": CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token}
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
    updated = CodexTokens(
        access_token=data.get("access_token") or tokens.access_token,
        refresh_token=data.get("refresh_token") or tokens.refresh_token,
        account_id=tokens.account_id,
        id_token=data.get("id_token") or tokens.id_token,
        source_path=tokens.source_path, _raw=tokens._raw,
    )
    _save_tokens(updated)
    return updated


async def get_access(*, force_refresh: bool = False) -> tuple[str, str]:
    tokens = load_tokens()
    if tokens is None:
        raise CodexAuthError(
            "no ChatGPT subscription login found (sign in, or run `codex login`)",
            needs_login=True,
        )
    exp = _access_exp(tokens.access_token)
    stale = force_refresh or (exp is not None and exp - time.time() < _REFRESH_WINDOW_SECONDS)
    if stale:
        tokens = await _refresh(tokens)
    return tokens.access_token, tokens.account_id


def connection_status() -> dict[str, Any]:
    tokens = load_tokens()
    if tokens is None:
        return {"connected": False}
    payload = _jwt_payload(tokens.access_token)
    auth = payload.get("https://api.openai.com/auth", {})
    exp = payload.get("exp")
    src = tokens.source_path
    return {
        "connected": True,
        "plan": auth.get("chatgpt_plan_type"),
        "account_id_tail": tokens.account_id[-6:] if tokens.account_id else None,
        "expires_at": int(exp) if isinstance(exp, (int, float)) else None,
        "expired": bool(exp and exp < time.time()),
        # "own" = our Phase-2 login; "cli" = the Codex CLI login.
        "mode": "own" if src == own_store_path() else ("cli" if src == cli_store_path() else "file"),
    }
