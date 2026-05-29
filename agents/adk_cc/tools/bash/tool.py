from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_backend, get_workspace
from ...sandbox.config import ExecChunk, ExecResult, NetworkConfig
from ..base import AdkCcTool, ToolMeta
from ..schemas import RunBashArgs
from .prompt import DESCRIPTION

_log = logging.getLogger(__name__)


class BashTool(AdkCcTool):
    """Shell command execution, delegated to the active SandboxBackend.

    The default `noop` backend runs on the host (dev only). Production
    deployments configure `ADK_CC_SANDBOX_BACKEND=docker|e2b|sandbox_service`
    and the selected backend isolates execution per session.

    Streaming: when `ADK_CC_BASH_STREAM=1` is set, the tool uses the
    backend's `exec_stream` method to receive output incrementally and
    logs each chunk at INFO. The model still receives a single
    aggregated result — streaming is operator-side observability for
    long-running commands. Backends without native streaming
    (Noop / Docker / E2B today) fall back to the ABC default impl which
    yields one chunk at the end; only `sandbox_service` actually
    streams live.
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
        if os.environ.get("ADK_CC_BASH_STREAM") == "1":
            result = await self._exec_streaming(backend, ws, args)
        else:
            result = await backend.exec(
                args.command,
                fs_write=ws.fs_write_config(),
                network=NetworkConfig(),
                timeout_s=args.timeout_seconds,
                cwd=ws.abs_path,
            )

        if result.timed_out:
            _log.warning(
                "run_bash timed out after %ss: %s",
                args.timeout_seconds,
                args.command[:200],
                extra={
                    "command": args.command,
                    "timeout_seconds": args.timeout_seconds,
                    "outcome": "timeout",
                },
            )
            return {
                "status": "timeout",
                "command": args.command,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        # Surface non-zero exits at WARNING — silent failures are
        # exactly what the user couldn't see before. Zero exits go to
        # DEBUG so they only show up when an operator opts in.
        if result.exit_code != 0:
            _log.warning(
                "run_bash exit_code=%s command=%s stderr_tail=%s",
                result.exit_code,
                args.command[:200],
                result.stderr[-200:].replace("\n", " "),
                extra={
                    "command": args.command,
                    "exit_code": result.exit_code,
                    "outcome": "nonzero_exit",
                },
            )
        elif _log.isEnabledFor(logging.DEBUG):
            _log.debug(
                "run_bash exit_code=0 command=%s",
                args.command[:200],
                extra={
                    "command": args.command,
                    "exit_code": 0,
                    "outcome": "ok",
                },
            )
        return {
            "status": "ok",
            "command": args.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
        }

    async def _exec_streaming(self, backend, ws, args: RunBashArgs) -> ExecResult:
        """Drive `backend.exec_stream`, log chunks at INFO, return the
        aggregated final ExecResult. Falls back transparently to the
        ABC's default (one chunk at end) when the backend doesn't
        actually stream — same contract either way.
        """
        final: ExecResult | None = None
        async for chunk in backend.exec_stream(
            args.command,
            fs_write=ws.fs_write_config(),
            network=NetworkConfig(),
            timeout_s=args.timeout_seconds,
            cwd=ws.abs_path,
        ):
            if chunk.kind == "result":
                final = chunk.result
            elif chunk.kind in ("stdout", "stderr"):
                # One log line per chunk. Operators tailing the agent's
                # log see live progress; the model still gets the
                # aggregated result via the tool's return value.
                _log.info(
                    "run_bash[%s]: %s",
                    chunk.kind,
                    # Trim each chunk so a single noisy command can't
                    # spam the log; full output goes to the model via
                    # the final aggregated stdout/stderr.
                    chunk.data.rstrip("\n")[:1000],
                )
        if final is None:
            # Backend default impl always yields a result; if it didn't,
            # synthesize a clean error rather than crashing the tool.
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr="run_bash: backend exec_stream produced no result chunk",
                timed_out=False,
            )
        return final
