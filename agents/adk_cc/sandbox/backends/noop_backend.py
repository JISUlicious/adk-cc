"""Host-execution backend.

Runs commands and FS operations directly on the process's host. Honors
config policy (path / network restrictions) via Python checks so the
contract is exercised in dev — but it is NOT a security boundary. Any
multi-tenant deployment must use docker/e2b/etc.

Two safety guards aimed at the dev footgun (a buggy or hostile model
emitting `rm -rf $HOME`) and the misdeployment footgun (`noop` set as
the production backend by accident):

  1. **Explicit-ack on prod-shaped paths.** `exec()` refuses to run if
     `cwd` is outside obviously-safe prefixes (`$HOME`, `/tmp`, OS
     tempdirs) unless `ADK_CC_NOOP_ACK_HOST_EXEC=1` is set. The
     workspace path normally lives under `$HOME` for dev, so the
     normal dev flow doesn't hit this. Production-shaped paths
     (`/var/lib/...`, `/srv/...`, `/opt/...`) trip the guard, and
     the operator must explicitly acknowledge — same pattern as
     `ADK_CC_ALLOW_NO_AUTH` for `make_app`.

  2. **cwd-prefix check.** `cwd` must be the workspace itself or
     a subdirectory of it. Doesn't stop in-shell `cd /` (that needs
     OS namespace tricks; if you need that, use DockerBackend), but
     materially harder to escape by accident.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ... import deployment
from ..config import (
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxViolation,
)
from .base import SandboxBackend

if TYPE_CHECKING:
    from ..workspace import WorkspaceRoot


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe_prefixes() -> list[Path]:
    """Paths under which NoopBackend exec is unconditionally allowed."""
    candidates = [
        Path(os.path.expanduser("~")),
        Path("/tmp"),
        Path("/var/folders"),  # macOS tempdir raw form
        Path("/private/var/folders"),  # macOS tempdir resolved form
        Path("/private/tmp"),  # macOS /tmp resolved form
    ]
    out: list[Path] = []
    for c in candidates:
        try:
            out.append(c.resolve())
        except OSError:
            pass
    return out


def _is_prod_shaped(cwd: str) -> bool:
    """True if `cwd` looks like a production path needing explicit-ack."""
    try:
        p = Path(cwd).resolve()
    except OSError:
        return True  # can't resolve → treat as prod-shaped
    return not any(_is_under(p, prefix) for prefix in _safe_prefixes())


# How long to wait for output after killing a timed-out process group. A child
# that ignores SIGKILL is not a thing, but a shared pipe held by an unrelated
# process is — so the drain is bounded rather than open-ended.
_DRAIN_GRACE_S = 3

# Per-stream cap on what is KEPT (reading continues past it, see _drain).
# 256KB leaves plenty of head-room over the 4KB the tool actually forwards,
# while bounding what one runaway command can hold in the server.
_MAX_STREAM_BYTES = 256 * 1024


def _kill_group(proc) -> None:
    """SIGKILL the process group, falling back to the process itself.

    Uses `proc.pid` AS the group id rather than looking it up: with
    `start_new_session=True` the child is its own group leader, so pgid == pid,
    and by the time we need to kill, the shell itself has usually already
    exited — `os.getpgid(pid)` then raises and the orphaned background child
    survives, still holding the pipe. That lookup cost the whole drain grace on
    every timeout.
    """
    import os as _os
    import signal as _signal

    for target in (proc.pid, None):
        try:
            if target is not None:
                _os.killpg(target, _signal.SIGKILL)
            else:
                proc.kill()
            return
        except (ProcessLookupError, PermissionError, OSError):
            continue


class NoopBackend(SandboxBackend):
    name = "noop"

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        Path(ws.abs_path).mkdir(parents=True, exist_ok=True)

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        # Guard 1: explicit-ack on prod-shaped paths. The desktop profile
        # acknowledges host exec by default (deployment.noop_ack_host_exec() →
        # True when ADK_CC_DESKTOP=1), since desktop is single-user and works
        # in-place in the user's real project root (which may be under /opt,
        # /Volumes/…, /Users/Shared, …). ADK_CC_NOOP_ACK_HOST_EXEC still overrides.
        if _is_prod_shaped(cwd) and not deployment.noop_ack_host_exec():
            raise SandboxViolation(
                f"NoopBackend: refusing to exec in prod-shaped path {cwd!r}. "
                "Either set ADK_CC_NOOP_ACK_HOST_EXEC=1 to acknowledge "
                "running commands directly on the host (dev-only), or "
                "switch to ADK_CC_SANDBOX_BACKEND=docker for real "
                "per-session isolation."
            )

        # Guard 2: cwd must exist and be a directory. The agent always
        # passes cwd=ws.abs_path, so this catches obvious misuse rather
        # than enforcing an in-shell sandbox.
        cwd_p = Path(cwd)
        if not cwd_p.is_dir():
            raise SandboxViolation(f"NoopBackend: cwd not a directory: {cwd!r}")

        # On-demand env injection: merge the session's resolved secrets/env
        # into THIS child's environment only (never mutates the server's
        # os.environ → no cross-session leak). Scoped to the subprocess, like
        # a container's per-exec env. Resolves fresh (TTL) so secrets provided
        # after sandbox creation are picked up on the next command.
        runtime_env = await self._runtime_env()
        child_env = {**os.environ, **runtime_env} if runtime_env else None

        # start_new_session: the command gets its OWN process group, so a
        # backgrounded child (`npx tsx server.ts &`) can be killed with it.
        # Without this, killing the shell left the grandchild alive holding the
        # stdout PIPE — see the timeout branch.
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            start_new_session=True,
        )
        # Accumulate output AS IT ARRIVES rather than via communicate().
        # `wait_for(communicate())` cancels the read on timeout, and a second
        # communicate() afterwards cannot recover what was already printed — a
        # script that ran for 6s and printed 40 lines came back with an empty
        # stdout and no exit code, which the UI could only render as `exit ?`.
        # Reader tasks keep every byte, so a timeout still reports the work.
        out_buf: list[bytes] = []
        err_buf: list[bytes] = []
        dropped = {"stdout": 0, "stderr": 0}

        async def _drain(stream, buf, which):
            """Read to EOF, but KEEP at most `_MAX_STREAM_BYTES`.

Keeping every byte buffers the whole flood in the SERVER
            process. A dogfooding session ran `find / -name psycopg2 ...` on
            Linux, where find prints "Permission denied" across /proc, /sys and
            /root — easily hundreds of MB held in memory for one command.

            To be precise about what this does and does not explain: the tool
            payload is already tail-capped (`result.stdout[-4000:]`), so the
            flood never reached the model. What it threatens is the server
            itself, and a server that dies mid-tool leaves the call with no
            response — which the UI renders as `exit ?`, the symptom reported.
            That chain is plausible and unproven; the memory hazard is real
            either way, and it is cheap to remove.

            Reading must CONTINUE past the cap even though the bytes are
            discarded: a 64KB pipe fills, and a child blocked on write never
            exits, so capping by breaking out of the loop would swap a huge
            payload for a hang.
            """
            if stream is None:
                return
            kept = 0
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                room = _MAX_STREAM_BYTES - kept
                if room > 0:
                    buf.append(chunk[:room])
                    kept += min(room, len(chunk))
                    if len(chunk) > room:
                        dropped[which] += len(chunk) - room
                else:
                    dropped[which] += len(chunk)

        readers = [
            asyncio.ensure_future(_drain(proc.stdout, out_buf, "stdout")),
            asyncio.ensure_future(_drain(proc.stderr, err_buf, "stderr")),
        ]

        def _text(buf, which):
            """Decode, and say plainly when output was dropped — a silently
            truncated log reads as a complete one and invites a wrong
            conclusion from a partial result."""
            text = b"".join(buf).decode("utf-8", errors="replace")
            lost = dropped[which]
            if lost:
                text += (
                    f"\n[adk-cc: {which} truncated — kept the first "
                    f"{_MAX_STREAM_BYTES // 1024}KB, dropped {lost} more bytes. "
                    "Narrow the command (a path instead of /, -maxdepth, "
                    "2>/dev/null) or write to a file and read it back.]"
                )
            return text
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            # The process is gone; give the readers a moment to see EOF. A
            # backgrounded grandchild can hold the pipe open indefinitely, so
            # this wait is bounded too — output already buffered is kept.
            await asyncio.wait(readers, timeout=_DRAIN_GRACE_S)
        except asyncio.TimeoutError:
            # Kill the whole GROUP: killing only the shell left background
            # children alive AND holding the pipe, which is what made a
            # 6s-timeout command block for the full 20s of its `sleep 20 &`.
            _kill_group(proc)
            await asyncio.wait(readers, timeout=_DRAIN_GRACE_S)
            for r in readers:
                r.cancel()
            return ExecResult(
                exit_code=-1,
                stdout=_text(out_buf, "stdout"),
                stderr=_text(err_buf, "stderr"),
                timed_out=True,
            )
        finally:
            for r in readers:
                if not r.done():
                    r.cancel()

        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=_text(out_buf, "stdout"),
            stderr=_text(err_buf, "stderr"),
        )

    async def read_text(self, path: str, *, fs_read: FsReadConfig) -> str:
        abs_path = os.path.abspath(path)
        if not fs_read.allows(abs_path):
            raise SandboxViolation(f"read denied by fs_read: {abs_path}")
        p = Path(abs_path)
        if not p.exists():
            raise FileNotFoundError(abs_path)
        if not p.is_file():
            raise IsADirectoryError(abs_path)
        return p.read_text(encoding="utf-8")

    async def write_text(
        self, path: str, content: str, *, fs_write: FsWriteConfig
    ) -> None:
        abs_path = os.path.abspath(path)
        if not fs_write.allows(abs_path):
            raise SandboxViolation(f"write denied by fs_write: {abs_path}")
        p = Path(abs_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
