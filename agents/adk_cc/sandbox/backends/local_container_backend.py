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
        # Signature (image + network + sorted mounts) of the container currently
        # in use. When ensure_workspace changes the mount set mid-session, the sig
        # changes and _ensure_container recreates the container (see #5).
        self._active_sig: Optional[str] = None
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

    def _mount_signature(self) -> str:
        """Identity of the container config that requires a recreate if changed:
        image + network + the sorted mount set. Stored as a label so a fresh
        backend instance next turn can decide reuse-vs-recreate."""
        import hashlib

        parts = [self._image, "net=" + ("1" if self._network_enabled else "0")] + sorted(self._mounts)
        return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]

    def _ensure_container_sync(self) -> None:
        """Attach to the session's container, reusing it across turns. Creates it
        only when absent; recreates when the mount set changed (#5). This is what
        makes the container per-SESSION even though a fresh backend object is built
        each turn — the container is keyed by its deterministic name."""
        rt = self._cli()
        if self._workspace_abs is None:
            raise RuntimeError("LocalContainerBackend: ensure_workspace() not called")
        desired = self._mount_signature()

        # Re-attach to an existing container with the same name, if its mount
        # signature still matches. (docker inspect exits non-zero when absent.)
        insp = subprocess.run(
            [rt.cli_path, "inspect", self._name, "--format",
             '{{.State.Running}}|{{index .Config.Labels "adk-cc-mounts"}}'],
            capture_output=True, text=True, timeout=30)
        if insp.returncode == 0:
            running, _, sig = insp.stdout.strip().partition("|")
            if sig == desired:
                if running.strip() != "true":
                    subprocess.run([rt.cli_path, "start", self._name],
                                   capture_output=True, text=True, timeout=30)
                self._started, self._active_sig = True, desired
                return
            # mount set / network / image changed → recreate with the new config
            subprocess.run([rt.cli_path, "rm", "-f", self._name],
                           capture_output=True, text=True, timeout=30)

        args = [
            rt.cli_path, "run", "-d", "--name", self._name,
            "--pull=never",  # never block first-exec on a 10-min implicit pull (#7)
            "--workdir", self._workspace_abs,
            "--network", "bridge" if self._network_enabled else "none",
            "--tmpfs", "/tmp:size=1g,mode=1777",
            "-e", f"HOME={_CONTAINER_HOME}",
            "--label", f"adk-cc-session={self._session_id}",
            "--label", f"adk-cc-tenant={self._tenant_id}",
            "--label", f"adk-cc-mounts={desired}",
        ]
        args += self._ownership_args(rt) + self._limit_args() + self._mount_args()
        args += [self._image, "sh", "-c", f"mkdir -p {_CONTAINER_HOME}; exec sleep infinity"]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            if "No such image" in err or "Unable to find image" in err or "manifest unknown" in err:
                raise RuntimeError(
                    f"sandbox image '{self._image}' is not present — pull it in Settings → Sandbox"
                )
            raise RuntimeError(f"container create failed ({rt.name}): {err}")
        self._started, self._active_sig = True, desired

    async def _ensure_container(self) -> None:
        desired = self._mount_signature()
        if self._started and self._active_sig == desired:
            return
        async with self._lock:
            if self._started and self._active_sig == desired:
                return
            await asyncio.to_thread(self._ensure_container_sync)

    # --- exec ------------------------------------------------------------

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,   # enforced by the bind mounts, not per-exec
        network: NetworkConfig,    # container network is all-or-nothing (set at create); the
        timeout_s: int,            # per-exec allow-domains policy isn't honored here by design
        cwd: str,
    ) -> ExecResult:
        # Bring up (or re-attach to) the container. A failure here — missing
        # runtime, absent image, wedged daemon — becomes a clean ExecResult
        # instead of a raw exception out of the run_bash tool (#7).
        try:
            await self._ensure_container()
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            msg = str(e) if isinstance(e, RuntimeError) else f"sandbox unavailable: {type(e).__name__}: {e}"
            return ExecResult(exit_code=-1, stdout="", stderr=msg)
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
        # in-container process. `-k 5` = send TERM at the deadline, then KILL 5s
        # later; GNU timeout then exits 124 on a timeout it initiated (distinct
        # from 137, which means a real external/OOM SIGKILL — see #3).
        n = int(timeout_s)
        inner = ["timeout", "-k", "5", str(n), "bash", "-lc", cmd]
        args = [rt.cli_path, "exec", "-w", workdir, *env_flags, self._name, *inner]

        def _run() -> ExecResult:
            try:
                proc = subprocess.run(
                    args, capture_output=True, text=True, env=subproc_env,
                    # backstop: the in-container `timeout` (+5s kill grace) fires
                    # first; this only trips if the exec wrapper itself wedges.
                    timeout=n + 15,
                )
            except subprocess.TimeoutExpired as e:
                return ExecResult(
                    exit_code=-1, stdout=(e.stdout or b"").decode("utf-8", "replace")
                    if isinstance(e.stdout, bytes) else (e.stdout or ""),
                    stderr=f"timed out after {n}s", timed_out=True)
            except OSError as e:
                return ExecResult(exit_code=-1, stdout="", stderr=f"{type(e).__name__}: {e}")
            # 124 == the in-container `timeout` fired; 137 (OOM / external SIGKILL)
            # is NOT a timeout and its real stderr must be preserved.
            timed = proc.returncode == 124
            stderr = proc.stderr or ""
            if timed:
                stderr = (stderr + f"\n[timed out after {n}s]").lstrip("\n")
            return ExecResult(
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=proc.stdout or "",
                stderr=stderr,
                timed_out=timed,
            )

        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        """Per-TURN no-op. TenancyPlugin.after_run_callback calls close() after
        every message, but the container is per-SESSION: removing it here would
        kill background processes / installs mid-session and force a slow recreate
        each turn. The container is left running (idle `sleep infinity` is ~free),
        reused by name next turn, and reaped by sweep_orphans() at app startup.
        We only drop the local `_started` flag so the next turn's backend re-checks
        (re-attaches) rather than assuming."""
        self._started = False

    async def remove(self) -> None:
        """Explicit teardown — remove this session's container. For a real
        session-end / delete-session hook (not the per-turn close())."""
        if self._runtime is None:
            return
        self._started, self._active_sig = False, None
        rt = self._runtime

        def _rm() -> None:
            try:
                subprocess.run([rt.cli_path, "rm", "-f", self._name],
                               capture_output=True, text=True, timeout=30)
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass

        await asyncio.to_thread(_rm)


class UnavailableSandboxBackend(NoopBackend):
    """Fail-CLOSED stand-in: the user required the container sandbox but no
    runtime is available, so run_bash errors instead of silently running on the
    host. File reads/writes stay host-direct (inherited) so the UI isn't broken."""

    name = "sandbox-unavailable"

    def __init__(self, reason: str = "container sandbox required but no runtime is available") -> None:
        self._reason = reason

    async def exec(self, cmd, *, fs_write, network, timeout_s, cwd) -> ExecResult:  # noqa: ANN001
        return ExecResult(exit_code=-1, stdout="", stderr=self._reason)


def sweep_orphans(runtime: Optional[Runtime] = None) -> int:
    """Remove leftover adk-cc-* containers. Best-effort; returns how many.

    Call ONCE at app startup — before any session of this instance is live — so
    it only reaps containers left by a PREVIOUS (crashed / force-killed) run.
    Desktop is single-instance, so removing every adk-cc-* is safe there. Do NOT
    call it mid-run or in a multi-instance deployment: the filter matches by the
    label's presence, so it would also remove a concurrently-running instance's
    live containers."""
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
