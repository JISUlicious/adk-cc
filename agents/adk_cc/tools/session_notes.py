"""Session notes: the curated SESSION-scope knowledge surface (#127).

Doctrine (analysis/session-scope-plan.md): session -> NOTES (explicit,
dies with the session), user -> MEMORY, team -> WIKI. Notes live in
SESSION STATE (key "session_notes") — the one store with exactly the
session's lifetime — and NotesPlugin re-injects them into every request's
system instruction, which is what makes them COMPACTION-IMMUNE: injected
blocks are re-materialized from state each turn, never stored as events,
so the summarizer cannot eat them.
"""
from __future__ import annotations

import os
from typing import Any, Literal, Optional

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .base import AdkCcTool, ToolMeta

STATE_KEY = "session_notes"


def notes_budget_chars() -> int:
    """~4 chars/token over ADK_CC_SESSION_NOTES_BUDGET (default 2000 tokens)."""
    try:
        tokens = int(os.environ.get("ADK_CC_SESSION_NOTES_BUDGET", "2000"))
    except ValueError:
        tokens = 2000
    return max(200, tokens) * 4


class UpdateSessionNotesArgs(BaseModel):
    content: str = Field(description="Markdown note content.")
    mode: Literal["append", "replace"] = Field(
        default="append",
        description="append adds a new entry below existing notes; "
                    "replace rewrites them entirely.")


class UpdateSessionNotesTool(AdkCcTool):
    meta = ToolMeta(
        name="update_session_notes",
        is_read_only=False,
        is_concurrency_safe=False,
    )
    input_model = UpdateSessionNotesArgs
    description = (
        "Record working notes for THIS session: decisions made, discovered "
        "constraints/gotchas, current task state. Notes are re-shown to you "
        "every turn and SURVIVE context compaction — write down anything "
        "that must not be forgotten in a long session. They die with the "
        "session: durable facts about the user belong in memory, shared "
        "knowledge in the wiki (wiki_add). Keep notes concise; the oldest "
        "lines are trimmed when the budget is exceeded."
    )

    async def _execute(
        self, args: UpdateSessionNotesArgs, ctx: ToolContext
    ) -> Optional[dict[str, Any]]:
        try:
            existing = str(ctx.state.get(STATE_KEY) or "")
        except Exception:  # noqa: BLE001
            existing = ""
        if args.mode == "replace" or not existing:
            merged = args.content.strip()
        else:
            merged = existing.rstrip() + "\n\n" + args.content.strip()
        cap = notes_budget_chars()
        trimmed = False
        if len(merged) > cap:
            # Oldest-first trim: keep the newest tail, on a line boundary.
            merged = merged[-cap:]
            nl = merged.find("\n")
            if 0 <= nl < 200:
                merged = merged[nl + 1:]
            trimmed = True
        try:
            ctx.state[STATE_KEY] = merged
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": f"could not update state: {e}"}
        return {"status": "ok", "chars": len(merged), "trimmed": trimmed}
