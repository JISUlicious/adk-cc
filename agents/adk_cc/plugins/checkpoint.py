"""Checkpoint plugin — an undo net for in-place desktop edits.

Desktop mode edits the user's real project files in place (see
``service/desktop_workspace``). To make that safe, this plugin snapshots the
project's working tree into a SEPARATE shadow git repo BEFORE the first mutating
tool of each turn, so any turn can be undone (POST ``/desktop/checkpoint/restore``
→ "Undo last turn" in the UI). The shadow store never touches the user's real
``.git``; see ``service/desktop_checkpoint`` for the mechanics.

Registered AFTER Permission / AuthZ / Quota in the plugin chain, so a denied or
throttled tool call — whose ``before_tool_callback`` is short-circuited by those
plugins — never triggers a snapshot; and AFTER Tenancy, so the per-session
workspace is already seeded in state. On by default for desktop + a bound
project; ``ADK_CC_CHECKPOINT=0`` disables it, and it is inert in web mode.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

_log = logging.getLogger(__name__)

# The mutating tools worth snapshotting before. Read-only tools (read_file, grep,
# glob, web_fetch) can't change the tree, so they never trigger a checkpoint.
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "run_bash"})


class CheckpointPlugin(BasePlugin):
    def __init__(self, *, name: str = "adk_cc_checkpoint") -> None:
        super().__init__(name=name)

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        # Never gate the call — this is an observer. Returning None lets the tool
        # proceed regardless of whether the snapshot succeeds.
        try:
            self._maybe_snapshot(tool, tool_context)
        except Exception as e:  # noqa: BLE001 — a checkpoint must never block a tool
            _log.debug("checkpoint before_tool error: %s", e)
        return None

    def _maybe_snapshot(self, tool: BaseTool, ctx: ToolContext) -> None:
        # Cheapest gates first — before_tool_callback runs on EVERY tool call, and
        # read-only tools (the majority) must bail before any import work.
        if tool.name not in _MUTATING_TOOLS:
            return

        # Deferred import: keeps desktop-only git plumbing out of the web path's
        # import graph, and avoids a cycle through service/*.
        from ..service.desktop_checkpoint import enabled, snapshot
        from ..service.desktop_routes import project_repo_path

        if not enabled():  # web mode, or ADK_CC_CHECKPOINT=0
            return

        state = ctx.state
        ws = state.get("temp:sandbox_workspace")
        root = getattr(ws, "abs_path", None)
        if not root:
            return

        # In desktop, user_id == the project id. Only snapshot a BOUND project
        # repo (the in-place root) — never the no-project scratch dir, which has
        # nothing project-scoped to undo.
        project_id = getattr(ctx, "user_id", None) or ""
        repo = project_repo_path(project_id)
        if not repo or os.path.realpath(repo) != os.path.realpath(root):
            return

        session_id = None
        sess = getattr(ctx, "session", None)
        if sess is not None:
            session_id = getattr(sess, "id", None)
        session_id = session_id or "local"

        # Once per turn: the first mutating tool of an invocation snapshots the
        # pre-turn state; later tools in the same turn reuse it. Mirrors
        # TenancyPlugin's _WS_ENSURED_KEY guard. Set the flag BEFORE snapshotting
        # so exactly one attempt runs per turn even if the snapshot fails.
        inv = getattr(ctx, "invocation_id", None) or "inv"
        guard = f"temp:checkpoint_taken_{inv}"
        if state.get(guard):
            return
        state[guard] = True

        snapshot(project_id, session_id, root, reason=tool.name)
