"""Inject the active workspace directory into filesystem/exec tool descriptions.

The model often fumbles paths because nothing tells it where it is: tools
resolve relative paths against the workspace root and `display_path` shows
paths relative to it, but the model never sees that root. This plugin appends
the working directory to the FS/exec tools' declaration descriptions on
`before_model_callback` (the same per-request declaration-mutation seam
`ToolTitlePlugin` uses), so the model is told its working directory every turn.

Crucially it surfaces the IN-EXECUTION-CONTEXT path, not the host path: under a
sandbox the host workspace is mounted at a fixed root (docker/service →
`/workspace`, daytona → `/home/daytona`), which is what `pwd` returns and what
the model's absolute paths must match. The value comes from the active backend
(`SandboxBackend.container_cwd`) applied to the session's real workspace (state
or `ADK_CC_WORKSPACE_ROOT`); noop falls back to the host path.

Always attached; set `ADK_CC_DISABLE_WORKSPACE_HINT=1` to turn it off.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin

_log = logging.getLogger(__name__)

# Set by TenancyPlugin / sandbox layer; a WorkspaceRoot or its dict form.
_WS_STATE_KEY = "temp:sandbox_workspace"
# The active SandboxBackend, set alongside the workspace. Used to translate the
# host workspace path to the path the model actually sees inside its execution
# environment (e.g. /workspace under docker) — see SandboxBackend.container_cwd.
_BACKEND_STATE_KEY = "temp:sandbox_backend"

# Tools whose path args anchor at the workspace root (run_bash's cwd is the
# workspace too). MCP / skill tools are left alone — their paths aren't ours.
_PATH_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "glob_files", "grep",
    "run_bash", "save_as_artifact", "load_artifact_to_sandbox",
})

# Idempotency guard within a single request's declarations.
_MARKER = "Working directory:"


def _state_get(callback_context: CallbackContext, key: str):
    for holder in (
        getattr(callback_context, "state", None),
        getattr(getattr(callback_context, "session", None), "state", None),
    ):
        if holder is not None and hasattr(holder, "get"):
            try:
                val = holder.get(key)
            except Exception:
                val = None
            if val is not None:
                return val
    return None


def _workspace_path(callback_context: CallbackContext) -> str:
    """The active workspace abs path: session state if seeded, else
    ADK_CC_WORKSPACE_ROOT, else CWD (mirrors sandbox.default_workspace)."""
    raw = _state_get(callback_context, _WS_STATE_KEY)
    abs_path = None
    if raw is not None:
        abs_path = getattr(raw, "abs_path", None)
        if abs_path is None and isinstance(raw, dict):
            abs_path = raw.get("abs_path")
    if not abs_path:
        env = os.environ.get("ADK_CC_WORKSPACE_ROOT")
        abs_path = (
            os.path.abspath(os.path.expanduser(env)) if env else os.path.abspath(os.getcwd())
        )
    return abs_path


def _in_context_cwd(callback_context: CallbackContext) -> str:
    """The working directory as the MODEL sees it — the in-execution-context
    path, NOT the host path. Under a sandbox (docker/daytona/service) the host
    workspace is mounted at a fixed root (/workspace, /home/daytona), which is
    what `pwd` returns and what the model's absolute paths must match; under
    noop it's the host path. Falls back to the host path if no backend is seeded.
    """
    host = _workspace_path(callback_context)
    backend = _state_get(callback_context, _BACKEND_STATE_KEY)
    if backend is not None and hasattr(backend, "container_cwd"):
        try:
            return backend.container_cwd(host) or host
        except Exception:
            pass
    return host


class WorkspaceHintPlugin(BasePlugin):
    """Appends the workspace dir to FS/exec tool descriptions each turn."""

    def __init__(self, *, name: str = "adk_cc_workspace_hint") -> None:
        super().__init__(name=name)

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        if os.environ.get("ADK_CC_DISABLE_WORKSPACE_HINT") == "1":
            return None
        try:
            ws = _in_context_cwd(callback_context)
            hint = (
                f"\n\n{_MARKER} `{ws}` (this is what `pwd` returns where your "
                f"tools run). Paths resolve relative to it — prefer "
                f"workspace-relative paths (e.g. `src/app.py`); an absolute path "
                f"must be under this root."
            )
            for tool in (llm_request.config.tools or []):
                for decl in (getattr(tool, "function_declarations", None) or []):
                    if decl.name in _PATH_TOOLS:
                        desc = decl.description or ""
                        if _MARKER not in desc:
                            decl.description = desc + hint
        except Exception as e:  # noqa: BLE001 — a hint must never break a turn
            _log.warning("workspace_hint: skipped (%s: %s)", type(e).__name__, e)
        return None
