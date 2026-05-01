"""Persist the Plan sub-agent's plan as an artifact.

Mirrors upstream Claude Code's plan-file pattern (`utils/plans.ts`):
the plan lives on disk, its path is recorded in session state, and
other agents (coordinator, verification, future iterations of Plan
itself) discover it via state.

Filenames are time-ordered + slug-named so multiple plans across a
session don't overwrite each other:

    <workspace>/.adk-cc/plans/<UTC-timestamp>-<slug>.md
    e.g.  .adk-cc/plans/20260501T123045-auth-refactor.md

State keys written:
    current_plan_path      absolute path to the latest plan file
    current_plan_title     first `# heading` of the latest plan (display label)
    plan_history           list of every plan written this session, oldest first,
                           each entry: {path, title, slug, written_at}

Why this tool is `is_read_only=True` even though it writes a file:
the artifact is part of the planning role, not a project mutation. The
file still goes through the sandbox backend's `write_text`, so the
workspace fs_write_config governs where it can land.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
from .base import AdkCcTool, ToolMeta
from .schemas import WritePlanArgs

_PLANS_DIR = ".adk-cc/plans"

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM = re.compile(r"^-+|-+$")


def _extract_title(content: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled plan"


def _slugify(text: str, *, max_len: int = 40) -> str:
    slug = _SLUG_NON_ALNUM.sub("-", text.lower())
    slug = _SLUG_TRIM.sub("", slug)
    return (slug[:max_len] or "plan").strip("-") or "plan"


def _timestamp() -> str:
    # Compact ISO-8601 (no colons — friendlier to filesystems and globs).
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class WritePlanTool(AdkCcTool):
    meta = ToolMeta(
        name="write_plan",
        is_read_only=True,  # planning artifact; see module docstring
        is_concurrency_safe=False,
    )
    input_model = WritePlanArgs
    description = (
        "Persist a plan as a Markdown file under the workspace. Each call "
        "creates a new file (timestamp + slug), so plan history grows over "
        "the session. Sets `current_plan_path` and `current_plan_title` in "
        "session state, and appends to `plan_history`. Other agents read "
        "the latest via `read_current_plan`."
    )

    async def _execute(self, args: WritePlanArgs, ctx: ToolContext) -> dict[str, Any]:
        ws = get_workspace(ctx)
        backend = get_backend(ctx)

        title = _extract_title(args.content)
        slug = _slugify(args.slug or title)
        filename = f"{_timestamp()}-{slug}.md"
        plan_path = str(Path(ws.abs_path) / _PLANS_DIR / filename)

        try:
            await backend.write_text(
                plan_path, args.content, fs_write=ws.fs_write_config()
            )
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}

        # Update state: current pointers + append to history.
        try:
            entry = {
                "path": plan_path,
                "title": title,
                "slug": slug,
                "written_at": datetime.now(timezone.utc).isoformat(),
            }
            ctx.state["current_plan_path"] = plan_path
            ctx.state["current_plan_title"] = title
            history = ctx.state.get("plan_history") or []
            history.append(entry)
            ctx.state["plan_history"] = history
        except Exception:
            pass

        return {
            "status": "ok",
            "path": plan_path,
            "title": title,
            "slug": slug,
            "bytes": len(args.content.encode("utf-8")),
        }
