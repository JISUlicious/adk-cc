"""Backend that delegates to an external sandbox service over REST.

Targets the JISUlicious/sandboxing service (or any compatible service
exposing the same `/v1/sessions/...` surface). Operations:

  agent process                      sandbox service host
  ─────────────────────              ──────────────────────────
  SandboxServiceBackend ── HTTPS ──► FastAPI control plane
                                       │
                                       ▼
                                     gVisor container
                                     + per-session named volume
                                     + Squid egress filter

The agent never reaches the sandbox host directly; everything goes
through the service's authenticated REST surface. We use REST (not the
service's `/mcp` endpoint) because adk-cc is a programmatic consumer —
the MCP transport is for direct LLM-driven clients (Claude Desktop,
Cursor) where the model itself is the audience for the 10 tools. From
Python, MCP framing is dead weight.

Per-session binding: each `SandboxServiceBackend` instance maps to one
adk-cc session, which maps 1:1 to one upstream service session. The
service auto-resumes a STOPPED session on the next exec/file call.

Trade-offs vs DockerBackend:

  - +  Stronger isolation (gVisor + cap-drop + read-only rootfs +
       userns-remap + per-tenant Squid allowlist). Service-managed.
  - +  Single env-var swap; no Docker-on-the-agent dependency.
  - +  Multi-tenant on the wire (since upstream PR #10): each
       adk-cc tenant maps to a distinct service-side tenant with
       its own scoped token, audit log, and Squid allowlist. The
       SHARED_TOKEN env var remains as a dev/single-tenant escape
       hatch. Token resolution falls back through the credential
       provider for production deployments.
  - −  Persistence ceiling: per-tenant `max_workspace_gib` is
       configurable (TenantLimits) but `hard_destroy_ttl_s` is still
       a global service knob (default 86400s = 24h of inactivity).
       DockerBackend uses the host-mounted per-user directory, which
       persists indefinitely.
  - −  No streaming exec for adk-cc tools today. The service exposes
       SSE at `/exec/stream` and MCP `progress` notifications via
       `progressToken`, but `SandboxBackend.exec` is sync. Agent
       waits on full stdout/stderr before returning. Background-
       process logs side-step this for long-running workloads (the
       service has a process API, not yet surfaced in adk-cc tools).

Idempotency: every mutating request (POST /v1/sessions, POST exec,
POST/DELETE files) sends an `Idempotency-Key` header so transient
network glitches retry safely. The service replays the cached response
for the same key inside its TTL, returning `Idempotent-Replay: 1` on
the replay.

See `docs/04-deployment-sandbox.md` for the operator setup story
(upstream Path A / B / C).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx

from ..config import (
    ExecChunk,
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxViolation,
)
from .base import SandboxBackend
from ...config.schema import env_bool

if TYPE_CHECKING:
    from ...credentials import CredentialProvider
    from ..workspace import WorkspaceRoot

log = logging.getLogger(__name__)

CONTAINER_WORKSPACE = "/workspace"
DEFAULT_REQUEST_TIMEOUT_S = 30.0
DEFAULT_EXEC_TIMEOUT_S = 60


def _humanize_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n // (1024 * 1024)} MiB"
    if n >= 1024:
        return f"{n // 1024} KiB"
    return f"{n} B"


class SandboxServiceBackend(SandboxBackend):
    """REST client for the JISUlicious/sandboxing service."""

    name = "sandbox_service"

    def __init__(
        self,
        *,
        base_url: str,
        api_token: Optional[str] = None,
        credentials: Optional["CredentialProvider"] = None,
        credential_key: str = "sandbox_service_token",
        session_id: str = "local",
        tenant_id: str = "local",
        verify_tls: bool = True,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        exec_default_timeout_s: int = DEFAULT_EXEC_TIMEOUT_S,
        limits: Optional[dict[str, Any]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Construct a backend bound to one adk-cc session.

        Token resolution is one of:
          - `api_token` set: static token (dev / single-tenant deployments).
          - `credentials` set: per-tenant token resolved from the credential
            provider at first call, keyed on `(tenant_id, credential_key)`.
            Recommended for production deployments where the upstream
            sandbox service has per-tenant scoped tokens (since
            JISUlicious/sandboxing PR #10).
        """
        if not base_url:
            raise ValueError("base_url is required")
        if not api_token and credentials is None:
            raise ValueError(
                "either api_token (static) or credentials (per-tenant lookup) "
                "must be provided"
            )
        self._base_url = base_url.rstrip("/")
        self._static_token = api_token
        self._credentials = credentials
        self._credential_key = credential_key
        self._session_id = session_id
        self._tenant_id = tenant_id
        self._verify_tls = verify_tls
        self._request_timeout_s = request_timeout_s
        self._exec_default_timeout_s = exec_default_timeout_s
        self._limits = dict(limits) if limits else {}
        # Workspace path on the AGENT host (not the service). Used to
        # translate absolute paths the tool layer passes us into the
        # /workspace-relative paths the service understands.
        self._workspace_abs_path: Optional[str] = None
        # Service-side session id (a string — loop-portable). Set by
        # `_ensure_session()` on first call.
        self._service_session_id: Optional[str] = None
        # Test-injection override. When set, this client is reused for
        # all calls (caller owns lifecycle). When unset (production),
        # each method creates a fresh client per call.
        #
        # Why no caching in production: `httpx.AsyncClient` carries an
        # internal `asyncio.Event` for connection-pool state, which
        # binds to the event loop it was first used in. Reusing it
        # across loops fails with `RuntimeError: ... is bound to a
        # different event loop`. `SandboxBackedCodeExecutor` runs us
        # inside `asyncio.run` on a worker thread (so a script exec
        # gets a fresh loop per call), so any cached client from the
        # main loop blows up on the second access. Per-call client
        # construction costs ~1 ms on loopback; negligible vs. the
        # 80–500 ms exec round-trip.
        self._http: Optional[httpx.AsyncClient] = client
        # threading.Lock (NOT asyncio.Lock) — survives across event
        # loops. Used to serialize the session_create RTT so a flurry
        # of concurrent first-calls doesn't create N upstream sessions.
        # Held briefly across one HTTP POST; never re-entered.
        self._session_create_lock = threading.Lock()

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _idem_key() -> str:
        """Fresh idempotency key for a single mutating call.

        The upstream service replays the cached response when the same
        key arrives within the TTL window (PR #7 follow-up). We mint
        one per logical call rather than reusing across retries — httpx
        retries are wrapped at the call site, not here, so giving each
        callsite a unique key matches the "retry the same logical
        operation" semantics the service expects.
        """
        return uuid.uuid4().hex

    async def _resolve_token(self) -> str:
        if self._static_token:
            return self._static_token
        assert self._credentials is not None  # ctor enforces one of the two
        token = await self._credentials.get(
            tenant_id=self._tenant_id, key=self._credential_key
        )
        if not token:
            raise RuntimeError(
                f"sandbox_service: no token for tenant {self._tenant_id!r} "
                f"under credential key {self._credential_key!r} — register one "
                f"via the admin API before opening this tenant's sessions"
            )
        return token

    @asynccontextmanager
    async def _client_ctx(self):
        """Yield an httpx.AsyncClient bound to the *current* loop.

        Test-injection mode: when the constructor was passed a
        pre-built `client`, yield that one (caller owns lifecycle, we
        don't `aclose()` it). Production mode: build a fresh client
        per call and close it on exit. Per-call construction is
        deliberate — see the long note in `__init__`.
        """
        if self._http is not None:
            yield self._http
            return
        token = await self._resolve_token()
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._request_timeout_s,
            verify=self._verify_tls,
        ) as client:
            yield client

    def container_cwd(self, host_abs_path: str) -> str:
        # The service mounts the workspace at CONTAINER_WORKSPACE.
        return CONTAINER_WORKSPACE

    def _to_container_path(self, host_path: str) -> str:
        """Translate an agent-side absolute path to a /workspace-relative path.

        Returns the path WITHOUT the leading `/workspace` — callers join
        it into a URL or compose `/workspace/<rel>` for cwd as needed.

        Raises `SandboxViolation` if the path escapes the workspace; the
        service would 400 anyway, but failing fast surfaces the cause
        before the round-trip.
        """
        if self._workspace_abs_path is None:
            # No bind known yet — assume host_path is already relative.
            # Reject leading slashes so we never accidentally request
            # `/v1/sessions/.../files//abs/path`.
            return host_path.lstrip("/")
        ws = self._workspace_abs_path.rstrip("/")
        if host_path == ws:
            return ""
        if host_path.startswith(ws + "/"):
            return host_path[len(ws) + 1 :]
        # Allow paths that are already container-relative `/workspace/...`
        # so callers passing the translated form continue to work.
        if host_path.startswith(CONTAINER_WORKSPACE + "/"):
            return host_path[len(CONTAINER_WORKSPACE) + 1 :]
        if host_path == CONTAINER_WORKSPACE:
            return ""
        raise SandboxViolation(
            f"path {host_path!r} is outside workspace {ws!r}"
        )

    def _container_cwd(self, host_cwd: str) -> str:
        rel = self._to_container_path(host_cwd)
        if not rel:
            return CONTAINER_WORKSPACE
        return str(PurePosixPath(CONTAINER_WORKSPACE) / rel)

    async def _ensure_session(self) -> str:
        """Create the upstream session if not yet created. Returns its id.

        Cached id is a plain string, so reads are safe across loops.
        Creation is serialized via a `threading.Lock` (not asyncio) so
        concurrent first-calls from different threads/loops don't
        each POST a new session.
        """
        # Fast path — string read is loop-portable.
        if self._service_session_id is not None:
            return self._service_session_id
        with self._session_create_lock:
            # Double-check under the lock: another caller may have
            # already created the session while we waited.
            if self._service_session_id is not None:
                return self._service_session_id
            payload: dict[str, Any] = {}
            if self._limits:
                payload["limits"] = self._limits
            # If the operator passed a client-supplied session id, prefer
            # it so the upstream id mirrors the adk-cc session id. The
            # service may ignore unknown keys; harmless fallback.
            payload["session_id"] = self._session_id
            async with self._client_ctx() as client:
                try:
                    resp = await client.post(
                        "/v1/sessions",
                        json=payload,
                        headers={"Idempotency-Key": self._idem_key()},
                    )
                except httpx.HTTPError as e:
                    raise RuntimeError(
                        f"sandbox_service: session_create failed: {e}"
                    ) from e
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"sandbox_service: session_create returned "
                        f"{resp.status_code}: {resp.text}"
                    )
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                # Upstream's SessionResponse.required field is `session_id`
                # (per OpenAPI). We also accept `id` for back-compat with
                # older builds and other clients. Falling back to the
                # client-supplied `self._session_id` would be wrong — the
                # upstream may have assigned a different ULID and that's
                # the one that subsequent /v1/sessions/<sid>/... calls
                # need to use. If neither field is present, fail loudly
                # rather than silently use a stale id.
                sid = body.get("session_id") or body.get("id")
                if not sid:
                    raise RuntimeError(
                        f"sandbox_service: session_create response missing "
                        f"both `session_id` and `id`: {body!r}"
                    )
                self._service_session_id = sid
                log.info(
                    "sandbox_service: created upstream session %s for adk-cc "
                    "session %s (tenant=%s)",
                    sid,
                    self._session_id,
                    self._tenant_id,
                )
                return sid

    # --- ABC methods ----------------------------------------------------

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        # Remember the agent-side workspace prefix so we can translate
        # absolute paths in subsequent calls.
        self._workspace_abs_path = ws.abs_path
        # Eagerly bring up the service-side session. Fail fast at session
        # start rather than on the first exec.
        await self._ensure_session()

    def _build_exec_body(self, cmd: str, cwd: str, timeout_s: int) -> dict[str, Any]:
        """Shared argv/timeout shaping for sync `exec` and streaming
        `exec_stream`. Wraps the caller's shell-style command in
        `bash -lc` and prepends a `cd` if cwd != /workspace."""
        container_cwd = self._container_cwd(cwd)
        argv = ["/bin/bash", "-lc", cmd]
        if container_cwd != CONTAINER_WORKSPACE:
            quoted = container_cwd.replace("'", "'\\''")
            argv = ["/bin/bash", "-lc", f"cd '{quoted}' && {cmd}"]
        return {
            "argv": argv,
            "timeout_s": int(timeout_s) if timeout_s else self._exec_default_timeout_s,
        }

    @staticmethod
    def _truncation_note(data: dict[str, Any]) -> str:
        """Produce the `[sandbox_service: output truncated…]` stderr
        suffix when `truncated=True`. Empty string otherwise. Shared
        by sync `exec` and streaming `exec_stream` so both surface
        the same warning text."""
        if not data.get("truncated"):
            return ""
        cap_bytes = int(
            data.get("effective_truncation_cap_bytes") or (8 * 1024 * 1024)
        )
        cap_human = _humanize_bytes(cap_bytes)
        truncated_streams = data.get("truncated_streams") or []
        streams_msg = (
            f" ({', '.join(truncated_streams)})" if truncated_streams else ""
        )
        return (
            f"\n[sandbox_service: output truncated by service "
            f"(>{cap_human} per stream{streams_msg}); rerun with "
            f"redirection to a file]"
        )

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        sid = await self._ensure_session()
        try:
            body = self._build_exec_body(cmd, cwd, timeout_s)
        except SandboxViolation:
            # Tool layer will catch and surface; preserve the message.
            raise
        async with self._client_ctx() as client:
            try:
                resp = await client.post(
                    f"/v1/sessions/{sid}/exec",
                    json=body,
                    headers={"Idempotency-Key": self._idem_key()},
                )
            except httpx.HTTPError as e:
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"sandbox_service: exec transport error: {e}",
                    timed_out=False,
                )
            if resp.status_code >= 400:
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr=(
                        f"sandbox_service: exec returned {resp.status_code}: "
                        f"{resp.text}"
                    ),
                    timed_out=False,
                )
            try:
                data = resp.json()
            except ValueError:
                data = {}
            stdout = data.get("stdout", "") or ""
            stderr = data.get("stderr", "") or ""
            exit_code = int(data.get("exit_code", -1))
            stderr += self._truncation_note(data)
            # `resume_latency_ms` (≥0, default 0) tells us how much of the
            # round-trip was the service waking up a STOPPED session vs.
            # actually executing. Log when it's non-trivial so audits can
            # distinguish "model is slow" from "service had to resume."
            resume_ms = int(data.get("resume_latency_ms") or 0)
            if resume_ms >= 250:
                log.info(
                    "sandbox_service: exec on %s included %d ms of session "
                    "resume",
                    sid,
                    resume_ms,
                )
            # The service signals timeouts inside its own ExecResponse shape
            # (typically exit_code=-1 with a stderr note); we don't try to
            # special-case here. `timed_out` stays False unless the agent
            # round-trip itself exceeded the budget.
            return ExecResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
            )

    async def exec_stream(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> AsyncIterator[ExecChunk]:
        """Stream stdout/stderr chunks from the upstream
        `POST /v1/sessions/<sid>/exec/stream` SSE endpoint, terminating
        with one `kind="result"` chunk carrying the full ExecResult.

        The upstream emits three event kinds:
          event: stdout / stderr  →  data: {"chunk_b64": "..."}
          event: result           →  data: {ExecResponse JSON}

        Yields:
          ExecChunk(kind="stdout", data=<decoded text>)
          ExecChunk(kind="stderr", data=<decoded text>)
          ExecChunk(kind="result", result=ExecResult(...))   ← always last

        On transport / HTTP errors, yields a single `result` chunk with
        a synthesized ExecResult carrying the error in stderr — same
        shape as the sync `exec` failure path so callers can write one
        error-handling pattern.
        """
        sid = await self._ensure_session()
        try:
            body = self._build_exec_body(cmd, cwd, timeout_s)
        except SandboxViolation:
            raise

        url = f"/v1/sessions/{sid}/exec/stream"
        # Track aggregated streams so the final result chunk is
        # accurate even if the service's `event: result` is malformed.
        agg_stdout: list[str] = []
        agg_stderr: list[str] = []
        try:
            async with self._client_ctx() as client, client.stream(
                "POST",
                url,
                json=body,
                headers={"Idempotency-Key": self._idem_key()},
            ) as resp:
                if resp.status_code >= 400:
                    text = await resp.aread()
                    yield ExecChunk(
                        kind="result",
                        result=ExecResult(
                            exit_code=-1,
                            stdout="",
                            stderr=(
                                f"sandbox_service: exec/stream returned "
                                f"{resp.status_code}: "
                                f"{text.decode('utf-8', 'replace')[:500]}"
                            ),
                            timed_out=False,
                        ),
                    )
                    return

                cur_event: str | None = None
                async for line in resp.aiter_lines():
                    if not line:
                        cur_event = None
                        continue
                    if line.startswith("event:"):
                        cur_event = line.split(":", 1)[1].strip()
                        continue
                    if line.startswith("data:") and cur_event:
                        payload_raw = line.split(":", 1)[1].strip()
                        try:
                            payload = json.loads(payload_raw)
                        except json.JSONDecodeError:
                            continue
                        if cur_event in ("stdout", "stderr"):
                            chunk_b64 = payload.get("chunk_b64") or ""
                            if not chunk_b64:
                                continue
                            try:
                                text = base64.b64decode(chunk_b64).decode(
                                    "utf-8", errors="replace"
                                )
                            except Exception:  # noqa: BLE001 — bad b64
                                continue
                            (agg_stdout if cur_event == "stdout" else agg_stderr).append(text)
                            yield ExecChunk(kind=cur_event, data=text)
                        elif cur_event == "result":
                            stdout = payload.get("stdout") or "".join(agg_stdout)
                            stderr = (payload.get("stderr") or "".join(agg_stderr))
                            stderr += self._truncation_note(payload)
                            resume_ms = int(payload.get("resume_latency_ms") or 0)
                            if resume_ms >= 250:
                                log.info(
                                    "sandbox_service: exec/stream on %s "
                                    "included %d ms of session resume",
                                    sid,
                                    resume_ms,
                                )
                            yield ExecChunk(
                                kind="result",
                                result=ExecResult(
                                    exit_code=int(payload.get("exit_code", -1)),
                                    stdout=stdout,
                                    stderr=stderr,
                                    timed_out=False,
                                ),
                            )
                            return
        except httpx.HTTPError as e:
            yield ExecChunk(
                kind="result",
                result=ExecResult(
                    exit_code=-1,
                    stdout="".join(agg_stdout),
                    stderr=(
                        "".join(agg_stderr)
                        + f"\nsandbox_service: exec/stream transport error: {e}"
                    ),
                    timed_out=False,
                ),
            )
            return

        # If we fall through without seeing a `result` event, synthesize
        # one from the aggregated streams. Robustness against a malformed
        # upstream stream that closes without the terminator.
        yield ExecChunk(
            kind="result",
            result=ExecResult(
                exit_code=-1,
                stdout="".join(agg_stdout),
                stderr=(
                    "".join(agg_stderr)
                    + "\nsandbox_service: stream ended without result event"
                ),
                timed_out=False,
            ),
        )

    async def read_text(self, path: str, *, fs_read: FsReadConfig) -> str:
        sid = await self._ensure_session()
        rel = self._to_container_path(path)
        if not rel:
            raise SandboxViolation(
                f"sandbox_service: read_text refuses to read the workspace "
                f"root itself ({path!r})"
            )
        url = f"/v1/sessions/{sid}/files/{quote(rel, safe='/')}"
        async with self._client_ctx() as client:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                raise RuntimeError(
                    f"sandbox_service: read_text transport error for "
                    f"{path!r}: {e}"
                ) from e
            if resp.status_code == 404:
                raise FileNotFoundError(path)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"sandbox_service: read_text returned {resp.status_code} "
                    f"for {path!r}: {resp.text}"
                )
            # The service's REST file_read returns the bytes directly
            # (`Content-Type: application/octet-stream`). Decode as UTF-8;
            # binary reads aren't part of the SandboxBackend contract.
            return resp.content.decode("utf-8", errors="replace")

    async def write_text(
        self, path: str, content: str, *, fs_write: FsWriteConfig
    ) -> None:
        sid = await self._ensure_session()
        rel = self._to_container_path(path)
        if not rel:
            raise SandboxViolation(
                f"sandbox_service: write_text refuses to write to the "
                f"workspace root itself ({path!r})"
            )
        url = f"/v1/sessions/{sid}/files/{quote(rel, safe='/')}"
        async with self._client_ctx() as client:
            try:
                resp = await client.post(
                    url,
                    content=content.encode("utf-8"),
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Idempotency-Key": self._idem_key(),
                    },
                )
            except httpx.HTTPError as e:
                raise RuntimeError(
                    f"sandbox_service: write_text transport error for "
                    f"{path!r}: {e}"
                ) from e
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"sandbox_service: write_text returned {resp.status_code} "
                    f"for {path!r}: {resp.text}"
                )

    async def close(self) -> None:
        # POST stop, not destroy: preserves the volume so the next session
        # can resume. The service's hard_destroy_ttl_s reaper handles
        # eventual cleanup; operators run scripts/sandbox_destroy.py for
        # immediate teardown (user offboarding, etc.).
        if self._service_session_id is None:
            return
        sid = self._service_session_id
        try:
            async with self._client_ctx() as client:
                await client.post(
                    f"/v1/sessions/{sid}/stop",
                    headers={"Idempotency-Key": self._idem_key()},
                )
        except Exception as e:  # noqa: BLE001 — best-effort
            log.warning(
                "sandbox_service: stop %s failed (best-effort): %s", sid, e
            )


def make_sandbox_service_backend_from_env(
    *,
    session_id: str,
    tenant_id: str,
    credentials: Optional["CredentialProvider"] = None,
) -> SandboxServiceBackend:
    """Construct from `ADK_CC_SANDBOX_SERVICE_*` env vars.

    Required:
      - ADK_CC_SANDBOX_SERVICE_URL
      - ONE OF:
        - ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN — single-tenant escape hatch
          that bypasses the credential provider entirely.
        - `credentials` parameter set — production multi-tenant: token
          resolved per `(tenant_id, key)` from the credential provider.
          Key defaults to `sandbox_service_token`; override via
          ADK_CC_SANDBOX_SERVICE_TOKEN_KEY.

    Optional Limits overrides land in the `POST /v1/sessions` body and
    are subject to the upstream tenant-max policy:
      - ADK_CC_SANDBOX_SERVICE_VCPU
      - ADK_CC_SANDBOX_SERVICE_MEMORY_GIB
      - ADK_CC_SANDBOX_SERVICE_WORKSPACE_GIB
      - ADK_CC_SANDBOX_SERVICE_EXEC_TIMEOUT_S
      - ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S
    """
    base_url = os.environ.get("ADK_CC_SANDBOX_SERVICE_URL")
    if not base_url:
        raise RuntimeError(
            "ADK_CC_SANDBOX_BACKEND=sandbox_service requires "
            "ADK_CC_SANDBOX_SERVICE_URL to be set"
        )
    static_token = os.environ.get("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN")
    credential_key = os.environ.get(
        "ADK_CC_SANDBOX_SERVICE_TOKEN_KEY", "sandbox_service_token"
    )
    if not static_token and credentials is None:
        raise RuntimeError(
            "ADK_CC_SANDBOX_BACKEND=sandbox_service requires either "
            "ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN (dev / single-tenant) "
            "or a CredentialProvider passed to the factory (production)."
        )
    verify_tls = env_bool("ADK_CC_SANDBOX_SERVICE_VERIFY_TLS", True)

    limits: dict[str, Any] = {}
    for env_key, limit_key, cast in (
        ("ADK_CC_SANDBOX_SERVICE_VCPU", "vcpu", int),
        ("ADK_CC_SANDBOX_SERVICE_MEMORY_GIB", "memory_gib", int),
        ("ADK_CC_SANDBOX_SERVICE_WORKSPACE_GIB", "workspace_gib", int),
        ("ADK_CC_SANDBOX_SERVICE_EXEC_TIMEOUT_S", "exec_timeout_s", int),
        ("ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S", "hard_destroy_ttl_s", int),
    ):
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            limits[limit_key] = cast(raw)
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f"{env_key}={raw!r} is not a valid {cast.__name__}: {e}"
            ) from e

    return SandboxServiceBackend(
        base_url=base_url,
        api_token=static_token,
        credentials=credentials if not static_token else None,
        credential_key=credential_key,
        session_id=session_id,
        tenant_id=tenant_id,
        verify_tls=verify_tls,
        limits=limits or None,
    )
