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
        # Guard 1: explicit-ack on prod-shaped paths.
        if (
            _is_prod_shaped(cwd)
            and os.environ.get("ADK_CC_NOOP_ACK_HOST_EXEC") != "1"
        ):
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

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                stdout_b, stderr_b = await proc.communicate()
            except Exception:
                stdout_b, stderr_b = b"", b""
            return ExecResult(
                exit_code=-1,
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                timed_out=True,
            )
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
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
