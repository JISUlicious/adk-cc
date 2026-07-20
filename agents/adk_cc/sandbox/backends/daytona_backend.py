"""Backend that delegates to a self-hosted Daytona compute plane.

Targets Daytona's two-service HTTP surface — the NestJS control plane
(sandbox lifecycle) and the Go toolbox proxy (per-operation exec / file
IO). One adk-cc session maps to one Daytona sandbox.

  agent process                            Daytona deployment
  ─────────────────                        ────────────────────────────
  DaytonaBackend
    │
    ├── control plane ───────────────────► <api_url>:3000
    │   (sandbox lifecycle)                 - POST /api/sandbox
    │                                       - GET  /api/sandbox/{id}
    │                                       - POST /api/sandbox/{id}/stop
    │                                       - DELETE /api/sandbox/{id}
    │
    └── toolbox proxy ───────────────────► <proxy_url>:4000
        (per-op exec / file IO)             - POST /toolbox/{id}/process/execute
                                            - POST /toolbox/{id}/files/upload
                                            - GET  /toolbox/{id}/files/download

Same Bearer token authenticates both services.

Routes we DO NOT take
---------------------

1. **`/api/toolbox/{id}/toolbox/*` on the control plane (port 3000)**.
   These are the `[DEPRECATED]` NestJS wrapper routes — they work but
   relay every byte through the API server, which doesn't scale. The
   doubled `/toolbox/` segment in the path is an artifact of how the
   Nest controller is namespaced (controller at `/api/toolbox`, routes
   start with `:sandboxId/toolbox/...`). Confirmed deprecation status
   with Daytona maintainers (2026-05-18, against self-hosted v0.176.0).
   Use the Go proxy on :4000 instead — canonical path is
   `/toolbox/{id}/<route>` with NO doubled `/toolbox/`.

2. **`GET /api/sandbox/{id}/toolbox-proxy-url`**. On self-hosted
   docker-compose this endpoint returns `http://proxy.localhost:4000/toolbox`
   — `proxy.localhost` is host-insensitive cosmetic (the Go proxy
   dispatches purely by URL path), so we accept the proxy base URL via
   `ADK_CC_DAYTONA_PROXY_URL` directly rather than dereferencing.
   Operators with custom routing can override the literal returned by
   this endpoint via the (undocumented) `PROXY_TOOLBOX_BASE_URL` env on
   the API container — relevant for in-cluster clients only.

Snapshot vs resources
---------------------

`POST /api/sandbox` rejects `cpu`/`memory`/`disk` request fields when a
`snapshot` is set (validated at
`apps/api/src/sandbox/controllers/sandbox.controller.ts:304-306` in
upstream). The snapshot dictates resources; resource overrides only
apply via a custom `buildInfo` build. Our request builder elides
resource fields whenever a snapshot is in play. The
`ADK_CC_DAYTONA_SNAPSHOT` env knob is the recommended path for v1; a
`buildInfo` path is a v2 follow-up.

Exec response shape
-------------------

`POST /toolbox/{id}/process/execute` returns `{exitCode: int, result:
str}` where `result` is stdout + stderr merged. We surface `result` on
`ExecResult.stdout` with `stderr=""`. Callers can't reliably split the
streams. The session-based exec API (`POST /toolbox/{id}/process/session`)
gives per-stream output and is the v2 route to streaming exec.

Idempotency
-----------

Every mutating control-plane request (POST /api/sandbox, POST stop,
DELETE) sends an `Idempotency-Key` header so transient network glitches
retry safely. Daytona's current API may not honor the header; sending
it is harmless and matches `SandboxServiceBackend`'s convention. Toolbox
proxy calls (exec, files) are stateless from Daytona's POV and don't
need keys.

Trade-offs vs SandboxServiceBackend
-----------------------------------

  - +  Daytona is open-source and self-hostable via docker-compose;
       no upstream-service dependency.
  - +  Stronger isolation (per-sandbox kernel/fs/network stack).
  - +  Native multi-tenant via Daytona's organization + API-key model.
  - −  Two services to reach (control plane + toolbox proxy). Operators
       on stock docker-compose just publish both ports.
  - −  No streaming exec in v1 (inherit ABC default — one chunk at end).
  - −  Stdout/stderr are merged in the exec response (see above).

See `docs/04-deployment-sandbox.md` Path D for the operator setup story.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shlex
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..config import (
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxCapacityError,
    SandboxViolation,
)
from .base import SandboxBackend

if TYPE_CHECKING:
    from ...credentials import CredentialProvider
    from ..sandbox_env import SandboxEnvSpec
    from ..workspace import WorkspaceRoot

log = logging.getLogger(__name__)

# Daytona's stock self-hosted layout: control plane on :3000, toolbox
# proxy on :4000, same host. If ADK_CC_DAYTONA_PROXY_URL is unset, we
# derive the proxy URL by swapping :3000 → :4000 on the API URL.
_DEFAULT_PROXY_PORT = 4000

# Terminal sandbox states that abort the start-poll loop. Anything not
# in this set OR "started" is treated as a transient state we keep
# polling on (creating, starting, pulling, building, …).
_TERMINAL_FAILURE_STATES = frozenset(
    {"error", "build_failed", "destroyed", "deleting"}
)

# Substrings that mark a 400 as *capacity backpressure* (retryable)
# rather than a permanent caller mistake. Daytona's control plane 400s a
# snapshot create with `BadRequestError('No available runners')` when
# every runner in the region is exhausted or below the availability
# score threshold (there is no server-side queue for snapshot creates —
# only builds queue). Kept narrow so a genuinely permanent 400 (bad
# snapshot name, invalid request) still fast-fails.
_CAPACITY_400_MARKERS = ("no available runners",)


def _is_capacity_400(message: str) -> bool:
    """True when a 400 body message denotes transient runner exhaustion."""
    low = message.lower()
    return any(marker in low for marker in _CAPACITY_400_MARKERS)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Server-suggested wait (seconds) from a rate-limit response, or None.

    Honors `Retry-After` (delta-seconds form; HTTP-date form is ignored
    — we fall back to our own backoff) and Daytona/gateway-style
    `X-RateLimit-Reset` / `RateLimit-Reset` (interpreted as an absolute
    epoch-seconds instant if it's in the future, otherwise as a relative
    delta). Never returns a negative value.
    """
    headers = response.headers
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after.strip()))
        except ValueError:
            # HTTP-date form — not worth a full date parse here; let the
            # caller's exponential backoff take over.
            pass
    for key in ("x-ratelimit-reset", "ratelimit-reset"):
        raw = headers.get(key)
        if not raw:
            continue
        try:
            reset = float(raw.strip())
        except ValueError:
            continue
        # A value well above "now" is an absolute epoch instant; a small
        # one is a relative delta. time.time() is only used to tell the
        # two apart and to convert an absolute instant to a delta.
        now = time.time()
        delta = reset - now if reset > now + 1 else reset
        return max(0.0, delta)
    return None


def _derive_proxy_url(api_url: str) -> str:
    """Swap the API URL's port to :4000 when ADK_CC_DAYTONA_PROXY_URL
    is unset. Stock docker-compose publishes the toolbox proxy on the
    same host as the control plane on port 4000."""
    parts = urlsplit(api_url)
    host = parts.hostname or "localhost"
    if parts.scheme not in ("http", "https"):
        # Defensive: malformed input; let the request layer surface it.
        return api_url
    return urlunsplit(
        (parts.scheme, f"{host}:{_DEFAULT_PROXY_PORT}", "", "", "")
    )


class DaytonaBackend(SandboxBackend):
    """Sandbox backend backed by a Daytona deployment.

    See module docstring for the architecture + the routes we
    deliberately don't take.
    """

    name = "daytona"

    def __init__(
        self,
        *,
        session_id: str,
        tenant_id: str,
        api_url: str,
        proxy_url: str,
        api_key: Optional[str] = None,
        credentials: Optional["CredentialProvider"] = None,
        credential_key: str = "daytona_api_key",
        env_spec: Optional["SandboxEnvSpec"] = None,
        snapshot: Optional[str] = None,
        workspace_path: str = "/home/daytona",
        autostop_minutes: int = 15,
        autodelete_minutes: int = 1440,
        delete_on_close: bool = False,
        start_timeout_s: float = 120.0,
        request_timeout_s: float = 30.0,
        create_max_attempts: int = 6,
        create_backoff_base_s: float = 0.5,
        create_backoff_cap_s: float = 8.0,
        create_total_wait_s: float = 45.0,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._session_id = session_id
        self._tenant_id = tenant_id
        self._api_base = api_url.rstrip("/")
        self._proxy_base = proxy_url.rstrip("/")
        # Exactly one of (api_key, credentials) must be provided. The
        # factory validates this; we accept either here for direct ctor
        # use (tests, embedding) without enforcing.
        self._static_token: Optional[str] = api_key
        self._credentials: Optional["CredentialProvider"] = credentials
        self._credential_key = credential_key
        # Env vars / secrets to bake into the sandbox at create time. None or
        # empty → no `env` field sent (unchanged create payload).
        self._env_spec: Optional["SandboxEnvSpec"] = env_spec
        self._snapshot = snapshot
        self._workspace_path = workspace_path
        self._autostop_minutes = int(autostop_minutes)
        self._autodelete_minutes = int(autodelete_minutes)
        self._delete_on_close = bool(delete_on_close)
        self._start_timeout_s = float(start_timeout_s)
        self._request_timeout_s = float(request_timeout_s)
        # Backoff policy for `POST /api/sandbox` under Daytona capacity
        # backpressure (400 "No available runners" — no server-side queue
        # for snapshot creates) and rate limits (429) / server errors
        # (5xx). Bounded so a session bring-up never blocks a tool call
        # indefinitely; the tenancy plugin retries on the next tool call
        # once these are exhausted.
        self._create_max_attempts = max(1, int(create_max_attempts))
        self._create_backoff_base_s = max(0.0, float(create_backoff_base_s))
        self._create_backoff_cap_s = max(0.0, float(create_backoff_cap_s))
        self._create_total_wait_s = max(0.0, float(create_total_wait_s))
        # TLS verify policy. `verify=True` uses certifi (production
        # default); a CA bundle path lets operators trust a private CA;
        # `verify=False` disables verification entirely (dev/test only
        # — self-signed Daytona instances).
        self._verify: Any = ca_bundle if ca_bundle else verify_ssl
        # Host-side workspace prefix captured from the WorkspaceRoot
        # passed to ensure_workspace(). Used to translate host paths
        # (`<workspace_root>/<tenant>/<user>/foo.py`) into Daytona's
        # in-sandbox paths (`<self._workspace_path>/foo.py`). Same role
        # as DockerBackend._workspace_abs_path.
        self._host_workspace: Optional[str] = None
        # Daytona-side sandbox id. Set by `ensure_workspace()` on first
        # call; subsequent calls are no-ops.
        self._sandbox_id: Optional[str] = None
        # Test-injection client; see SandboxServiceBackend's note on why
        # we don't cache an AsyncClient in production (cross-event-loop
        # safety). When set, the same client is used for BOTH api and
        # proxy calls — test fixtures usually drive both with one
        # MockTransport's route dispatcher.
        self._http: Optional[httpx.AsyncClient] = client
        # threading.Lock (not asyncio.Lock) so concurrent first-calls
        # from different event loops don't each create a sandbox.
        self._create_lock = threading.Lock()

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _idem_key() -> str:
        """Fresh idempotency key for a single mutating call. UUIDv4 hex.

        Same convention as SandboxServiceBackend: one key per logical
        operation, not reused across retries (httpx retries — if any —
        would be wrapped at the call site, not here)."""
        return uuid.uuid4().hex

    async def _resolve_token(self) -> str:
        if self._static_token:
            return self._static_token
        if self._credentials is None:
            raise RuntimeError(
                "daytona: no api_key set and no CredentialProvider available — "
                "configure ADK_CC_DAYTONA_API_KEY (single-tenant) or pass a "
                "credentials provider to make_daytona_backend_from_env()."
            )
        token = await self._credentials.get(
            tenant_id=self._tenant_id, key=self._credential_key
        )
        if not token:
            raise RuntimeError(
                f"daytona: no token for tenant {self._tenant_id!r} under "
                f"credential key {self._credential_key!r} — register one in "
                f"the credential store before opening this tenant's sessions"
            )
        return token

    @asynccontextmanager
    async def _client(self):
        """Yield an httpx.AsyncClient. One client serves both the
        control plane and the toolbox proxy — we route by passing
        absolute URLs to each request (no base_url on the client), so
        a single MockTransport in tests can dispatch by request URL.

        Production: build a fresh client per call (cross-event-loop
        safety; see SandboxServiceBackend's note). Test injection: the
        constructor's `client=` kwarg short-circuits this.
        """
        if self._http is not None:
            yield self._http
            return
        token = await self._resolve_token()
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._request_timeout_s,
            verify=self._verify,
        ) as client:
            yield client

    def _api_url(self, path: str) -> str:
        return f"{self._api_base}{path}"

    def _proxy_url(self, path: str) -> str:
        return f"{self._proxy_base}{path}"

    def container_cwd(self, host_abs_path: str) -> str:
        # Daytona runs in its own sandbox rooted at workspace_path.
        return self._workspace_path

    def _to_sandbox_path(self, host_path: str) -> str:
        """Translate a host-side workspace path to its in-sandbox equivalent.

        Tools call read_text / write_text / exec with absolute host
        paths under the WorkspaceRoot (`<root>/<tenant>/<user>/foo.py`).
        Daytona's toolbox expects paths rooted at the sandbox's own
        filesystem (`<self._workspace_path>/foo.py`, default
        `/home/daytona/foo.py`). Same role as DockerBackend's
        `_to_container_path`.

        When the host path is the workspace prefix itself, it maps to
        the in-sandbox workspace_path directly. When the path is
        outside the workspace mount (e.g. `/etc/hostname`), pass it
        through unchanged — the sandbox's own rootfs handles it.
        """
        if not self._host_workspace:
            return host_path
        ws = self._host_workspace
        if host_path == ws:
            return self._workspace_path
        if host_path.startswith(ws + "/"):
            tail = host_path[len(ws) + 1:]
            return f"{self._workspace_path.rstrip('/')}/{tail}"
        return host_path

    def _to_host_path(self, path: str) -> str:
        """Inverse of `_to_sandbox_path`: map a sandbox-domain path back
        to its host-workspace equivalent.

        The allow-check (`_check_allowed`) runs against the host-rooted
        workspace config, but the agent legitimately learns sandbox
        paths from `run_bash` (`pwd` → `/home/daytona`) and may pass
        e.g. `/home/daytona/hello.py` to read/write. Without this, that
        path fails the host-domain allow-check even though it points
        squarely inside the sandbox workspace. We map it back to the
        host view so the check passes; host paths and out-of-workspace
        paths pass through unchanged.
        """
        if not self._host_workspace:
            return path
        sw = self._workspace_path.rstrip("/")
        if path == sw:
            return self._host_workspace
        if path.startswith(sw + "/"):
            tail = path[len(sw) + 1:]
            return f"{self._host_workspace}/{tail}"
        return path

    def _normalize_error(self, response: httpx.Response, op: str) -> None:
        """Map a Daytona error response to our exception model.

        Daytona returns errors as `{path, timestamp, statusCode, error,
        message}`. Two flavors:

          - **Permanent** caller mistakes (401/403/404, and most 400s
            like a bad snapshot name) raise `SandboxViolation` — no
            retry would help.
          - **Transient** backpressure raises `SandboxCapacityError`
            (a SandboxViolation subclass): 429 rate limits, 5xx server
            errors, and the specific 400 "No available runners" capacity
            signal. The create path backs off and retries these; other
            callsites still see them as a SandboxViolation.

        No-op on 2xx.
        """
        if response.status_code < 400:
            return
        try:
            body = response.json()
            msg = body.get("message") or body.get("error") or ""
        except Exception:
            msg = response.text[:200]
        code = response.status_code
        if code == 401:
            raise SandboxViolation(f"daytona auth failed during {op}: {msg}")
        if code == 403:
            raise SandboxViolation(f"daytona forbidden during {op}: {msg}")
        if code == 404:
            raise SandboxViolation(f"daytona not found during {op}: {msg}")
        if code == 429:
            raise SandboxCapacityError(
                f"daytona rate limited during {op}: {msg}",
                retry_after=_parse_retry_after(response),
            )
        if 500 <= code < 600:
            raise SandboxCapacityError(
                f"daytona server error {code} during {op}: {msg}",
                retry_after=_parse_retry_after(response),
            )
        if code == 400 and _is_capacity_400(msg):
            raise SandboxCapacityError(
                f"daytona at capacity during {op}: {msg}",
                retry_after=_parse_retry_after(response),
            )
        raise SandboxViolation(f"daytona {code} during {op}: {msg}")

    def _check_allowed(
        self, path: str, fs_cfg: FsReadConfig | FsWriteConfig, *, op: str
    ) -> None:
        """Raise SandboxViolation if the workspace's allow_paths don't
        cover `path`. Mirrors the client-side fail-fast pattern other
        backends use — surfaces "you're outside the workspace" before
        any HTTP round-trip."""
        if not fs_cfg.allows(path):
            raise SandboxViolation(
                f"daytona: path {path!r} is not in the workspace's "
                f"allowed paths during {op}"
            )

    # --- lifecycle ------------------------------------------------------

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        """Create the Daytona sandbox and poll until state=started.

        Idempotent: a cached `_sandbox_id` short-circuits on second call.
        Serialized via `threading.Lock` so concurrent first-calls from
        different event loops don't each POST a new sandbox.
        """
        # Always capture the host workspace prefix, even on re-entry —
        # _to_sandbox_path() needs it for path translation, and the
        # ws may differ between calls in test fixtures.
        self._host_workspace = ws.abs_path.rstrip("/") if ws.abs_path else None
        if self._sandbox_id is not None:
            return
        with self._create_lock:
            if self._sandbox_id is not None:
                return
            # Build the create body. Daytona rejects resource fields
            # alongside a snapshot; we never send them in v1 (see
            # module docstring). The snapshot is optional — omitting
            # it lets Daytona use its configured default.
            payload: dict[str, Any] = {
                "name": f"adk-cc-{self._session_id}",
                "autoStopInterval": self._autostop_minutes,
                "autoDeleteInterval": self._autodelete_minutes,
            }
            if self._snapshot:
                payload["snapshot"] = self._snapshot
            # Env vars / secrets the agent's in-sandbox commands need (git
            # tokens, vendor API keys, the session user's personal secrets, …).
            # Daytona bakes `env` into the sandbox's container environment, so
            # every later `exec` inherits it. Resolved USER-OVER-TENANT via
            # _runtime_env (operator SandboxEnvSpec + the user's own secrets),
            # so each user gets their own values. Only KEY NAMES are logged.
            resolved_env = await self._runtime_env()
            if resolved_env:
                payload["env"] = resolved_env
                log.info(
                    "daytona: injecting %d env var(s) into sandbox for "
                    "session %s (tenant=%s user=%s): %s",
                    len(resolved_env),
                    self._session_id,
                    self._tenant_id,
                    getattr(self, "_env_user_id", ""),
                    sorted(resolved_env),  # names only
                )
            # One idempotency key for the WHOLE logical create — reused
            # across backoff retries. If a transient 5xx drops the
            # response after Daytona actually created the sandbox, the
            # next attempt's POST dedupes server-side instead of spawning
            # a duplicate (and the 409-adopt path is the belt to that
            # suspenders when the key path doesn't dedupe).
            idem_key = self._idem_key()
            self._sandbox_id = await self._create_with_backoff(payload, idem_key)

    async def _create_with_backoff(
        self, payload: dict[str, Any], idem_key: str
    ) -> str:
        """Run `_attempt_create` under bounded exponential backoff.

        Retries ONLY `SandboxCapacityError` — Daytona capacity
        backpressure (400 "No available runners"), rate limits (429,
        honoring `Retry-After`/`X-RateLimit-Reset`), and 5xx. A permanent
        `SandboxViolation` (bad snapshot, auth, terminal build state) or
        any other error propagates immediately.

        Bounded by both `_create_max_attempts` and a `_create_total_wait_s`
        wall-clock deadline so a bring-up never blocks a tool call
        indefinitely; the tenancy plugin retries on the next tool call
        once we give up.
        """
        deadline = time.monotonic() + self._create_total_wait_s
        for attempt in range(1, self._create_max_attempts + 1):
            try:
                return await self._attempt_create(payload, idem_key)
            except SandboxCapacityError as e:
                if e.retry_after is not None:
                    # Honor the server's directive (small jitter to
                    # de-synchronize a thundering herd of sessions).
                    wait = e.retry_after + random.uniform(0.0, 0.5)
                else:
                    peak = min(
                        self._create_backoff_cap_s,
                        self._create_backoff_base_s * (2 ** (attempt - 1)),
                    )
                    # Equal jitter: half the peak plus a random half.
                    wait = peak / 2 + random.uniform(0.0, peak / 2)
                if attempt >= self._create_max_attempts or (
                    time.monotonic() + wait > deadline
                ):
                    log.warning(
                        "daytona: sandbox create for session %s still under "
                        "backpressure after %d attempt(s) (%s) — giving up; "
                        "tenancy retries on the next tool call",
                        self._session_id,
                        attempt,
                        e,
                    )
                    raise
                log.info(
                    "daytona: sandbox create backpressure for session %s "
                    "(attempt %d/%d): %s — backing off %.1fs",
                    self._session_id,
                    attempt,
                    self._create_max_attempts,
                    e,
                    wait,
                )
                await asyncio.sleep(wait)
        # The loop body always returns or raises; this only satisfies the
        # type checker that the method returns a str on every path.
        raise RuntimeError("daytona: create backoff loop exited unexpectedly")

    async def _attempt_create(
        self, payload: dict[str, Any], idem_key: str
    ) -> str:
        """A single create + poll-until-started attempt; returns the id.

        Opens a fresh client per attempt (a 5xx/timeout may have left the
        previous connection unusable). Raises `SandboxCapacityError` on
        transient backpressure so `_create_with_backoff` retries; other
        failures are permanent.
        """
        async with self._client() as client:
            resp = await client.post(
                self._api_url("/api/sandbox"),
                json=payload,
                headers={"Idempotency-Key": idem_key},
            )
            if resp.status_code == 409:
                # A sandbox with this name already exists — typical
                # case is a server restart where Daytona kept the
                # sandbox alive (autodelete hasn't fired) but our
                # in-process `_sandbox_id` was reset. Adopt the
                # existing one instead of failing the session.
                sandbox_id = await self._find_sandbox_id_by_name(
                    client, payload["name"]
                )
                if not sandbox_id:
                    # The name conflict resolved nothing — surface
                    # the original 409 with its body.
                    self._normalize_error(resp, op="create_sandbox")
                log.info(
                    "daytona: adopted existing sandbox %s for adk-cc "
                    "session %s (tenant=%s) after 409",
                    sandbox_id,
                    self._session_id,
                    self._tenant_id,
                )
                # The sandbox might be stopped (autostop fired). Best-
                # effort wake it; ignore errors so an already-running
                # one passes through.
                await self._wake_if_stopped(client, sandbox_id)
            else:
                self._normalize_error(resp, op="create_sandbox")
                body = resp.json()
                sandbox_id = body.get("id")
                if not sandbox_id:
                    raise RuntimeError(
                        f"daytona: create_sandbox response missing `id`: {body!r}"
                    )
                log.info(
                    "daytona: created sandbox %s for adk-cc session %s "
                    "(tenant=%s, snapshot=%s, state=%s)",
                    sandbox_id,
                    self._session_id,
                    self._tenant_id,
                    body.get("snapshot"),
                    body.get("state"),
                )
            # Poll the same client (avoid reopening the connection).
            await self._poll_until_started(client, sandbox_id)
            # Best-effort: make sure the in-sandbox workspace dir
            # exists and is writable before any file op targets it.
            await self._ensure_workspace_dir(client, sandbox_id)
        return sandbox_id

    async def _ensure_workspace_dir(
        self, client: httpx.AsyncClient, sandbox_id: str
    ) -> None:
        """`mkdir -p <workspace_path>` inside the sandbox.

        Daytona's default workspace (/home/daytona) already exists and is
        owned by the sandbox user. A custom workspace path needs the dir
        to exist before files/upload can write into it:

          - A path under a writable parent (e.g. /home/daytona/work) is
            created here at runtime — no snapshot change needed.
          - A top-level path (e.g. /workspace) CANNOT be created by the
            non-root sandbox user; it must be prepared in the snapshot
            (`mkdir -p /workspace && chown daytona:daytona /workspace` in
            Dockerfile.daytona-snapshot). If mkdir fails we log an
            actionable warning rather than hard-failing — the subsequent
            file op's 400 would otherwise be opaque.
        """
        try:
            resp = await client.post(
                self._proxy_url(f"/toolbox/{sandbox_id}/process/execute"),
                json={"command": f"mkdir -p {shlex.quote(self._workspace_path)}"},
            )
            if resp.status_code >= 400:
                log.warning(
                    "daytona: could not prepare workspace dir %r (HTTP %s): %s",
                    self._workspace_path,
                    resp.status_code,
                    resp.text[:200],
                )
                return
            data = resp.json()
            if int(data.get("exitCode", 0)) != 0:
                log.warning(
                    "daytona: workspace dir %r is not creatable by the "
                    "sandbox user (mkdir exit %s: %s). A top-level path must "
                    "be created + chowned in the snapshot "
                    "(Dockerfile.daytona-snapshot); file ops will 400 until "
                    "then. Set ADK_CC_DAYTONA_WORKSPACE_PATH to a path under "
                    "the user's home, or fix the snapshot.",
                    self._workspace_path,
                    data.get("exitCode"),
                    (data.get("result") or "")[:200],
                )
        except Exception as e:  # noqa: BLE001 — diagnostic only, never fatal
            log.warning(
                "daytona: workspace-dir preflight failed for %r: %s",
                self._workspace_path,
                e,
            )

    async def _find_sandbox_id_by_name(
        self, client: httpx.AsyncClient, name: str
    ) -> Optional[str]:
        """List sandboxes and return the id of the one matching `name`.

        Used on 409 from create_sandbox to recover the existing id when
        our in-memory cache was lost (server restart). Returns None if
        Daytona doesn't return a matching record — caller should then
        surface the original 409 rather than silently spinning."""
        try:
            resp = await client.get(self._api_url("/api/sandbox"))
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            items = resp.json()
        except Exception:
            return None
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and item.get("name") == name:
                sid = item.get("id")
                if isinstance(sid, str) and sid:
                    return sid
        return None

    async def _wake_if_stopped(
        self, client: httpx.AsyncClient, sandbox_id: str
    ) -> None:
        """POST /api/sandbox/{id}/start when the sandbox is stopped.

        Daytona auto-stops idle sandboxes after `autoStopInterval`
        minutes; an adopted sandbox is usually in `stopped` state when
        we recover it after a restart. Best-effort: failures here are
        logged but don't raise — the subsequent poll-until-started
        either succeeds (if the start kicked in) or surfaces a clearer
        terminal-state error."""
        try:
            resp = await client.get(self._api_url(f"/api/sandbox/{sandbox_id}"))
            if resp.status_code >= 400:
                return
            state = (resp.json() or {}).get("state")
        except Exception:
            return
        if state == "started":
            return
        try:
            start_resp = await client.post(
                self._api_url(f"/api/sandbox/{sandbox_id}/start"),
                headers={"Idempotency-Key": self._idem_key()},
            )
            if start_resp.status_code >= 400:
                log.warning(
                    "daytona: wake start returned %s for sandbox %s: %s",
                    start_resp.status_code,
                    sandbox_id,
                    start_resp.text[:300],
                )
        except httpx.HTTPError as e:
            log.warning(
                "daytona: wake start transport error for sandbox %s: %s",
                sandbox_id,
                e,
            )

    async def _poll_until_started(
        self, client: httpx.AsyncClient, sandbox_id: str
    ) -> None:
        """Exponential-backoff poll on `GET /api/sandbox/{id}` until
        state=started or a terminal failure state.

        Initial wait 0.5s, doubling, capped at 5s; total deadline is
        `self._start_timeout_s`.
        """
        delay = 0.5
        deadline = time.monotonic() + self._start_timeout_s
        while time.monotonic() < deadline:
            resp = await client.get(self._api_url(f"/api/sandbox/{sandbox_id}"))
            self._normalize_error(resp, op="poll_sandbox")
            body = resp.json()
            state = body.get("state")
            if state == "started":
                return
            if state in _TERMINAL_FAILURE_STATES:
                reason = (
                    body.get("errorReason")
                    or body.get("reason")
                    or "<no reason>"
                )
                raise SandboxViolation(
                    f"daytona: sandbox {sandbox_id} entered terminal state "
                    f"{state!r}: {reason}"
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
        raise SandboxViolation(
            f"daytona: sandbox {sandbox_id} not started after "
            f"{self._start_timeout_s}s (last state was transient)"
        )

    async def close(self) -> None:
        """Best-effort stop / delete. Never raises.

        Default: POST stop (preserves the sandbox for resume; Daytona's
        own autoDeleteInterval reaper handles eventual cleanup).
        Set ADK_CC_DAYTONA_DELETE_ON_CLOSE=1 to issue DELETE instead —
        useful for ephemeral CI-style usage where you want the storage
        reclaimed immediately.
        """
        if self._sandbox_id is None:
            return
        sandbox_id = self._sandbox_id
        try:
            async with self._client() as client:
                if self._delete_on_close:
                    await client.delete(
                        self._api_url(f"/api/sandbox/{sandbox_id}"),
                        headers={"Idempotency-Key": self._idem_key()},
                    )
                else:
                    await client.post(
                        self._api_url(f"/api/sandbox/{sandbox_id}/stop"),
                        headers={"Idempotency-Key": self._idem_key()},
                    )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning(
                "daytona: close (%s) on sandbox %s failed (best-effort): %s",
                "delete" if self._delete_on_close else "stop",
                sandbox_id,
                e,
            )

    # --- ABC methods ----------------------------------------------------

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        """Synchronous exec via the toolbox proxy's POST
        /toolbox/{id}/process/execute endpoint.

        Daytona returns `{exitCode, result}` with stdout+stderr merged
        in `result`. We surface `result` on ExecResult.stdout with
        stderr="" — callers can't reliably split the streams.

        Transport / 4xx / 5xx errors all synthesize an
        ExecResult(exit_code=-1, stderr=<error>) rather than raise; this
        matches SandboxServiceBackend's convention so callers have one
        error-handling pattern.
        """
        await self.ensure_workspace_inferred()
        # cwd from the tool layer is a host-side workspace path; translate
        # to the in-sandbox equivalent. Falls back to the in-sandbox
        # workspace root when the caller passed no cwd.
        body: dict[str, Any] = {
            "command": cmd,
            "cwd": self._to_sandbox_path(cwd) if cwd else self._workspace_path,
        }
        if timeout_s and timeout_s > 0:
            # Daytona's spec calls this `timeout` (seconds); we mirror.
            body["timeout"] = int(timeout_s)
        # On-demand env injection (resolve-at-exec): merge the session user's
        # secrets (user-over-tenant) + operator SandboxEnvSpec into THIS
        # command's env, via the toolbox execute `env` map. TTL-cached, so a
        # secret provided after sandbox creation reaches the next command
        # without recreating the sandbox. Empty → field omitted (unchanged).
        runtime_env = await self._runtime_env()
        if runtime_env:
            body["env"] = runtime_env
        # `network` arg is not sent per-call: Daytona enforces network
        # policy at sandbox-create time (networkBlockAll / networkAllowList),
        # not per-exec. Documented in the module docstring as a known
        # behavioral asymmetry vs other backends.
        del network  # unused; kept in signature to satisfy the ABC
        del fs_write  # write scoping likewise enforced sandbox-wide
        try:
            async with self._client() as client:
                resp = await client.post(
                    self._proxy_url(f"/toolbox/{self._sandbox_id}/process/execute"),
                    json=body,
                )
        except httpx.HTTPError as e:
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"daytona: exec transport error: {e}",
                timed_out=False,
            )
        if resp.status_code >= 400:
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=(
                    f"daytona: exec returned {resp.status_code}: {resp.text}"
                ),
                timed_out=False,
            )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        exit_code = int(data.get("exitCode", -1))
        result_text = data.get("result", "") or ""
        return ExecResult(
            exit_code=exit_code,
            stdout=result_text,
            stderr="",
            timed_out=False,
        )

    async def read_text(self, path: str, *, fs_read: FsReadConfig) -> str:
        raw = await self.read_bytes(path, fs_read=fs_read)
        return raw.decode("utf-8", errors="replace")

    async def read_bytes(self, path: str, *, fs_read: FsReadConfig) -> bytes:
        # Allow-check in the host domain — map a sandbox-domain path
        # (e.g. the agent passing `/home/daytona/x` from `pwd`) back to
        # its host equivalent first so it isn't spuriously rejected.
        self._check_allowed(self._to_host_path(path), fs_read, op="read_bytes")
        await self.ensure_workspace_inferred()
        sandbox_path = self._to_sandbox_path(path)
        async with self._client() as client:
            try:
                resp = await client.get(
                    self._proxy_url(f"/toolbox/{self._sandbox_id}/files/download"),
                    params={"path": sandbox_path},
                )
            except httpx.HTTPError as e:
                raise RuntimeError(
                    f"daytona: read_bytes transport error for "
                    f"{sandbox_path!r} (requested {path!r}): {e}"
                ) from e
            if resp.status_code == 404:
                raise FileNotFoundError(path)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"daytona: read_bytes returned {resp.status_code} for "
                    f"sandbox path {sandbox_path!r} (requested {path!r}): "
                    f"{resp.text}"
                )
            return resp.content

    async def write_text(
        self, path: str, content: str, *, fs_write: FsWriteConfig
    ) -> None:
        await self.write_bytes(
            path, content.encode("utf-8"), fs_write=fs_write
        )

    async def write_bytes(
        self, path: str, content: bytes, *, fs_write: FsWriteConfig
    ) -> None:
        self._check_allowed(self._to_host_path(path), fs_write, op="write_bytes")
        await self.ensure_workspace_inferred()
        sandbox_path = self._to_sandbox_path(path)
        # Multipart upload: `path` is a QUERY parameter (not a form
        # field — easy to get wrong; Daytona's OpenAPI marks it as
        # `in: query`). The form field name is `file`; the filename is
        # cosmetic (server uses the query path for placement).
        files = {
            "file": ("payload", content, "application/octet-stream"),
        }
        async with self._client() as client:
            try:
                resp = await client.post(
                    self._proxy_url(f"/toolbox/{self._sandbox_id}/files/upload"),
                    params={"path": sandbox_path},
                    files=files,
                )
            except httpx.HTTPError as e:
                raise RuntimeError(
                    f"daytona: write_bytes transport error for "
                    f"{sandbox_path!r} (requested {path!r}): {e}"
                ) from e
            if resp.status_code >= 400:
                # Show the IN-SANDBOX path Daytona actually received, not
                # just the requested host path — a 400 here usually means
                # that sandbox dir isn't writable by the sandbox user
                # (e.g. ADK_CC_DAYTONA_WORKSPACE_PATH points outside the
                # user's home and the snapshot didn't create/chown it).
                raise RuntimeError(
                    f"daytona: write_bytes returned {resp.status_code} for "
                    f"sandbox path {sandbox_path!r} (requested {path!r}): "
                    f"{resp.text}"
                )

    # --- internal -------------------------------------------------------

    async def ensure_workspace_inferred(self) -> None:
        """No-op if the sandbox already exists.

        Tools may call exec / read_text / write_text without an explicit
        prior call to `ensure_workspace()` (the runner usually does it
        eagerly, but defensive code paths exist). If `_sandbox_id` is
        still None here, we can't proceed — raise rather than silently
        create a sandbox with empty fs config.
        """
        if self._sandbox_id is None:
            raise RuntimeError(
                "daytona: backend used before ensure_workspace() — the "
                "runner should call ensure_workspace() at session start"
            )


def make_daytona_backend_from_env(
    *,
    session_id: str,
    tenant_id: str,
    credentials: Optional["CredentialProvider"] = None,
) -> DaytonaBackend:
    """Construct from `ADK_CC_DAYTONA_*` env vars.

    Required:
      - ADK_CC_DAYTONA_API_URL
      - ONE OF:
        - ADK_CC_DAYTONA_API_KEY — single-tenant / dev shared bearer.
        - `credentials` parameter — production multi-tenant: token
          resolved per `(tenant_id, key)` from the credential provider.
          Key defaults to `daytona_api_key`; override via
          ADK_CC_DAYTONA_CREDENTIAL_KEY.

    Optional:
      - ADK_CC_DAYTONA_PROXY_URL          — toolbox proxy base; default
                                            derived by swapping :3000
                                            → :4000 on the API URL.
      - ADK_CC_DAYTONA_SNAPSHOT           — snapshot id/name; default
                                            uses Daytona's configured
                                            default (see GET /api/config).
      - ADK_CC_DAYTONA_WORKSPACE_PATH     — in-sandbox cwd; default
                                            `/home/daytona`.
      - ADK_CC_DAYTONA_AUTOSTOP_MIN       — default 15.
      - ADK_CC_DAYTONA_AUTODELETE_MIN     — default 1440 (24h).
      - ADK_CC_DAYTONA_DELETE_ON_CLOSE    — "1" to DELETE instead of stop.
      - ADK_CC_DAYTONA_START_TIMEOUT_S    — default 120.
      - ADK_CC_DAYTONA_REQUEST_TIMEOUT_S  — default 30.
      - ADK_CC_DAYTONA_CREATE_MAX_ATTEMPTS   — create backoff attempt
                                            cap under Daytona capacity /
                                            rate-limit backpressure;
                                            default 6.
      - ADK_CC_DAYTONA_CREATE_TOTAL_WAIT_S   — total wall-clock cap across
                                            all create retries; default 45.

    Sandbox environment (backend-agnostic; see sandbox/sandbox_env.py).
    These bake env vars / per-tenant secrets into the sandbox at create
    time so in-sandbox commands (git, vendor CLIs) have what they need:
      - ADK_CC_SANDBOX_ENV               — static KEY=VALUE,… (or JSON).
      - ADK_CC_SANDBOX_ENV_PASSTHROUGH   — host env var names to copy.
      - ADK_CC_SANDBOX_ENV_CREDENTIALS   — ENV_NAME=credential_key,… ;
                                           values resolved per-tenant from
                                           the CredentialProvider.

    Resource fields (cpu/memory/disk) are NOT exposed via env in v1:
    Daytona's API rejects them alongside a snapshot, and v1 always
    routes through snapshots. Custom resource sizing goes through a
    `buildInfo` build in v2.
    """
    api_url = os.environ.get("ADK_CC_DAYTONA_API_URL")
    if not api_url:
        raise RuntimeError(
            "ADK_CC_SANDBOX_BACKEND=daytona requires ADK_CC_DAYTONA_API_URL"
        )
    proxy_url = (
        os.environ.get("ADK_CC_DAYTONA_PROXY_URL")
        or _derive_proxy_url(api_url)
    )

    static_token = os.environ.get("ADK_CC_DAYTONA_API_KEY")
    credential_key = os.environ.get(
        "ADK_CC_DAYTONA_CREDENTIAL_KEY", "daytona_api_key"
    )
    if not static_token and credentials is None:
        raise RuntimeError(
            "ADK_CC_SANDBOX_BACKEND=daytona requires either "
            "ADK_CC_DAYTONA_API_KEY (single-tenant / dev) or a "
            "CredentialProvider passed to the factory (production)."
        )

    def _int_env(key: str, default: int) -> int:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError as e:
            raise RuntimeError(f"{key}={raw!r} is not a valid int: {e}") from e

    def _float_env(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError as e:
            raise RuntimeError(f"{key}={raw!r} is not a valid float: {e}") from e

    # Env/secret injection is backend-agnostic: the same ADK_CC_SANDBOX_ENV*
    # knobs feed every backend. Pass the credential provider through for
    # per-tenant secret resolution EVEN in static-token mode (token
    # resolution still prefers the static token; the provider is only used
    # to look up sandbox-env secrets here).
    from ..sandbox_env import sandbox_env_spec_from_env

    env_spec = sandbox_env_spec_from_env()

    return DaytonaBackend(
        session_id=session_id,
        tenant_id=tenant_id,
        api_url=api_url,
        proxy_url=proxy_url,
        api_key=static_token,
        credentials=credentials,
        credential_key=credential_key,
        env_spec=env_spec,
        snapshot=os.environ.get("ADK_CC_DAYTONA_SNAPSHOT") or None,
        workspace_path=os.environ.get(
            "ADK_CC_DAYTONA_WORKSPACE_PATH", "/home/daytona"
        ),
        autostop_minutes=_int_env("ADK_CC_DAYTONA_AUTOSTOP_MIN", 15),
        autodelete_minutes=_int_env("ADK_CC_DAYTONA_AUTODELETE_MIN", 1440),
        delete_on_close=os.environ.get("ADK_CC_DAYTONA_DELETE_ON_CLOSE") == "1",
        start_timeout_s=_float_env("ADK_CC_DAYTONA_START_TIMEOUT_S", 120.0),
        request_timeout_s=_float_env("ADK_CC_DAYTONA_REQUEST_TIMEOUT_S", 30.0),
        create_max_attempts=_int_env("ADK_CC_DAYTONA_CREATE_MAX_ATTEMPTS", 6),
        # backoff base/cap use the ctor defaults (0.5 / 8.0); MAX_ATTEMPTS +
        # TOTAL_WAIT already bound the loop, so they aren't env-tunable.
        create_total_wait_s=_float_env(
            "ADK_CC_DAYTONA_CREATE_TOTAL_WAIT_S", 45.0
        ),
        # TLS: default verify=True (certifi). For a private CA, point
        # ADK_CC_DAYTONA_CA_BUNDLE at the PEM. For a self-signed
        # dev/test Daytona, ADK_CC_DAYTONA_VERIFY_SSL=0 disables verify
        # entirely (use only when the network path is otherwise trusted).
        verify_ssl=os.environ.get("ADK_CC_DAYTONA_VERIFY_SSL", "1") != "0",
        ca_bundle=os.environ.get("ADK_CC_DAYTONA_CA_BUNDLE") or None,
    )
