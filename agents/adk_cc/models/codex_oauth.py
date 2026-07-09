"""Phase-2 "Sign in with ChatGPT" — our own PKCE browser login.

Reuses Codex CLI's public OAuth client (``codex_auth.CLIENT_ID``): open the
authorize URL in a browser, catch the redirect on ``localhost:1455`` (the
port the client's redirect-URI allow-list is fixed to; ``1457`` fallback),
exchange the code, and persist to adk-cc's OWN store (``codex_auth.save_new_login``)
— no dependency on the Codex CLI being installed.

The callback runs on a short-lived background HTTP server; the caller polls
``status()`` (waiting → done/error). No token is ever logged.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
from typing import Any, Optional

from . import codex_auth

AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
# The client's redirect-URI allow-list is fixed to these (openai/codex login/server.rs).
_PORTS = (1455, 1457)
_SCOPES = "openid profile email offline_access"
_LOGIN_TIMEOUT_SECONDS = 300

_lock = threading.Lock()
_pending: Optional[dict[str, Any]] = None


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def _authorize_url(state: str, challenge: str, redirect_uri: str) -> str:
    q = {
        "response_type": "code",
        "client_id": codex_auth.CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": _SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)


def _exchange_code(code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
    import httpx

    r = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": codex_auth.CLIENT_ID,
        "code_verifier": verifier,
    }, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed (HTTP {r.status_code})")
    data = r.json()
    if not data.get("access_token") or not data.get("refresh_token"):
        raise RuntimeError("token exchange returned no tokens")
    return data


_RESULT_HTML = (
    "<!doctype html><meta charset=utf-8><title>adk-cc</title>"
    "<body style='font-family:system-ui;background:#f5f4ed;color:{color};"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
    "<div style='text-align:center'><h2>{msg}</h2>"
    "<p style='color:#8a8776'>You can close this tab and return to adk-cc.</p></div>"
)


def _make_handler(pending: dict[str, Any]):  # noqa: ANN202
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):  # noqa: ANN002 — silence access logs
            pass

        def do_GET(self):  # noqa: ANN201, N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            err = (qs.get("error") or [None])[0]
            ok = False
            if err:
                pending.update(status="error", error=err)
                msg = f"Sign-in failed: {err}"
            elif state != pending["state"]:
                pending.update(status="error", error="state_mismatch")
                msg = "Sign-in failed (state mismatch)."
            elif not code:
                pending.update(status="error", error="no_code")
                msg = "Sign-in failed (no code)."
            else:
                try:
                    data = _exchange_code(code, pending["verifier"], pending["redirect_uri"])
                    codex_auth.save_new_login(
                        access_token=data["access_token"],
                        refresh_token=data["refresh_token"],
                        id_token=data.get("id_token", ""),
                    )
                    pending.update(status="done")
                    ok = True
                    msg = "Signed in to ChatGPT."
                except Exception as e:  # noqa: BLE001 — surface via status
                    pending.update(status="error", error=str(e))
                    msg = "Sign-in failed."
            html = _RESULT_HTML.format(msg=msg, color="#1B365D" if ok else "#b34e3d")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            # Shut the one-shot server down after answering (from another thread).
            threading.Thread(target=pending["server"].shutdown, daemon=True).start()

    return Handler


def start() -> str:
    """Begin a login: bind the callback server and return the authorize URL to
    open in a browser. Raises RuntimeError if no callback port is available."""
    cancel()
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce()
    pending: dict[str, Any] = {"state": state, "verifier": verifier,
                               "status": "waiting", "error": None}
    server = None
    for port in _PORTS:
        try:
            server = http.server.HTTPServer(("127.0.0.1", port), _make_handler(pending))
            pending["redirect_uri"] = f"http://localhost:{port}/auth/callback"
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(
            f"cannot bind the login callback on port {_PORTS[0]}/{_PORTS[1]}. "
            "Close any in-progress `codex login` and try again."
        )
    pending["server"] = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    timer = threading.Timer(_LOGIN_TIMEOUT_SECONDS, lambda: _timeout(state))
    timer.daemon = True  # must not keep the process alive
    timer.start()
    pending["timer"] = timer
    with _lock:
        global _pending
        _pending = pending
    return _authorize_url(state, challenge, pending["redirect_uri"])


def status() -> dict[str, Any]:
    with _lock:
        p = _pending
    if p is None:
        return {"state": "idle"}
    return {"state": p["status"], "error": p.get("error")}


def cancel() -> None:
    with _lock:
        p = _pending
    if p:
        if p.get("timer"):
            p["timer"].cancel()
        if p.get("server"):
            threading.Thread(target=p["server"].shutdown, daemon=True).start()


def _timeout(state: str) -> None:
    with _lock:
        p = _pending
    if p and p["state"] == state and p["status"] == "waiting":
        p.update(status="error", error="timeout")
        if p.get("server"):
            threading.Thread(target=p["server"].shutdown, daemon=True).start()
