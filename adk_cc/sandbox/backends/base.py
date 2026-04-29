"""Abstract sandbox backend.

Tools call into this contract instead of touching the host directly. The
concrete backend (noop / docker / e2b / ...) decides how the operation
actually runs. The contract is intentionally narrow:

  - `exec(cmd, ...)` for shell commands
  - `read_text(path, ...)` for FS reads
  - `write_text(path, content, ...)` for FS writes

`fs_read` / `fs_write` / `network` configs are passed per call so the
caller (the tool) can scope each operation to the active workspace and
the operator's policy. Backends that genuinely isolate (docker, e2b)
implement these via bind mounts / firewall rules / etc.; the noop
backend honors them via Python checks so the contract is exercised
end-to-end in dev.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import ExecResult, FsReadConfig, FsWriteConfig, NetworkConfig


class SandboxBackend(ABC):
    """Abstract isolation boundary."""

    name: str = "abstract"

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

    @abstractmethod
    async def read_text(self, path: str, *, fs_read: FsReadConfig) -> str:
        ...

    @abstractmethod
    async def write_text(
        self, path: str, content: str, *, fs_write: FsWriteConfig
    ) -> None:
        ...
