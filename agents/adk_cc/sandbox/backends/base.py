"""Abstract sandbox backend.

Tools call into this contract instead of touching the host directly. The
concrete backend (noop / docker / e2b / ...) decides how the operation
actually runs. The contract:

  - `exec(cmd, ...)` for shell commands
  - `read_text(path, ...)` for FS reads
  - `write_text(path, content, ...)` for FS writes
  - `ensure_workspace(ws)` for session start
  - `close()` for session end

`fs_read` / `fs_write` / `network` configs are passed per call so the
caller (the tool) can scope each operation to the active workspace and
the operator's policy. Backends that genuinely isolate (docker, e2b)
implement these via bind mounts / firewall rules / etc.; the noop
backend honors them via Python checks so the contract is exercised
end-to-end in dev.

`ensure_workspace` and `close` exist because some backends operate on
remote infrastructure where the agent process can't directly create
or clean up the workspace dir (e.g. workspaces live on a separate
sandbox VM). Default no-op implementations are provided so backends
without that need don't have to override.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncIterator

from ..config import ExecChunk, ExecResult, FsReadConfig, FsWriteConfig, NetworkConfig

if TYPE_CHECKING:
    from ..workspace import WorkspaceRoot


class SandboxBackend(ABC):
    """Abstract isolation boundary."""

    name: str = "abstract"

    # ---- on-demand env injection (resolve-at-exec) ------------------------
    # The session's secrets/env are resolved fresh per exec (TTL-cached) and
    # merged into the command environment, so a secret a user provides AFTER
    # the sandbox was created still reaches the next command — no recreate.
    # Backends opt in by merging `await self._runtime_env()` into their exec
    # environment. Resolution is user-over-tenant via the CredentialProvider;
    # values never touch session state or the event record (side-channel only).

    def configure_runtime_env(
        self,
        *,
        credentials=None,
        tenant_id: str = "local",
        user_id: str = "",
        env_spec=None,
        declared_keys=None,
        ttl_s: float = 15.0,
    ) -> None:
        """Wire the per-session secret/env sources. Safe to call once at
        backend construction; all fields optional (no provider → inert).

        `declared_keys` (a set of credential key names declared-required by the
        installed skills) is the least-privilege ALLOWLIST: when non-empty, only
        the user's secrets whose key is in it are injected. When empty/None, ALL
        of the user's secrets are injected (pre-declaration fallback)."""
        self._env_credentials = credentials
        self._env_tenant_id = tenant_id
        self._env_user_id = user_id
        self._env_spec = env_spec
        self._env_declared_keys = declared_keys
        self._env_ttl_s = ttl_s
        self._env_cache = None  # (expiry_monotonic, dict[str,str])

    def invalidate_runtime_env(self) -> None:
        self._env_cache = None

    async def _runtime_env(self) -> dict:
        """Resolve the env to inject for the NEXT command (TTL-cached).

        = the operator `SandboxEnvSpec` (static/passthrough/credentials) PLUS
        the session user's own secrets (each credential key → value),
        user-over-tenant. Empty when nothing is configured. Never raises."""
        import time

        creds = getattr(self, "_env_credentials", None)
        spec = getattr(self, "_env_spec", None)
        if creds is None and (spec is None or spec.is_empty()):
            return {}

        now = time.monotonic()
        cache = getattr(self, "_env_cache", None)
        if cache and cache[0] > now:
            return cache[1]

        tenant_id = getattr(self, "_env_tenant_id", "local")
        user_id = getattr(self, "_env_user_id", "") or None
        env: dict = {}
        try:
            if spec is not None and not spec.is_empty():
                env.update(
                    await spec.resolve(
                        tenant_id=tenant_id, user_id=user_id, credentials=creds
                    )
                )
            if creds is not None:
                names = set(await creds.list_keys(tenant_id=tenant_id))
                if user_id:
                    names |= set(
                        await creds.list_keys(tenant_id=tenant_id, user_id=user_id)
                    )
                # Least-privilege: when skills declare required secrets, inject
                # only those (∩ what the user has set). No declarations → inject
                # all (pre-declaration fallback).
                declared = getattr(self, "_env_declared_keys", None)
                if declared:
                    names &= set(declared)
                for name in names:
                    val = await creds.get(tenant_id=tenant_id, key=name, user_id=user_id)
                    if val:
                        env[name] = val
        except Exception:  # noqa: BLE001 — env resolution must never break exec
            prev = getattr(self, "_env_cache", None)
            return prev[1] if prev else {}

        self._env_cache = (now + getattr(self, "_env_ttl_s", 15.0), env)
        return env

    @abstractmethod
    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        ...

    async def exec_stream(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> AsyncIterator[ExecChunk]:
        """Stream stdout/stderr chunks as they arrive, terminating with
        a `kind="result"` chunk carrying the full `ExecResult`.

        Default impl: call `exec`, yield one final chunk. Backends that
        actually stream (currently `SandboxServiceBackend` via SSE)
        override this to deliver chunks live. Callers can rely on
        eventual termination — every stream ends with exactly one
        `result` chunk.
        """
        result = await self.exec(
            cmd,
            fs_write=fs_write,
            network=network,
            timeout_s=timeout_s,
            cwd=cwd,
        )
        yield ExecChunk(kind="result", result=result)

    @abstractmethod
    async def read_text(self, path: str, *, fs_read: FsReadConfig) -> str:
        ...

    @abstractmethod
    async def write_text(
        self, path: str, content: str, *, fs_write: FsWriteConfig
    ) -> None:
        ...

    async def read_bytes(self, path: str, *, fs_read: FsReadConfig) -> bytes:
        """Read a file as raw bytes (binary-safe).

        Default impl round-trips through `read_text` + utf-8 — works
        for text files but corrupts binaries. Backends with a true
        binary read path (DaytonaBackend's `files/download` already
        returns bytes; SandboxServiceBackend's read endpoint too)
        should override to skip the decode.

        Used by `save_as_artifact` — when an agent publishes a PDF /
        image / zip, the text fallback would mangle it.
        """
        return (await self.read_text(path, fs_read=fs_read)).encode("utf-8")

    async def write_bytes(
        self, path: str, content: bytes, *, fs_write: FsWriteConfig
    ) -> None:
        """Write raw bytes (binary-safe).

        Default impl round-trips through `write_text` after a utf-8
        decode — works only when the bytes are valid utf-8. Backends
        with a true binary write path should override.

        Used by `fetch_from_artifact` to materialize a user-uploaded
        file into the agent's sandbox without lossy re-encoding.
        """
        await self.write_text(
            path, content.decode("utf-8"), fs_write=fs_write
        )

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        """Create the workspace dir if it doesn't exist.

        Default no-op for backends that don't need init (e.g. backends
        where workspace creation happens elsewhere). NoopBackend does a
        local mkdir; DockerBackend creates the dir on the remote VM.
        """
        return None

    def container_cwd(self, host_abs_path: str) -> str:
        """The workspace directory as seen INSIDE this backend's execution
        context — i.e. what `pwd` returns and what absolute paths the model
        forms must fall under.

        For host-exec backends (NoopBackend) this IS the host path, so the
        default returns it unchanged. Sandboxed backends bind/mount the host
        workspace to a fixed in-container root and override this (DockerBackend
        and SandboxServiceBackend → "/workspace", DaytonaBackend → its
        workspace_path). The workspace hint surfaces THIS to the model — not the
        host path — so an absolute path it constructs actually exists where its
        tools run.
        """
        return host_abs_path

    async def close(self) -> None:
        """Tear down per-session state (e.g. stop and remove a container).

        Default no-op. Wired into ADK's session-end via the tenancy plugin.
        Best-effort — should not raise; a failure to clean up shouldn't
        block the next session.
        """
        return None
