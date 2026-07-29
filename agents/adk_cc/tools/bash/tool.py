from __future__ import annotations

import re

import logging
import os
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

from ...sandbox import get_backend, get_workspace
from ...sandbox.config import ExecChunk, ExecResult, NetworkConfig
from ..base import AdkCcTool, ToolMeta
from ..schemas import RunBashArgs
from .prompt import DESCRIPTION
from ...config.schema import env_bool

_log = logging.getLogger(__name__)


_INVOKES_PYTHON = re.compile(r"(?:^|[\s;&|(`$])(?:python3?|pip3?|uv\s+run)\b")


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


    async def _with_managed_python(self, backend, ws, args: RunBashArgs) -> RunBashArgs:
        """Put the uv-managed interpreter on PATH when the command runs Python.

        A bare `python3` in a shell resolves to whatever the runtime ships —
        on stock macOS that is Python 3.9 with no packages, so any analysis
        one-liner fails on `import pandas` even though the code executor has a
        perfectly good managed env. Only python-invoking commands pay the
        (first-time, then cached) provisioning cost.

        `$PWD` is used rather than a relative entry because cwd is the
        workspace in every backend, and a script that `cd`s must not lose the
        interpreter. Failures degrade to the original command — a broken
        analysis env must never make ordinary shell commands unrunnable.
        """
        if not _INVOKES_PYTHON.search(args.command or ""):
            return args
        try:
            from ...sandbox.analysis_env import ensure_env, required_tiers

            env = await ensure_env(
                backend, ws, tiers=required_tiers(args.command)
            )
            if not env.is_managed:
                return args
            bin_dir = env.python.rsplit("/", 1)[0]
            prefixed = f'export PATH="$PWD/{bin_dir}:$PATH"; {args.command}'
            return args.model_copy(update={"command": prefixed})
        except Exception as e:  # noqa: BLE001 — never block a shell command
            # Record WHY. Without this the user just sees `exit 127: python:
            # command not found`, which is precisely the unexplained failure
            # this whole subsystem exists to prevent.
            self._env_note = str(e)
            _log.warning("managed python unavailable (%s: %s); using PATH as-is",
                         type(e).__name__, str(e)[:200])
            return args

    async def _dataset_guard(self, backend, ws, args: RunBashArgs) -> Optional[str]:
        """Refuse a python command that would load an oversized dataset.

        Returns the refusal text, or None to proceed. Costs one `wc -c` round
        trip, and ONLY when a python command names a data file it is not
        already sampling — see sandbox/dataset_guard.py for why it is scoped
        this narrowly.
        """
        from ...sandbox import dataset_guard as dg

        cmd = args.command or ""
        if not dg.enabled() or not _INVOKES_PYTHON.search(cmd):
            return None
        if dg.already_samples(cmd):
            return None
        paths = dg.data_paths(cmd)
        if not paths:
            return None
        try:
            probe = await backend.exec(
                dg.size_probe(paths), fs_write=None, network=NetworkConfig(),
                timeout_s=20, cwd=ws.abs_path,
            )
        except Exception:  # noqa: BLE001 — a guard must never break the tool
            return None
        over = dg.oversized(dg.parse_sizes(getattr(probe, "stdout", "") or ""))
        return dg.refusal(over) if over else None

    async def _execute(self, args: RunBashArgs, ctx: ToolContext) -> dict[str, Any]:
        backend = get_backend(ctx)
        ws = get_workspace(ctx)
        self._env_note: str = ""
        args = await self._with_managed_python(backend, ws, args)
        refusal = await self._dataset_guard(backend, ws, args)
        if refusal:
            _log.info("dataset guard refused: %s", args.command[:160])
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": refusal,
                "timed_out": False,
                "error_code": "DATASET_TOO_LARGE",
            }
        # Network policy is intentionally empty here — bash with no
        # explicit network allowlist gets no egress in real backends.
        # Operators wanting outbound for builds (apt, pip) configure
        # this via Stage E's WebFetch path or by setting NetworkConfig
        # at session-state level.
        if env_bool("ADK_CC_BASH_STREAM"):
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
            # `timed_out` + a plain-language note: without them the UI had
            # only a missing exit_code to go on and rendered a bare `exit ?`,
            # which says nothing about what happened or why. A background job
            # holding the output pipe is the usual cause, so name it.
            hint = (
                f"command timed out after {args.timeout_seconds}s and was killed "
                "(its process group too). Output above is what it printed before "
                "then. If it starts a background process, redirect that process's "
                "output (`cmd >/dev/null 2>&1 &`) or it keeps the pipe open."
            )
            return {
                "status": "timeout",
                "timed_out": True,
                "timeout_seconds": args.timeout_seconds,
                "command": args.command,
                "stdout": result.stdout,
                "stderr": (result.stderr + "\n" + hint).strip(),
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
        stderr = result.stderr[-2000:]
        if self._env_note and result.exit_code != 0:
            # The command failed AND the managed interpreter was unavailable —
            # almost certainly the cause. Say so instead of leaving the model
            # to guess at `python: command not found`.
            stderr = (
                "[adk-cc] the managed Python environment could not be "
                f"prepared, so `python` may be missing or lack packages:\n"
                f"{self._env_note}\n\n" + stderr
            )
        return {
            "status": "ok",
            "command": args.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-4000:],
            "stderr": stderr,
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
