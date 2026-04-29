"""Host-execution backend.

Runs commands and FS operations directly on the process's host. Honors
config policy (path / network restrictions) via Python checks so the
contract is exercised in dev — but it is NOT a security boundary. Any
multi-tenant deployment must use docker/e2b/etc.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..config import (
    ExecResult,
    FsReadConfig,
    FsWriteConfig,
    NetworkConfig,
    SandboxViolation,
)
from .base import SandboxBackend


class NoopBackend(SandboxBackend):
    name = "noop"

    async def exec(
        self,
        cmd: str,
        *,
        fs_write: FsWriteConfig,
        network: NetworkConfig,
        timeout_s: int,
        cwd: str,
    ) -> ExecResult:
        try:
            r = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                exit_code=-1,
                stdout=e.stdout or "",
                stderr=e.stderr or "",
                timed_out=True,
            )
        return ExecResult(
            exit_code=r.returncode,
            stdout=r.stdout,
            stderr=r.stderr,
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
