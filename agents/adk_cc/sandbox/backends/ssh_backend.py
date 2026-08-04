"""SSH sandbox backend — the workspace lives on a REMOTE device.

Implements the `SandboxBackend` contract over `SshTransport`: exec and file
IO run on the remote host (key/agent auth, one multiplexed connection per
host), while tools stay completely unaware. Identical-path model, like the
desktop in-place mode but on another machine: `ws.abs_path` IS the remote
project root, so `container_cwd` is the identity and no path translation
exists (contrast DaytonaBackend's `_to_sandbox_path`).

Trust model (v1, stated plainly): remote exec is NOT containerized — commands
run as the configured remote account, same trust level as NoopBackend on the
local host, relocated. Path policy (`fs_read`/`fs_write` allow_paths) is
enforced client-side before any round trip — a fail-fast contract mirror,
not a security boundary. Isolation on the remote is future work (e.g. a
container runtime on the remote via this same transport).

Error mapping:
  - `exec` transport failures → ExecResult(-1, stderr="ssh transport error…")
    (never raises; same convention as DaytonaBackend's exec).
  - File-op / ensure_workspace transport failures → `SandboxCapacityError`
    (transient, retryable) so the tool layer surfaces a structured error and
    the tenancy plugin's next-tool-call retry stays honest.
  - Env/secrets ride the transport's stdin script — never on argv.

`close()` is a deliberate no-op: the ControlMaster is SHARED per host across
sessions (and, later, the desktop file panel); `ControlPersist` reaps idle
masters. Killing it on one session's end would sever the others.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, AsyncIterator, Optional

from ..config import (
    ExecChunk,
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxCapacityError,
    SandboxViolation,
)
from ..ssh_transport import SshConnectionError, SshTransport, get_transport
from .base import SandboxBackend

if TYPE_CHECKING:
    from ..workspace import WorkspaceRoot

log = logging.getLogger(__name__)


class SshBackend(SandboxBackend):
    name = "ssh"

    def __init__(
        self,
        *,
        session_id: str,
        tenant_id: str,
        transport: SshTransport,
        workspace_path: Optional[str] = None,
    ) -> None:
        self._session_id = session_id
        self._tenant_id = tenant_id
        self._t = transport
        # Remote workspace root; (re)captured by ensure_workspace from the
        # WorkspaceRoot so the ctor arg is optional for factory use.
        self._workspace_path = workspace_path.rstrip("/") if workspace_path else None
        # Remote $HOME, captured from the ensure_workspace probe. Consumed by
        # the permission floor to guard the REMOTE machine's ~/.ssh etc.
        self._remote_home: Optional[str] = None

    # --- helpers ----------------------------------------------------------

    @property
    def host(self) -> str:
        return self._t.host

    @property
    def remote_home(self) -> Optional[str]:
        """The probed remote $HOME (None before ensure_workspace ran)."""
        return self._remote_home

    @property
    def transport(self) -> SshTransport:
        """The shared per-host transport — consumed by the desktop services
        (file panel, remote checkpoint) so ALL traffic to this remote rides
        one ControlMaster."""
        return self._t

    def _check_allowed(
        self, path: str, fs_cfg: FsReadConfig | FsWriteConfig, *, op: str
    ) -> None:
        """Fail fast (client-side) when `path` escapes the workspace's
        allow_paths — same contract mirror as DaytonaBackend."""
        if not fs_cfg.allows(path):
            raise SandboxViolation(
                f"ssh: path {path!r} is not in the workspace's allowed "
                f"paths during {op}"
            )

    # --- exec -------------------------------------------------------------

    # Remote background processes (#108). The mechanism generalises to any
    # backend that can exec: capture the process GROUP id at launch, then
    # terminate later with an ordinary `kill -TERM -<pgid>` over a second
    # channel. That also fixes a documented v1 limitation — ssh timeouts are
    # enforced CLIENT-side today ("the remote command may keep running"), so
    # before this there was no way to stop remote work at all.
    supports_background = True

    async def start_background(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        cwd: str,
        label: str = "",
        session_key: str = "",
        project_id: str = "",
    ) -> dict:
        import shlex as _sh

        from ..process_registry import get_registry

        if cwd and not fs_write.allows(cwd):
            raise SandboxViolation(
                f"ssh: cwd {cwd!r} is outside the workspace's allowed paths")

        reg = get_registry()
        rec = reg.create(session_key=session_key, project_id=project_id,
                         label=label, command=cmd, cwd=cwd, backend="ssh",
                         can_terminate=True)
        # The log lives on the REMOTE (that is where the process writes), and
        # is read back on demand — copying it continuously would put a second
        # long-lived channel per process on the ControlMaster.
        remote_log = f".adk-cc/processes/{rec.id}.log"
        launch = (
            f"mkdir -p .adk-cc/processes && "
            f"setsid nohup sh -c {_sh.quote(cmd)} "
            f"> {_sh.quote(remote_log)} 2>&1 < /dev/null & "
            # setsid makes the child a group leader, so pgid == pid and the
            # kill below reaches everything it forks.
            f"echo $!"
        )
        try:
            res = await self._t.run(launch, env=await self._runtime_env(),
                                    cwd=cwd, timeout_s=30)
        except SshConnectionError as e:
            reg.mark_exited(rec.id, exit_code=None, status="failed")
            reg.append_log(rec.id, f"ssh transport error: {e}\n".encode())
            return (reg.get(rec.id) or rec).public()

        pid = 0
        for line in (res.stdout or "").splitlines():
            if line.strip().isdigit():
                pid = int(line.strip())
        if not pid or res.exit_code != 0:
            reg.mark_exited(rec.id, exit_code=res.exit_code, status="failed")
            reg.append_log(
                rec.id, (res.stderr or res.stdout or "failed to start").encode())
            return (reg.get(rec.id) or rec).public()

        reg.mark_started(rec.id, pid=pid, pgid=pid)
        rec2 = reg.get(rec.id)
        if rec2 is not None:
            rec2.remote_log_path = remote_log
            reg.save()
        # Give it a moment, then pull the first output so the caller can
        # already report "listening on :5173" or the reason it died.
        import asyncio as _asyncio

        await _asyncio.sleep(0.8)
        await self.sync_background_log(rec.id)
        return (reg.get(rec.id) or rec).public()

    async def sync_background_log(self, process_id: str) -> None:
        """Copy the remote log tail into the local record (read-on-demand)."""
        from ..process_registry import get_registry

        reg = get_registry()
        rec = reg.get(process_id)
        remote_log = getattr(rec, "remote_log_path", "") if rec else ""
        if not rec or not remote_log:
            return
        import shlex as _sh

        try:
            res = await self._t.run(f"tail -c 200000 {_sh.quote(remote_log)}",
                                    cwd=rec.cwd, timeout_s=20)
        except SshConnectionError:
            return
        if res.exit_code == 0 and res.stdout:
            reg.replace_log(process_id, res.stdout.encode("utf-8", "replace"))
        # Liveness: the local pgid check is meaningless for a remote pid.
        try:
            alive = await self._t.run(f"kill -0 -{rec.pgid} 2>/dev/null && echo LIVE",
                                      cwd=rec.cwd, timeout_s=15)
            if "LIVE" not in (alive.stdout or "") and rec.status == "running":
                reg.mark_exited(process_id, exit_code=None, status="exited")
        except SshConnectionError:
            pass

    async def terminate_background(self, process_id: str) -> bool:
        """`kill` the remote process GROUP — TERM, grace, then KILL."""
        from ..process_registry import get_registry

        reg = get_registry()
        rec = reg.get(process_id)
        if not rec or not rec.pgid:
            return False
        try:
            await self._t.run(
                f"kill -TERM -{rec.pgid} 2>/dev/null; sleep 3; "
                f"kill -KILL -{rec.pgid} 2>/dev/null; true",
                cwd=rec.cwd, timeout_s=30)
        except SshConnectionError:
            return False
        reg.mark_exited(process_id, exit_code=None, status="killed")
        return True

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,  # remote egress is the remote account's own — no per-exec knob in v1
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        if cwd and not fs_write.allows(cwd):
            raise SandboxViolation(
                f"ssh: cwd {cwd!r} is outside the workspace's allowed paths"
            )
        env = await self._runtime_env()
        try:
            return await self._t.run(cmd, env=env, cwd=cwd, timeout_s=timeout_s)
        except SshConnectionError as e:
            # Same convention as the other remote backends: exec never
            # raises for transport trouble — the model sees a failed
            # command and can retry.
            return ExecResult(
                exit_code=-1, stdout="", stderr=f"ssh transport error: {e}"
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
        """True live streaming from the remote command's pipes."""
        if cwd and not fs_write.allows(cwd):
            raise SandboxViolation(
                f"ssh: cwd {cwd!r} is outside the workspace's allowed paths"
            )
        env = await self._runtime_env()
        try:
            async for chunk in self._t.run_stream(
                cmd, env=env, cwd=cwd, timeout_s=timeout_s
            ):
                yield chunk
        except SshConnectionError as e:
            yield ExecChunk(
                kind="result",
                result=ExecResult(
                    exit_code=-1, stdout="", stderr=f"ssh transport error: {e}"
                ),
            )

    # --- file IO ----------------------------------------------------------

    async def read_text(self, path: str, *, fs_read: FsReadConfig) -> str:
        return (await self.read_bytes(path, fs_read=fs_read)).decode("utf-8")

    async def read_bytes(self, path: str, *, fs_read: FsReadConfig) -> bytes:
        self._check_allowed(path, fs_read, op="read")
        try:
            return await self._t.read_file(path)
        except SshConnectionError as e:
            raise SandboxCapacityError(f"ssh read failed (transient): {e}") from e

    async def write_text(
        self, path: str, content: str, *, fs_write: FsWriteConfig
    ) -> None:
        await self.write_bytes(path, content.encode("utf-8"), fs_write=fs_write)

    async def write_bytes(
        self, path: str, content: bytes, *, fs_write: FsWriteConfig
    ) -> None:
        self._check_allowed(path, fs_write, op="write")
        try:
            await self._t.write_file(path, content)
        except SshConnectionError as e:
            raise SandboxCapacityError(f"ssh write failed (transient): {e}") from e

    # --- lifecycle --------------------------------------------------------

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        """Probe the connection and `mkdir -p` the remote workspace root.

        Raises SandboxCapacityError (retryable) when the host is
        unreachable — the tenancy plugin logs and retries on the next tool
        call, matching the other remote backends' bring-up contract."""
        self._workspace_path = ws.abs_path.rstrip("/") if ws.abs_path else None
        if not self._workspace_path:
            raise SandboxViolation("ssh: workspace has no abs_path")
        try:
            probe = await self._t.probe()
            self._remote_home = probe.get("home") or None
            res = await self._t.run(
                f"mkdir -p {_q(self._workspace_path)}", timeout_s=30
            )
        except SshConnectionError as e:
            raise SandboxCapacityError(f"ssh workspace bring-up failed: {e}") from e
        if res.exit_code != 0:
            raise SandboxViolation(
                f"ssh: could not create workspace {self._workspace_path!r} "
                f"on {self.host!r} (exit {res.exit_code}): {res.stderr[:200]}"
            )
        log.info(
            "ssh: workspace %s ready on %s (session=%s tenant=%s uname=%s)",
            self._workspace_path,
            self.host,
            self._session_id,
            self._tenant_id,
            probe.get("uname", "?"),
        )

    async def close(self) -> None:
        """No-op ON PURPOSE — the per-host ControlMaster is shared across
        sessions and (later) the desktop panel; `ControlPersist` reaps it
        when idle. See module docstring."""
        return None


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)


def make_ssh_backend_from_env(
    *, session_id: str, tenant_id: str
) -> SshBackend:
    """Construct from `ADK_CC_SSH_*` env vars (single-workspace / dev path;
    the desktop per-project flow constructs directly in a later phase).

      - ADK_CC_SSH_HOST            — required; anything your `ssh` accepts:
                                     `host`, `user@host`, or a config alias.
      - ADK_CC_SSH_WORKSPACE_PATH  — required (consumed by
                                     `default_workspace()`); absolute remote
                                     path of the workspace root.
      - ADK_CC_SSH_PORT            — optional.
      - ADK_CC_SSH_IDENTITY_FILE   — optional key path (else ssh config/agent).
      - ADK_CC_SSH_EXTRA_OPTS      — optional extra `ssh` args, shlex-split
                                     (tests use this for throwaway known_hosts;
                                     production normally leaves it unset).
    """
    host = os.environ.get("ADK_CC_SSH_HOST")
    if not host:
        raise RuntimeError("ADK_CC_SANDBOX_BACKEND=ssh requires ADK_CC_SSH_HOST")
    port_raw = os.environ.get("ADK_CC_SSH_PORT")
    try:
        port = int(port_raw) if port_raw else None
    except ValueError as e:
        raise RuntimeError(f"ADK_CC_SSH_PORT={port_raw!r} is not an int") from e
    identity = os.environ.get("ADK_CC_SSH_IDENTITY_FILE") or None
    extra_raw = os.environ.get("ADK_CC_SSH_EXTRA_OPTS") or ""
    import shlex

    extra = tuple(shlex.split(extra_raw)) if extra_raw else ()
    transport = get_transport(
        host, port=port, identity_file=identity, extra_ssh_opts=extra
    )
    return SshBackend(
        session_id=session_id, tenant_id=tenant_id, transport=transport
    )
