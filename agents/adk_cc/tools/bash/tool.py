from __future__ import annotations

import re

import logging
import os
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

from ...branding import NOTE_PREFIX
from ...sandbox import get_backend, get_workspace
from ...sandbox.config import ExecChunk, ExecResult, NetworkConfig
from ..base import AdkCcTool, ToolMeta
from ..schemas import RunBashArgs
from .prompt import DESCRIPTION
from ...config.schema import env_bool

_log = logging.getLogger(__name__)


_INVOKES_PYTHON = re.compile(r"(?:^|[\s;&|(`$])(?:python3?|pip3?|uv\s+run)\b")


_MISSING_FILE_RE = re.compile(
    r"(can't open file|No such file or directory|not found|cannot find)", re.I
)
# A reference INTO the materialised skill-runtime cache (any prefix, absolute
# included — tracebacks print the absolute form and the model copies it). The
# digest dir changes on every skill edit and siblings are deleted, so a saved
# path is stale by design; the path itself names the skill and relative file,
# which is exactly what the redirect needs.
_RUNTIME_REF_RE = re.compile(
    r"\.(?:adk-cc|agents)/skill-runtime/(?P<skill>[\w.-]+)/[\w.-]+/"
    r"(?P<rel>[\w./-]+)")
_SCRIPTISH_RE: Optional[re.Pattern] = None


def _scriptish_re() -> re.Pattern:
    """Path-ish tokens that look like a script.

    Built from the skill launcher's own extension set rather than restating it,
    so adding an interpreter there extends this redirect too — they drifted the
    moment that set grew. Built lazily because the skills module is imported
    lazily here (import cycle), and cached because this runs on every failed
    command. Longest-first so `.mjs` never matches as `.js`.
    """
    global _SCRIPTISH_RE
    if _SCRIPTISH_RE is None:
        from ...tools.skills import launchable_script_exts

        alts = sorted((re.escape(e) for e in launchable_script_exts()),
                      key=lambda e: (-len(e), e))
        _SCRIPTISH_RE = re.compile(r"[\w./-]+\.(?:" + "|".join(alts) + r")\b")
    return _SCRIPTISH_RE


def _skill_script_hint(command: str, stderr: str) -> Optional[str]:
    """Explain a failed attempt to run a SKILL's script as a plain file.

    Skill files are not in the workspace — they are served through the skill
    tools — so `python scripts/premodel_audit.py …` fails with "can't open
    file". Observed live: the agent had read the skill's own README, which
    documents exactly that invocation, hit the failure, and silently fell back
    to writing its own analysis. Six vetted probe scripts shipped; none ran.

    Enriching the FAILURE rather than intercepting the command: a pre-flight
    block would have to guess, and this way a legitimate command keeps its real
    exit code and output.
    """
    if not _MISSING_FILE_RE.search(stderr or ""):
        return None
    from ...tools.skills import locate_skill_script

    # A stale materialised path (#113): `.adk-cc/skill-runtime/<skill>/
    # <digest>/scripts/x.py`, usually copied out of a traceback. The digest
    # dir it names is gone (only one exists at a time), but the path itself
    # says which skill and which file — redirect precisely, absolute or not.
    m = _RUNTIME_REF_RE.search(command or "")
    # Same-LINE check: a CURRENT digest path runs fine, and its script's own
    # traceback can also say "No such file" (about a data file) while naming
    # the runtime path in a File "…" frame. Only the script itself failing to
    # open puts its basename on the same line as the missing-file phrase.
    if m and any(
        _MISSING_FILE_RE.search(ln) and m.group("rel").rsplit("/", 1)[-1] in ln
        for ln in (stderr or "").splitlines()
    ):
        return (
            f"{NOTE_PREFIX} that path points into the skill runtime CACHE — a "
            f"materialised copy of the `{m.group('skill')}` skill whose "
            "location changes whenever the skill does, so it is never stable "
            "to address directly. Run the script through its skill instead:\n"
            f'  run_skill_script(skill_name="{m.group("skill")}", '
            f'file_path="{m.group("rel")}", args=["<arg>", ...])\n'
            "It runs with your workspace as its working directory."
        )

    for token in _scriptish_re().findall(command or ""):
        if token.startswith("/"):
            continue                       # absolute: not a skill-relative call
        found = locate_skill_script(token)
        if not found:
            continue
        skill, rel = found
        return (
            f"{NOTE_PREFIX} `{token}` belongs to the `{skill}` skill, and a skill's "
            "files are NOT in your workspace — no filesystem path reaches them. "
            "Run it through the skill tool instead:\n"
            f'  run_skill_script(skill_name="{skill}", file_path="{rel}", '
            'args=["<arg>", ...])\n'
            "It runs with your workspace as its working directory, so paths in "
            "the arguments work the same as they do here. Do not re-implement "
            "the script inline — it is shipped because its behaviour is the "
            "vetted one."
        )
    return None


def _session_key(ctx) -> str:
    """`app/user/session` — the same key shape the sub-agents registry uses."""
    try:
        sess = ctx._invocation_context.session
        return f"{sess.app_name}/{sess.user_id}/{sess.id}"
    except Exception:  # noqa: BLE001
        return "unknown"


def _project_id(ctx) -> str:
    """Desktop runs each project as its own user_id, so that IS the project.

    Background processes are listed per PROJECT rather than per session: a dev
    server started in one session is still what occupies the port in the next.
    """
    try:
        return str(ctx._invocation_context.session.user_id or "")
    except Exception:  # noqa: BLE001
        return ""


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

    async def _start_background(self, backend, ws, args: RunBashArgs,
                                ctx: ToolContext) -> dict[str, Any]:
        """Hand off to the backend's detached-start path and report the record.

        A backend that cannot detach (or cannot signal afterwards) says so
        rather than silently running the command in the foreground — a dev
        server started "in the background" that actually blocks the turn would
        be the worst of both."""
        if not getattr(backend, "supports_background", False):
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": (
                    f"the active sandbox backend ({type(backend).__name__}) "
                    "cannot run background processes; run the command in the "
                    "foreground with a timeout instead"),
                "timed_out": False,
                "error_code": "BACKGROUND_UNSUPPORTED",
            }
        rec = await backend.start_background(
            args.command,
            fs_write=ws.fs_write_config(),
            network=NetworkConfig(),
            cwd=ws.abs_path,
            label=args.label or "",
            session_key=_session_key(ctx),
            project_id=_project_id(ctx),
        )
        from ...sandbox.process_registry import get_registry

        log_tail = get_registry().read_log(rec["id"], tail_bytes=2000)
        port = rec.get("port")
        return {
            "exit_code": 0,
            "stdout": (
                f"started background process {rec['id']} ({rec['label']})"
                + (f" on port {port}" if port else "")
                + (f"\n--- first output ---\n{log_tail}" if log_tail else "")
            ),
            "stderr": "",
            "timed_out": False,
            "process": {
                "id": rec["id"], "label": rec["label"], "status": rec["status"],
                "pid": rec.get("pid"), "port": port,
            },
            "next": ("it keeps running after this turn — read_process_log(id) "
                     "for output, stop_process(id) to end it"),
        }

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
        # Background: a long-lived process (dev server, watcher). Branches
        # BEFORE the exec paths because it does not produce an ExecResult —
        # nothing waits for it, and its output goes to a log file the UI and
        # `read_process_log` read later.
        if getattr(args, "background", False):
            return await self._start_background(backend, ws, args, ctx)

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
        if result.exit_code != 0:
            hint = _skill_script_hint(args.command, stderr)
            if hint:
                stderr = hint + "\n\n" + stderr
        if self._env_note and result.exit_code != 0:
            # The command failed AND the managed interpreter was unavailable —
            # almost certainly the cause. Say so instead of leaving the model
            # to guess at `python: command not found`.
            stderr = (
                f"{NOTE_PREFIX} the managed Python environment could not be "
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
