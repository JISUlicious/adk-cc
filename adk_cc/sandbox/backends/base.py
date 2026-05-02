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
from typing import TYPE_CHECKING

from ..config import ExecResult, FsReadConfig, FsWriteConfig, NetworkConfig

if TYPE_CHECKING:
    from ..workspace import WorkspaceRoot


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

    async def ensure_workspace(self, ws: "WorkspaceRoot") -> None:
        """Create the workspace dir if it doesn't exist.

        Default no-op for backends that don't need init (e.g. backends
        where workspace creation happens elsewhere). NoopBackend does a
        local mkdir; DockerBackend creates the dir on the remote VM.
        """
        return None

    async def close(self) -> None:
        """Tear down per-session state (e.g. stop and remove a container).

        Default no-op. Wired into ADK's session-end via the tenancy plugin.
        Best-effort — should not raise; a failure to clean up shouldn't
        block the next session.
        """
        return None
