from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_backend, get_workspace
from ...sandbox.config import NetworkConfig
from ..base import AdkCcTool, ToolMeta
from ..schemas import RunBashArgs
from .prompt import DESCRIPTION


class BashTool(AdkCcTool):
    """Shell command execution, delegated to the active SandboxBackend.

    The default `noop` backend runs on the host (dev only). Production
    deployments configure `ADK_CC_SANDBOX_BACKEND=docker|e2b` and the
    selected backend isolates execution per session.
    """

    meta = ToolMeta(
        name="run_bash",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
        needs_sandbox=True,
    )
    input_model = RunBashArgs
    description = DESCRIPTION

    async def _execute(self, args: RunBashArgs, ctx: ToolContext) -> dict[str, Any]:
        backend = get_backend(ctx)
        ws = get_workspace(ctx)
        # Network policy is intentionally empty here — bash with no
        # explicit network allowlist gets no egress in real backends.
        # Operators wanting outbound for builds (apt, pip) configure
        # this via Stage E's WebFetch path or by setting NetworkConfig
        # at session-state level.
        result = await backend.exec(
            args.command,
            fs_write=ws.fs_write_config(),
            network=NetworkConfig(),
            timeout_s=args.timeout_seconds,
            cwd=ws.abs_path,
        )
        if result.timed_out:
            return {
                "status": "timeout",
                "command": args.command,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        return {
            "status": "ok",
            "command": args.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
        }
