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
import hashlib
import re
import shlex
import threading
from typing import Optional

from google.adk.agents.invocation_context import InvocationContext
from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.code_executors.code_execution_utils import (
    CodeExecutionInput,
    CodeExecutionResult,
)

from ..branding import NOTE_PREFIX
from .config import NetworkConfig
from .analysis_env import AnalysisEnvError, ensure_env, required_tiers
from .backends.base import SandboxBackend
from .workspace import WorkspaceRoot


# The skill-script launcher states the imports of the skill it is about to run,
# because the script's source is NOT in the code sent here — it is materialised
# in the workspace and run as a subprocess, and a warm run ships no source at
# all. Sizing the environment from what happens to be inline would provision
# `base` and the script would die on its first import.
_SKILL_TIERS_RE = re.compile(r"^# adk-cc-skill-tiers:(.*)$", re.M)


def _tiers_for(code: str) -> frozenset[str]:
    """Package tiers the code needs, including a skill script's own imports.

    Worth being explicit about why the header exists: `required_tiers` matches
    `^import x` per line, and a data-analyst probe importing numpy, pandas and
    sklearn resolved to ZERO tiers when its source was only present as an
    escaped one-line repr — the environment came up without them and the probe
    died on "No module named 'numpy'".
    """
    tiers = required_tiers(code)
    m = _SKILL_TIERS_RE.search(code or "")
    if m:
        names = [n for n in m.group(1).split() if n.isidentifier()]
        if names:
            tiers = tiers | required_tiers("\n".join(f"import {n}" for n in names))
    return tiers


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

        # Stable name for a stateful execution_id so the model can refer to
        # files between turns. WITHOUT one, the name is derived from the code
        # itself rather than being the constant "scratch": two executions
        # running at once shared that single file, and the second write landed
        # while the first was still being read.
        #
        # Measured live: a model ran `premodel_audit.py` and `collinearity_probe.py`
        # in the same moment and BOTH died with `SyntaxError: unmatched ')'` at
        # the same line of scratch.py — two halves of two different programs.
        # Content-addressed means identical code still shares one file (so a
        # retry is idempotent) while different code never collides.
        eid = code_execution_input.execution_id or "scratch-" + hashlib.sha256(
            (code_execution_input.code or "").encode("utf-8")).hexdigest()[:12]
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
                backend, ws, tiers=_tiers_for(code_execution_input.code)
            )
            cmd = f"{shlex.quote(env.python)} {shlex.quote(rel_tmpfile)}"
            # NOTE: a skill script used to be run by `runpy` inside a wrapper
            # process, which does NOT put the script's own directory on
            # sys.path — sibling imports failed and were repaired here with an
            # `os.chdir` hook. The launcher now runs the script as a real
            # subprocess, and Python puts a script's directory at sys.path[0]
            # by itself, so the hook is gone rather than merely disabled.
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

        if getattr(res, "timed_out", False):
            # A timeout that produced nothing used to arrive as empty
            # stdout+stderr, which the skill launcher then reported as "no
            # output from the skill-script launcher" — pointing the reader at
            # the launcher instead of at the clock. Say what happened.
            note = (f"{NOTE_PREFIX} the script did not finish within "
                    f"{self.timeout_seconds}s and was terminated.")
            return CodeExecutionResult(
                stdout=res.stdout,
                stderr=(res.stderr + "\n" + note) if res.stderr else note,
            )
        return CodeExecutionResult(
            stdout=res.stdout,
            stderr=res.stderr,
        )
