"""Local container sandbox backend (Docker / Podman), for desktop.

Only the SHELL is containerized. The project is bind-mounted **in-place at its
identical host path**, so paths are transparent inside and out (`pwd` prints the
real host path, no `/workspace` remap) and the inherited host-direct file I/O
(`read_text`/`write_text` from NoopBackend) operates on the very same bytes. That
is correct for the "host-only isolation, in-place edits" model: a shell command
can't escape to the rest of the host, the network (when locked), or blow past the
resource limits — but the file tools stay host-direct and permission-gated, exactly
as today.

Driven through the `docker`/`podman` CLI (see container_runtime.py). One container
per session (`adk-cc-<session>`, `sleep infinity`), created lazily on first exec
and removed on `close()`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..config import ExecResult, FsWriteConfig, NetworkConfig
from .container_runtime import Runtime, detect_runtime
from .noop_backend import NoopBackend

if TYPE_CHECKING:
    from ..workspace import WorkspaceRoot

_DEFAULT_IMAGE = "python:3.12-slim"
# HOME must be writable for an arbitrary uid (pip/npm caches, `bash -l`); /tmp is
# a tmpfs mount so it always is. Ephemeral — gone when the container is removed.
_CONTAINER_HOME = "/tmp/adk-home"


def _safe_name(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in (session_id or "local"))
    return f"adk-cc-{safe or 'local'}"


class LocalContainerBackend(NoopBackend):
    """Host-only isolation for `run_bash`; in-place file I/O inherited from Noop."""

    name = "container"

    def __init__(
        self,
        *,
        session_id: str = "local",
        tenant_id: str = "local",
        runtime: Optional[Runtime] = None,
        image: Optional[str] = None,
        network_enabled: bool = True,
        workspace_abs_path: Optional[str] = None,
    ) -> None:
        self._session_id = session_id
        self._tenant_id = tenant_id
        # Runtime resolved lazily off-loop on first use (detection shells out).
        self._runtime: Optional[Runtime] = runtime
        self._image = image or os.environ.get("ADK_CC_SANDBOX_IMAGE", _DEFAULT_IMAGE)
        self._network_enabled = network_enabled
        self._name = _safe_name(session_id)
        self._workspace_abs: Optional[str] = (
            os.path.realpath(workspace_abs_path) if workspace_abs_path else None
        )
        self._mounts: list[str] = []  # host paths bind-mounted at identical paths
        self._started = False
        self._lock = asyncio.Lock()

    # --- config helpers --------------------------------------------------

    def _cli(self) -> Runtime:
        if self._runtime is None:
            rt = detect_runtime()
            if rt is None:
                raise RuntimeError("no local container runtime (docker/podman) available")
            self._runtime = rt
        return self._runtime

    def _limit_args(self) -> list[str]:
        mem = os.environ.get("ADK_CC_SANDBOX_MEM_LIMIT", "4g")
        cpus = os.environ.get("ADK_CC_SANDBOX_CPUS", "2")
        pids = os.environ.get("ADK_CC_SANDBOX_PIDS_LIMIT", "512")
        args = ["--security-opt", "no-new-privileges", "--cap-drop", "ALL"]
        if mem:
            args += ["--memory", mem]
        if cpus:
            args += ["--cpus", cpus]
        if pids:
            args += ["--pids-limit", pids]
        return args

    def _ownership_args(self, rt: Runtime) -> list[str]:
        # Podman rootless maps the host uid cleanly with keep-id; Docker needs an
        # explicit --user so bind-mount writes aren't root-owned on Linux (on
        # macOS Docker Desktop maps ownership via the VM regardless, but --user
        # is harmless there).
        if rt.name == "podman":
            return ["--userns=keep-id"]
        try:
            return ["--user", f"{os.getuid()}:{os.getgid()}"]
        except AttributeError:  # Windows — no getuid; Desktop handles ownership
            return []

    def _mount_args(self) -> list[str]:
        out: list[str] = []
        for host in self._mounts:
            out += ["-v", f"{host}:{host}:rw"]
        return out

    # --- lifecycle -------------------------------------------------------

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        # In-place: the project dir already exists (mkdir is a harmless no-op if
        # so). NO chown, NO helper container. Record the mount set = the primary
        # root + any desktop granted roots folded into ws.extra_roots.
        await asyncio.to_thread(Path(ws.abs_path).mkdir, parents=True, exist_ok=True)
        self._workspace_abs = os.path.realpath(ws.abs_path)
        mounts = [self._workspace_abs]
        for r in ws.extra_roots:
            rp = os.path.realpath(r)
            if rp not in mounts:
                mounts.append(rp)
        self._mounts = mounts

    def _create_container_sync(self) -> None:
        rt = self._cli()
        if self._workspace_abs is None:
            raise RuntimeError("LocalContainerBackend: ensure_workspace() not called")
        # Remove any stale container from a previous boot with the same session.
        subprocess.run([rt.cli_path, "rm", "-f", self._name],
                       capture_output=True, text=True, timeout=30)
        args = [
            rt.cli_path, "run", "-d", "--name", self._name,
            "--workdir", self._workspace_abs,
            "--network", "bridge" if self._network_enabled else "none",
            "--tmpfs", "/tmp:size=1g,mode=1777",
            "-e", f"HOME={_CONTAINER_HOME}",
            "--label", f"adk-cc-session={self._session_id}",
            "--label", f"adk-cc-tenant={self._tenant_id}",
        ]
        args += self._ownership_args(rt) + self._limit_args() + self._mount_args()
        args += [self._image, "sh", "-c", f"mkdir -p {_CONTAINER_HOME}; exec sleep infinity"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(
                f"container create failed ({rt.name}): {proc.stderr.strip() or proc.stdout.strip()}"
            )
        self._started = True

    async def _ensure_container(self) -> None:
        if self._started:
            return
        async with self._lock:
            if self._started:
                return
            await asyncio.to_thread(self._create_container_sync)

    # --- exec ------------------------------------------------------------

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        await self._ensure_container()
        rt = self._cli()
        # Resolve the session's secrets/env fresh (TTL-cached), and forward them
        # into the container BY NAME (`-e KEY`, no value) so the VALUES ride the
        # CLI subprocess environment, never argv (`ps`/history) or container config.
        runtime_env = await self._runtime_env()
        env_flags: list[str] = []
        for k in runtime_env:
            env_flags += ["-e", k]
        subproc_env = {**os.environ, **runtime_env}

        # Normalize to the realpath so it matches the (realpath'd) bind-mount
        # target — otherwise a symlinked cwd (macOS /var → /private/var) fails to
        # chdir inside the container.
        workdir = (os.path.realpath(cwd) if cwd else None) or self._workspace_abs or "/"
        # `timeout` runs INSIDE the container so the deadline actually kills the
        # in-container process (killing only the `<rt> exec` subprocess orphans it).
        inner = ["timeout", "--signal=KILL", str(int(timeout_s)), "bash", "-lc", cmd]
        args = [rt.cli_path, "exec", "-w", workdir, *env_flags, self._name, *inner]

        def _run() -> ExecResult:
            try:
                proc = subprocess.run(
                    args, capture_output=True, text=True, env=subproc_env,
                    # backstop: allow the in-container `timeout` to fire first,
                    # then kill the exec wrapper if it somehow hangs.
                    timeout=timeout_s + 15,
                )
            except subprocess.TimeoutExpired:
                return ExecResult(exit_code=-1, stdout="", stderr=f"timed out after {timeout_s}s",
                                  timed_out=True)
            except OSError as e:
                return ExecResult(exit_code=-1, stdout="", stderr=f"{type(e).__name__}: {e}")
            timed = proc.returncode == 137  # 128 + SIGKILL from `timeout --signal=KILL`
            return ExecResult(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=proc.stdout or "",
                stderr=(proc.stderr or "") if not timed else f"timed out after {timeout_s}s",
                timed_out=timed,
            )

        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        if not self._started or self._runtime is None:
            return
        self._started = False
        rt = self._runtime

        def _rm() -> None:
            try:
                subprocess.run([rt.cli_path, "rm", "-f", self._name],
                               capture_output=True, text=True, timeout=30)
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass

        await asyncio.to_thread(_rm)


def sweep_orphans(runtime: Optional[Runtime] = None) -> int:
    """Remove leftover adk-cc-* containers from crashed sessions. Best-effort;
    returns how many were removed. Safe to call at startup."""
    rt = runtime or detect_runtime()
    if rt is None:
        return 0
    try:
        ls = subprocess.run(
            [rt.cli_path, "ps", "-aq", "--filter", "label=adk-cc-session"],
            capture_output=True, text=True, timeout=30,
        )
        ids = [x for x in (ls.stdout or "").split() if x]
        if not ids:
            return 0
        subprocess.run([rt.cli_path, "rm", "-f", *ids], capture_output=True, text=True, timeout=60)
        return len(ids)
    except Exception:  # noqa: BLE001
        return 0
