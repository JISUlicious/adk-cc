"""Set a human-readable session title for the UI session rail.

Companion to the tool-call titles feature (plugins/tool_title.py): tool calls
get per-call labels; this gives the SESSION a label, so the rail shows
"Fizzbuzz script demo" instead of a bare session id. The title is written to
`session.state["session_title"]`, which ADK persists via the event's
state_delta and returns in the sessions LIST response — exactly where the
frontend rail reads it.

The model is nudged (via the ToolTitlePlugin guidance) to call this once at
the start of a session and update it only if the topic shifts substantially.
Overwriting is allowed by design. Display-only: nothing gates on the value.

NOTE: the arg is deliberately named `title` — ToolTitlePlugin treats a native
`title` arg as the tool's own (no injection, no strip), which is precisely the
semantics here.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta

_STATE_KEY = "session_title"
_MAX_LEN = 80


class SetSessionTitleArgs(BaseModel):
    title: str = Field(
        description=(
            "Short session label for the UI session list (2-6 words, e.g. "
            "'Fizzbuzz script demo'). Not a sentence; no trailing period."
        )
    )


class SetSessionTitleTool(AdkCcTool):
    meta = ToolMeta(
        name="set_session_title",
        is_read_only=True,  # mutates UI-only state, not the workspace
        is_concurrency_safe=False,
        requires_user_approval=False,
    )
    input_model = SetSessionTitleArgs
    description = (
        "Sets this session's display title, shown in the UI session list. "
        "Call once near the start of a session with a 2-6 word label; call "
        "again only if the session's topic changes substantially."
    )

    async def _execute(
        self, args: SetSessionTitleArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        text = " ".join(args.title.split()).strip()
        if not text:
            return {"status": "error", "error": "title must be non-empty"}
        if len(text) > _MAX_LEN:
            text = text[: _MAX_LEN - 1].rstrip() + "…"
        try:
            previous = ctx.state.get(_STATE_KEY)
        except Exception:
            previous = None
        try:
            ctx.state[_STATE_KEY] = text
        except Exception as e:
            return {"status": "error", "error": f"could not update state: {e}"}
        return {"status": "ok", "session_title": text, "previous": previous}
