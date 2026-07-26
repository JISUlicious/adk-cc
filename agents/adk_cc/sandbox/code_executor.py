"""Adapter from ADK's `BaseCodeExecutor` to adk-cc's `SandboxBackend`.

Skill scripts and any other code-executor-driven path run inside the
active per-session sandbox container, not on the agent host. With
`NoopBackend` it's host execution (dev only); with `DockerBackend` it's
the same per-session container that handles `run_bash`.

Without this adapter, ADK's default code executor runs Python on the
agent process — defeating the sandbox boundary for skills.

Implementation: writes the code to a workspace-relative scratch file
via `backend.write_text`, runs it with a uv-managed interpreter
(`analysis_env.ensure_env`) via `backend.exec`,
returns stdout/stderr. The exec lifecycle is async; ADK's
`execute_code` is sync, so we run the async work on a private event
loop in a worker thread (the conventional pattern when ADK calls a
sync method from inside its own running loop).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import threading
from typing import Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.code_executors.code_execution_utils import (
    CodeExecutionInput,
    CodeExecutionResult,
)

from .config import NetworkConfig
from .analysis_env import AnalysisEnvError, ensure_env, required_tiers
from .backends.base import SandboxBackend
from .workspace import WorkspaceRoot


class SandboxBackedCodeExecutor(BaseCodeExecutor):
    """Run code through the active session's `SandboxBackend.exec`."""

    timeout_seconds: int = 60

    def execute_code(
        self,
        invocation_context: InvocationContext,
        code_execution_input: CodeExecutionInput,
    ) -> CodeExecutionResult:
        # ADK's flow is async; this method is declared sync per the ABC.
        # asyncio.run() from inside a running loop raises — run on a
        # private loop in a worker thread.
        result_box: list[CodeExecutionResult] = []
        error_box: list[BaseException] = []

        def _runner() -> None:
            try:
                result_box.append(
                    asyncio.run(
                        self._execute_async(invocation_context, code_execution_input)
                    )
                )
            except BaseException as e:  # noqa: BLE001 — propagate to caller
                error_box.append(e)

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if error_box:
            raise error_box[0]
        return result_box[0]

    async def _execute_async(
        self,
        invocation_context: InvocationContext,
        code_execution_input: CodeExecutionInput,
    ) -> CodeExecutionResult:
        state = invocation_context.session.state
        backend: Optional[SandboxBackend] = state.get("temp:sandbox_backend")
        ws: Optional[WorkspaceRoot] = state.get("temp:sandbox_workspace")
        if backend is None or ws is None:
            return CodeExecutionResult(
                stdout="",
                stderr=(
                    "SandboxBackedCodeExecutor: no sandbox backend or workspace "
                    "in session state. Make sure TenancyPlugin is active."
                ),
            )

        # Stable name for stateful execution_id so the model can refer to
        # files between turns; ephemeral otherwise.
        eid = code_execution_input.execution_id or "scratch"
        rel_tmpfile = f".adk-cc/code/{eid}.py"
        abs_tmpfile = os.path.join(ws.abs_path, rel_tmpfile)

        try:
            # write_text takes the agent-side absolute path; the backend
            # translates it to whatever the runtime sees (e.g. /workspace
            # for Docker / sandbox_service, or the same path for Noop).
            await backend.write_text(
                abs_tmpfile,
                code_execution_input.code,
                fs_write=ws.fs_write_config(),
            )
            # NB: pass `cmd` as a relative path, NOT the absolute one.
            # Commands are opaque to backends — paths baked into the cmd
            # string don't get translated. cwd=ws.abs_path IS translated
            # by the backend (Docker / sandbox_service → /workspace,
            # Noop → identity), so the relative path resolves correctly
            # inside whichever runtime is in play.
            # NEVER a bare `python3`: inside NoopBackend that resolves to the
            # host interpreter (stock macOS: Python 3.9 with no packages), which
            # made every analysis skill fail on its first import. `analysis_env`
            # supplies a uv-managed interpreter — and escalates package tiers
            # based on what this code actually imports, so a trivial script
            # doesn't pay for the modeling stack.
            env = await ensure_env(
                backend, ws, tiers=required_tiers(code_execution_input.code)
            )
            cmd = f"{shlex.quote(env.python)} {shlex.quote(rel_tmpfile)}"
            res = await backend.exec(
                cmd,
                fs_write=ws.fs_write_config(),
                network=NetworkConfig(),
                timeout_s=self.timeout_seconds,
                cwd=ws.abs_path,
            )
        except AnalysisEnvError as e:
            # Actionable by construction — surface verbatim rather than as a
            # bare ModuleNotFoundError three steps later.
            return CodeExecutionResult(stdout="", stderr=str(e))
        except Exception as e:  # noqa: BLE001 — surface as stderr
            return CodeExecutionResult(stdout="", stderr=f"{type(e).__name__}: {e}")

        return CodeExecutionResult(
            stdout=res.stdout,
            stderr=res.stderr,
        )
