"""SSH transport for remote workspaces.

One multiplexed connection per remote host, shared by everything that talks to
that host: the `SshBackend` (exec / file IO for the agent's tools), and — in
later phases — the desktop file panel and the remote checkpoint store. Built on
the SYSTEM OpenSSH client, not paramiko/asyncssh, so the user's `~/.ssh/config`
works at 100% fidelity (aliases, ProxyJump, IdentityFile, agent, Match blocks,
Include). The trade: we shell out per operation — cheap, because a
`ControlMaster` connection turns each op into a channel open (~tens of ms on a
LAN), not a fresh TCP+auth handshake.

Auth policy (v1, deliberate):
  - Keys / agent only. `BatchMode=yes` on every invocation, so ssh NEVER
    prompts (no password auth, no interactive host-key confirmation). If the
    host isn't reachable non-interactively, we fail with a clear message
    telling the user to run `ssh <host>` once in their terminal first.
  - Host-key verification is inherited from the user's ssh config/known_hosts
    (we do not relax StrictHostKeyChecking). Tests pass `extra_ssh_opts` to
    point at a throwaway known_hosts for the disposable sshd container.

Secret hygiene (load-bearing):
  - Command *environment* (the session's resolved secrets) is delivered via an
    stdin SCRIPT piped to `/bin/sh -s` on the remote — never on the ssh argv —
    so values are invisible to `ps` on both machines. Only env var NAMES may
    be logged.

Execution model:
  - `run(cmd, env=…, cwd=…)`  → remote `/bin/sh -s`, script = exports + cd +
    the command text. Exit code is the command's own. NOTE: a command that
    reads stdin will consume (already-drained) script bytes — same caveat as a
    shell heredoc; the tool layer never feeds stdin to run_bash, so this is
    theoretical.
  - `read_file(path)`         → remote `cat < 'path'` (binary-safe stdout).
  - `write_file(path, data)`  → remote `mkdir -p 'parent' && cat > 'path'`,
    data over stdin (binary-safe; path is not secret, so argv is fine).
  - `probe()`                 → one round trip caching remote $HOME, git
    presence, and uname — consumed by the backend and later phases.

Timeouts are enforced CLIENT-side (kill the local ssh process). The remote
command may keep running after a timeout kill — documented v1 limitation (a
remote `timeout(1)` wrapper is not portable across busybox/macOS remotes).

POSIX remotes only in v1: `sh`, `cat`, `mkdir -p` are assumed. `probe()`
surfaces `uname` so callers can fail early on unsupported platforms.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
from typing import Optional

from .config import ExecChunk, ExecResult

log = logging.getLogger(__name__)

# Where ControlMaster sockets live. `%C` is ssh's own hash token
# (local host + remote host + port + user), which keeps the socket path
# under the unix-socket length limit and makes reuse automatic: the same
# remote → the same master, across backends, panels, and sessions.
_DEFAULT_CONTROL_DIR = "~/.adk-cc-ssh"

# How long an idle master persists after the last channel closes. Long
# enough that a chat's tool calls ride one connection; short enough that
# an abandoned session doesn't pin the remote forever.
_CONTROL_PERSIST_S = 600

_CONNECT_TIMEOUT_S = 10

# stderr shapes that mean "the TRANSPORT failed" (as opposed to the remote
# command exiting non-zero). Checked only when ssh exits 255 — the client's
# own error code — since a remote command could legitimately exit 255 too.
_TRANSPORT_ERR_RE = re.compile(
    r"(ssh:|Connection (refused|reset|closed|timed out)|Could not resolve"
    r"|Permission denied|Host key verification failed|kex_exchange"
    r"|Broken pipe|Operation timed out|No route to host"
    r"|Control socket connect)",
    re.IGNORECASE,
)


class SshConnectionError(Exception):
    """The SSH *transport* failed (unreachable host, auth, host key) —
    distinct from a remote command failing. Carries a user-actionable
    message; retry/backoff policy is the caller's concern."""


# Unix sockets cap sun_path at ~104 bytes on macOS (108 on Linux), and the
# ControlPath's `%C` token expands to a 40-char hash — so the control DIR
# itself must stay short. macOS $TMPDIR alone (`/var/folders/…/T/…`) already
# blows the budget. Keep headroom: dir + "/" + 40 ≤ ~100.
_MAX_CONTROL_DIR_LEN = 59


def _usable_control_dir(preferred: str) -> str:
    """`preferred` if a `%C` socket fits under the unix-socket path limit,
    else a SHORT deterministic per-config fallback (`/tmp/adk-ssh-<hash>`,
    0700). Callers keep isolation (unique dir per configured path); ssh
    keeps a valid socket. Pure-ish (no fs writes); unit-tested."""
    if len(preferred) <= _MAX_CONTROL_DIR_LEN:
        return preferred
    import hashlib

    short = f"/tmp/adk-ssh-{hashlib.sha1(preferred.encode()).hexdigest()[:8]}"
    log.debug(
        "ssh: control dir %r too long for a unix socket; using %s", preferred, short
    )
    return short


def _shq(s: str) -> str:
    """Safe single-quoting for POSIX sh (shlex.quote, aliased for brevity)."""
    return shlex.quote(s)


def build_script(cmd: str, *, cwd: Optional[str] = None, env: Optional[dict] = None) -> str:
    """The stdin script for `run()`: exports, cd, then the command text.

    Pure function (unit-tested): env VALUES appear only here — the script
    travels over stdin, never argv. `cd` failure exits 96 so a missing
    cwd is distinguishable from the command's own exit codes.
    """
    lines: list[str] = []
    for k in sorted(env or {}):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            # An invalid name can't be exported; skip rather than inject
            # sh syntax. Names only in logs — never values.
            log.warning("ssh: skipping invalid env var name %r", k)
            continue
        lines.append(f"export {k}={_shq((env or {})[k])}")
    if cwd:
        lines.append(f"cd {_shq(cwd)} || exit 96")
    lines.append(cmd)
    return "\n".join(lines) + "\n"


def looks_like_transport_error(exit_code: int, stderr: str) -> bool:
    """True when (exit_code, stderr) indicates the ssh CLIENT failed rather
    than the remote command. Pure; unit-tested."""
    return exit_code == 255 and bool(_TRANSPORT_ERR_RE.search(stderr))


class SshTransport:
    """One remote host; every operation rides the shared ControlMaster."""

    def __init__(
        self,
        host: str,
        *,
        port: Optional[int] = None,
        identity_file: Optional[str] = None,
        extra_ssh_opts: tuple[str, ...] = (),
        control_dir: Optional[str] = None,
        connect_timeout_s: float = _CONNECT_TIMEOUT_S,
    ) -> None:
        self.host = host
        self._port = port
        self._identity = identity_file
        self._extra = tuple(extra_ssh_opts)
        self._connect_timeout_s = connect_timeout_s
        cd = control_dir or os.environ.get("ADK_CC_SSH_CONTROL_DIR") or _DEFAULT_CONTROL_DIR
        self._control_dir = _usable_control_dir(os.path.expanduser(cd))
        # Sockets are credentials-adjacent: 0700, like ~/.ssh itself.
        os.makedirs(self._control_dir, mode=0o700, exist_ok=True)
        self._probe_cache: Optional[dict] = None

    # --- argv construction (pure-ish; unit-tested via build_argv) ---------

    def build_argv(self, remote_command: list[str]) -> list[str]:
        """The full local argv for one operation. Batch-mode, multiplexed,
        quiet. `remote_command` items are joined by ssh into the remote
        shell command string — callers pre-quote anything path-like."""
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_dir}/%C",
            "-o", f"ControlPersist={_CONTROL_PERSIST_S}",
            "-o", f"ConnectTimeout={int(self._connect_timeout_s)}",
            "-o", "LogLevel=ERROR",  # keep banners/motd out of stderr parsing
        ]
        if self._port:
            argv += ["-p", str(self._port)]
        if self._identity:
            argv += ["-i", self._identity]
        argv += list(self._extra)
        argv.append(self.host)
        argv += remote_command
        return argv

    # --- core ops ---------------------------------------------------------

    async def run(
        self,
        cmd: str,
        *,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
        timeout_s: float = 60.0,
    ) -> ExecResult:
        """Run `cmd` under `/bin/sh -s` on the remote; env+cwd via the stdin
        script (secrets never on argv). Raises SshConnectionError on
        transport failure; remote command failures return normally."""
        script = build_script(cmd, cwd=cwd, env=env)
        code, out, err = await self._spawn(
            self.build_argv(["/bin/sh", "-s"]),
            stdin_data=script.encode("utf-8"),
            timeout_s=timeout_s,
        )
        if code is None:  # timed out
            return ExecResult(exit_code=-1, stdout=out.decode("utf-8", "replace"),
                              stderr=err.decode("utf-8", "replace"), timed_out=True)
        err_text = err.decode("utf-8", "replace")
        if looks_like_transport_error(code, err_text):
            raise SshConnectionError(self._friendly(err_text))
        return ExecResult(
            exit_code=code,
            stdout=out.decode("utf-8", "replace"),
            stderr=err_text,
            timed_out=False,
        )

    async def run_stream(
        self,
        cmd: str,
        *,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
        timeout_s: float = 60.0,
    ):
        """Like `run()`, but yields `ExecChunk`s live as output arrives,
        terminating with exactly one `kind="result"` chunk (the transport
        analogue of `SandboxBackend.exec_stream`). Raises
        SshConnectionError only for a transport-shaped failure detected at
        exit; output seen up to that point has already been yielded."""
        script = build_script(cmd, cwd=cwd, env=env)
        argv = self.build_argv(["/bin/sh", "-s"])
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None
        proc.stdin.write(script.encode("utf-8"))
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # remote exited before reading the whole script
        proc.stdin.close()

        queue: asyncio.Queue = asyncio.Queue()
        out_parts: list[str] = []
        err_parts: list[str] = []

        async def pump(stream, kind: str, parts: list[str]) -> None:
            while True:
                block = await stream.read(4096)
                if not block:
                    break
                text = block.decode("utf-8", "replace")
                parts.append(text)
                await queue.put(ExecChunk(kind=kind, data=text))
            await queue.put(None)  # this pump is done

        pumps = [
            asyncio.ensure_future(pump(proc.stdout, "stdout", out_parts)),
            asyncio.ensure_future(pump(proc.stderr, "stderr", err_parts)),
        ]
        deadline = asyncio.get_event_loop().time() + timeout_s
        timed_out = False
        done_pumps = 0
        try:
            while done_pumps < 2:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                if item is None:
                    done_pumps += 1
                    continue
                yield item
        finally:
            if timed_out:
                proc.kill()
            for p in pumps:
                if not p.done():
                    p.cancel()
        await proc.wait()

        stderr_text = "".join(err_parts)
        if not timed_out and looks_like_transport_error(
            proc.returncode or 0, stderr_text
        ):
            raise SshConnectionError(self._friendly(stderr_text))
        yield ExecChunk(
            kind="result",
            result=ExecResult(
                exit_code=-1 if timed_out else (proc.returncode or 0),
                stdout="".join(out_parts),
                stderr=stderr_text,
                timed_out=timed_out,
            ),
        )

    async def read_file(self, path: str, *, timeout_s: float = 60.0) -> bytes:
        """Binary-safe read via `cat < path`. FileNotFoundError on a missing
        file; SshConnectionError on transport failure."""
        code, out, err = await self._spawn(
            self.build_argv([f"cat < {_shq(path)}"]), stdin_data=None, timeout_s=timeout_s
        )
        if code == 0:
            return out
        err_text = err.decode("utf-8", "replace")
        if code is None:
            raise SshConnectionError(f"ssh read timed out for {path!r}")
        if looks_like_transport_error(code, err_text):
            raise SshConnectionError(self._friendly(err_text))
        if "o such file" in err_text or "annot open" in err_text:
            raise FileNotFoundError(path)
        raise RuntimeError(f"ssh read failed ({code}) for {path!r}: {err_text[:200]}")

    async def write_file(
        self, path: str, data: bytes, *, mkdirs: bool = True, timeout_s: float = 60.0
    ) -> None:
        """Binary-safe write via `cat > path`, data over stdin. `mkdirs`
        creates the parent in the same round trip."""
        parent = os.path.dirname(path.rstrip("/"))
        pre = f"mkdir -p {_shq(parent)} && " if (mkdirs and parent) else ""
        code, _out, err = await self._spawn(
            self.build_argv([f"{pre}cat > {_shq(path)}"]),
            stdin_data=data,
            timeout_s=timeout_s,
        )
        if code == 0:
            return
        err_text = err.decode("utf-8", "replace")
        if code is None:
            raise SshConnectionError(f"ssh write timed out for {path!r}")
        if looks_like_transport_error(code, err_text):
            raise SshConnectionError(self._friendly(err_text))
        raise RuntimeError(f"ssh write failed ({code}) for {path!r}: {err_text[:200]}")

    async def probe(self, *, refresh: bool = False, timeout_s: float = 20.0) -> dict:
        """One cached round trip: `{'home': str, 'git': bool, 'uname': str}`.
        Raises SshConnectionError when the host is unreachable — callers use
        this as the connection test."""
        if self._probe_cache is not None and not refresh:
            return self._probe_cache
        res = await self.run(
            'printf "H=%s\\n" "$HOME"; '
            "command -v git >/dev/null 2>&1 && echo G=1 || echo G=0; "
            'printf "U=%s\\n" "$(uname -s)"',
            timeout_s=timeout_s,
        )
        if res.exit_code != 0:
            raise SshConnectionError(
                f"ssh probe of {self.host!r} failed "
                f"(exit {res.exit_code}): {res.stderr[:200]}"
            )
        info: dict = {"home": "", "git": False, "uname": ""}
        for line in res.stdout.splitlines():
            if line.startswith("H="):
                info["home"] = line[2:].strip()
            elif line.startswith("G="):
                info["git"] = line[2:].strip() == "1"
            elif line.startswith("U="):
                info["uname"] = line[2:].strip()
        self._probe_cache = info
        return info

    # --- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Ask the ControlMaster to exit. Best-effort, never raises."""
        try:
            subprocess.run(
                self.build_argv([])[:-1] + ["-O", "exit", self.host],
                capture_output=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001 — teardown must not propagate
            pass

    def is_connected(self) -> bool:
        """True when a live master exists for this host (no new auth)."""
        try:
            r = subprocess.run(
                self.build_argv([])[:-1] + ["-O", "check", self.host],
                capture_output=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    # --- internals --------------------------------------------------------

    async def _spawn(
        self, argv: list[str], *, stdin_data: Optional[bytes], timeout_s: float
    ) -> tuple[Optional[int], bytes, bytes]:
        """Run one ssh op; `(exit_code|None-on-timeout, stdout, stderr)`.
        Timeout kills the LOCAL ssh process (channel drops; remote may
        outlive — documented v1 limitation)."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            # The pipes may NOT hit EOF on kill: under multiplexing the
            # background ControlMaster still holds the channel's write end
            # until the REMOTE command exits — a plain communicate() here
            # would block for the rest of the remote runtime (live-e2e
            # reproduced: 30s for a `sleep 30`). Bounded grace, then abandon.
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            except Exception:  # noqa: BLE001 — grace expired; drop the pipes
                out, err = b"", b""
            return None, out or b"", err or b""
        return proc.returncode, out, err

    def _friendly(self, stderr: str) -> str:
        """Actionable message for the failure modes BatchMode surfaces."""
        s = stderr.strip().splitlines()[-1] if stderr.strip() else "connection failed"
        hint = ""
        if "Host key verification failed" in stderr or "Permission denied" in stderr:
            hint = (
                f" — set up key auth and trust the host first: run "
                f"`ssh {self.host}` once in your terminal, then retry"
            )
        return f"ssh to {self.host!r} failed: {s}{hint}"


# --- shared registry ------------------------------------------------------
# The panel/checkpoint phases resolve transports here so ALL traffic to one
# host shares one master. Keyed by (host, port, identity).

_REGISTRY: dict[tuple, SshTransport] = {}


def get_transport(
    host: str,
    *,
    port: Optional[int] = None,
    identity_file: Optional[str] = None,
    extra_ssh_opts: tuple[str, ...] = (),
) -> SshTransport:
    key = (host, port, identity_file, extra_ssh_opts)
    t = _REGISTRY.get(key)
    if t is None:
        t = SshTransport(
            host, port=port, identity_file=identity_file, extra_ssh_opts=extra_ssh_opts
        )
        _REGISTRY[key] = t
    return t


def close_all() -> None:
    """Drop every cached master (best-effort; server shutdown hook)."""
    for t in _REGISTRY.values():
        t.close()
    _REGISTRY.clear()
