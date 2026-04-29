"""Docker-based sandbox backend (stub).

Operator implements this when self-hosting. The shape:
  - One container per session, started lazily on first exec/read/write.
  - Bind-mount `WorkspaceRoot.abs_path` → /workspace inside the container.
  - Translate `fs_read`/`fs_write` into bind-mount configurations and
    optional `--read-only` / `--mount` flags.
  - Translate `network.allow_domains` into iptables rules or use a
    pre-configured Docker network with egress filtering.
  - Reuse warm pools to keep latency under ~200ms per call.

Pseudocode for the operator implementing this:

    async def exec(self, cmd, ..., timeout_s, cwd):
        container = await self._get_or_create_container(self.session_id)
        result = await container.exec_run(
            ["bash", "-c", cmd],
            workdir=cwd,
            user=self.unprivileged_uid,
            demux=True,
        )
        ...
"""

from __future__ import annotations

from ..config import ExecResult, FsReadConfig, FsWriteConfig, NetworkConfig
from .base import SandboxBackend


class DockerBackend(SandboxBackend):
    name = "docker"

    def __init__(self, *, image: str = "adk-cc-sandbox:latest") -> None:
        self.image = image
        # See module docstring for the implementation outline. The runtime
        # pieces (docker SDK client, container pool, bind-mount setup) are
        # left to the operator so adk-cc itself stays minimal.

    async def exec(self, cmd, *, fs_write, network, timeout_s, cwd):
        raise NotImplementedError("DockerBackend.exec — see module docstring")

    async def read_text(self, path, *, fs_read):
        raise NotImplementedError("DockerBackend.read_text — see module docstring")

    async def write_text(self, path, content, *, fs_write):
        raise NotImplementedError("DockerBackend.write_text — see module docstring")
