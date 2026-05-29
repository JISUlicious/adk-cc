"""e2b-based sandbox backend (stub).

Recommended for hosted multi-tenant production. Per-session Firecracker
microVMs map 1:1 to (tenant_id, session_id, workspace_root); built-in
network isolation; managed by e2b.

Operator implements this. The shape:
  - On session start, `Sandbox.create()` returns a session-scoped VM.
  - `exec` calls `sandbox.commands.run(cmd, cwd=cwd, timeout=timeout_s)`.
  - `read_text` / `write_text` use `sandbox.files.read(path)` /
    `sandbox.files.write(path, content)`.
  - Network policy: configure at sandbox creation via
    `Sandbox.create(template=..., metadata=..., timeout=...)` plus
    e2b's per-template firewall rules.
  - Sandbox lifetime tied to the ADK session; tear down on session end.

Add `e2b` as an optional extra in pyproject.toml:
    [project.optional-dependencies]
    e2b = ["e2b>=1.0"]
"""

from __future__ import annotations

from ..config import ExecResult, FsReadConfig, FsWriteConfig, NetworkConfig
from .base import SandboxBackend


class E2BBackend(SandboxBackend):
    name = "e2b"

    def __init__(self, *, template: str = "base", api_key: str | None = None) -> None:
        self.template = template
        self.api_key = api_key
        # See module docstring for the implementation outline. e2b SDK
        # integration is left to the operator so adk-cc keeps its dep
        # tree minimal.

    async def exec(self, cmd, *, fs_write, network, timeout_s, cwd):
        raise NotImplementedError("E2BBackend.exec — see module docstring")

    async def read_text(self, path, *, fs_read):
        raise NotImplementedError("E2BBackend.read_text — see module docstring")

    async def write_text(self, path, content, *, fs_write):
        raise NotImplementedError("E2BBackend.write_text — see module docstring")
