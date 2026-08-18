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
import logging
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
from .analysis_env import (AnalysisEnvError, ensure_env, forget,
                           required_tiers)
from .backends.base import SandboxBackend
from .workspace import WorkspaceRoot

_log = logging.getLogger(__name__)


# The skill-script launcher states the imports of the skill it is about to run,
# because the script's source is NOT in the code sent here — it is materialised
# in the workspace and run as a subprocess, and a warm run ships no source at
# all. Sizing the environment from what happens to be inline would provision
# `base` and the script would die on its first import.
_SKILL_TIERS_RE = re.compile(r"^# adk-cc-skill-tiers:(.*)$", re.M)
# #131: the skill wrapper stamps what it is about to run so the process
# panel can show "skill: data-analyst scripts/analyze.py" instead of a hash.
_SKILL_SCRIPT_RE = re.compile(r"^# adk-cc-skill-script: (.+)$", re.M)


def _label_from_code(code: str) -> str:
    m = _SKILL_SCRIPT_RE.search(code or "")
    if m:
        return ("skill: " + m.group(1).strip())[:60]
    return "code run " + hashlib.sha256(
        (code or "").encode("utf-8")).hexdigest()[:8]


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

        # ---- #131: this run is a PROCESS the user can see -----------------
        # Host-visible backends (noop runs on the host; docker/container
        # bind-mount the workspace) get the full treatment: the command's
        # output is redirected to a workspace file the panel tails LIVE, and
        # the command records its own pgid so Stop can kill the group. Other
        # backends still get a record + an end-of-run log — never a lying
        # Stop button.
        host_visible = getattr(backend, "name", "") in ("noop", "docker",
                                                        "container")
        rel_out = f".adk-cc/code/{eid}.out"
        rel_pid = f".adk-cc/code/{eid}.pid"
        rec_id = self._record_start(
            invocation_context, backend=backend, ws=ws,
            code=code_execution_input.code or "",
            log_path=(os.path.join(ws.abs_path, rel_out)
                      if host_visible else ""),
            pidfile_path=(os.path.join(ws.abs_path, rel_pid)
                          if getattr(backend, "name", "") == "noop" else ""),
        )

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
            tiers = _tiers_for(code_execution_input.code)
            env = await ensure_env(backend, ws, tiers=tiers)
            cmd = f"{shlex.quote(env.python)} {shlex.quote(rel_tmpfile)}"
            if host_visible:
                # `echo $$` then `exec`: the interpreter REPLACES the shell,
                # so the recorded pid IS the interpreter's — and under noop's
                # start_new_session it is the group leader, which is what
                # makes Stop a reliable group kill. stdout→.out (live tail),
                # stderr→.err (kept separate so the result is byte-identical
                # to the unredirected path).
                cmd = (f"echo $$ > {shlex.quote(rel_pid)}; exec {cmd} "
                       f"> {shlex.quote(rel_out)} 2> {shlex.quote(rel_out)}.err")
            self._mark_running(rec_id)
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
            # 127 is the shell's "command not found", which for an interpreter
            # PATH means the env is gone — `.adk-cc/analysis-env/bin/python: No
            # such file or directory`, reported live after a sandbox image
            # rebuild. `ensure_env` now detects that on disk, but its
            # in-process cache short-circuits the probe entirely, so a sandbox
            # REPLACED mid-session still hands back a dead interpreter.
            #
            # Retry from the observed failure rather than probing defensively
            # on every run: the happy path stays one round trip, and this
            # recovers regardless of WHY the interpreter vanished.
            if (getattr(res, "exit_code", 0) == 127 and env.is_managed
                    and not getattr(res, "timed_out", False)):
                dropped = forget(ws.abs_path)
                _log.warning(
                    "analysis env interpreter missing (exit 127); dropped %d "
                    "cached entr%s and rebuilding", dropped,
                    "y" if dropped == 1 else "ies")
                env = await ensure_env(backend, ws, tiers=tiers)
                retry_cmd = f"{shlex.quote(env.python)} {shlex.quote(rel_tmpfile)}"
                if host_visible:
                    retry_cmd = (
                        f"echo $$ > {shlex.quote(rel_pid)}; exec {retry_cmd} "
                        f"> {shlex.quote(rel_out)} 2> {shlex.quote(rel_out)}.err")
                res = await backend.exec(
                    retry_cmd,
                    fs_write=ws.fs_write_config(),
                    network=NetworkConfig(),
                    timeout_s=self.timeout_seconds,
                    cwd=ws.abs_path,
                )
        except AnalysisEnvError as e:
            # Actionable by construction — surface verbatim rather than as a
            # bare ModuleNotFoundError three steps later.
            self._finalize(rec_id, exit_code=None, failed=True, note=str(e))
            return CodeExecutionResult(stdout="", stderr=str(e))
        except Exception as e:  # noqa: BLE001 — surface as stderr
            self._finalize(rec_id, exit_code=None, failed=True,
                           note=f"{type(e).__name__}: {e}")
            return CodeExecutionResult(stdout="", stderr=f"{type(e).__name__}: {e}")

        stdout, stderr = res.stdout, res.stderr
        if host_visible:
            # The redirected files are the source of truth (buffered exec
            # captured nothing) — and on a timeout or Stop they hold the
            # PARTIAL output the buffered path would have lost.
            stdout = self._read_ws_file(ws.abs_path, rel_out)
            stderr = self._read_ws_file(ws.abs_path, rel_out + ".err")
        timed_out = bool(getattr(res, "timed_out", False))
        self._finalize(rec_id, exit_code=getattr(res, "exit_code", None),
                       timed_out=timed_out,
                       stderr_tail=stderr if host_visible else
                       (stdout + ("\n" + stderr if stderr else "")))

        if timed_out:
            # A timeout that produced nothing used to arrive as empty
            # stdout+stderr, which the skill launcher then reported as "no
            # output from the skill-script launcher" — pointing the reader at
            # the launcher instead of at the clock. Say what happened.
            note = (f"{NOTE_PREFIX} the script did not finish within "
                    f"{self.timeout_seconds}s and was terminated.")
            return CodeExecutionResult(
                stdout=stdout,
                stderr=(stderr + "\n" + note) if stderr else note,
            )
        return CodeExecutionResult(
            stdout=stdout,
            stderr=stderr,
        )

    # ---- #131 process-registry plumbing (all best-effort: visibility ----
    # ---- must never break an execution) ---------------------------------
    @staticmethod
    def _read_ws_file(ws_abs: str, rel: str) -> str:
        try:
            with open(os.path.join(ws_abs, rel), "r", encoding="utf-8",
                      errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def _record_start(self, invocation_context, *, backend, ws, code: str,
                      log_path: str, pidfile_path: str) -> Optional[str]:
        try:
            from .process_registry import get_registry

            sess = invocation_context.session
            label = _label_from_code(code)
            rec = get_registry().create(
                session_key=f"{sess.app_name}/{sess.user_id}/{sess.id}",
                project_id=str(sess.user_id or ""),
                label=label,
                command=label,
                cwd=ws.abs_path,
                backend=getattr(backend, "name", "") or type(backend).__name__,
                # Stop is only offered where terminate() can actually do it:
                # host kill by pgid, i.e. the noop backend with a pidfile.
                can_terminate=bool(pidfile_path),
                kind="foreground",
                log_path=log_path,
                pidfile_path=pidfile_path,
                timeout_s=self.timeout_seconds,
            )
            return rec.id
        except Exception as e:  # noqa: BLE001
            _log.debug("foreground record skipped: %s", e)
            return None

    def _mark_running(self, rec_id: Optional[str]) -> None:
        if not rec_id:
            return
        try:
            from .process_registry import get_registry

            get_registry().mark_running(rec_id)
        except Exception:  # noqa: BLE001
            pass

    def _finalize(self, rec_id: Optional[str], *, exit_code,
                  timed_out: bool = False, failed: bool = False,
                  note: str = "", stderr_tail: str = "") -> None:
        if not rec_id:
            return
        try:
            from .process_registry import get_registry

            reg = get_registry()
            rec = reg.get(rec_id)
            if rec is None:
                return
            # Non-host-visible backends buffered everything — write it now so
            # the record has SOME log; host-visible logs are already the live
            # workspace file, so only stderr (a separate file) is appended.
            if stderr_tail:
                if rec.log_path.startswith(str(reg.dir)):
                    reg.append_log(rec_id, stderr_tail.encode())
                elif rec.kind == "foreground" and stderr_tail.strip():
                    err_only = self._maybe_stderr_suffix(rec, stderr_tail)
                    if err_only:
                        reg.append_log(rec_id, err_only.encode())
            if note:
                reg.append_log(rec_id, ("\n" + note + "\n").encode())
            reg.finalize_foreground(
                rec_id, exit_code=exit_code,
                timed_out=timed_out or failed)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _maybe_stderr_suffix(rec, stderr_text: str) -> str:
        """Host-visible: the panel log IS the .out file; fold stderr under a
        divider so the log tells the whole story after the run."""
        return "\n--- stderr ---\n" + stderr_text
