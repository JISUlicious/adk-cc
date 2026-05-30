"""Shared helper: persist a `types.Part` into ADK's artifact service.

Extracted from `save_as_artifact.py` so multiple callers (the sandbox
publish tool, the MCP-resource save tool, the auto-persist plugin) share
one implementation of the scope branch + artifact-service plumbing.

The caller builds the `types.Part` (each source owns its own bytes →
Part construction); this helper validates the scope, locates the
artifact service on the runtime, saves under the right scope, and
returns the canonical result dict.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import types


async def save_part_as_artifact(
    ctx: ToolContext, *, filename: str, part: types.Part, scope: str
) -> dict[str, Any]:
    """Persist a pre-built Part into the artifact service.

    Args:
      ctx: the tool/callback context (must expose `_invocation_context`
        with `artifact_service`, and an async `save_artifact`).
      filename: artifact filename / storage key. ADK auto-versions saves
        of the same filename.
      part: a `types.Part` carrying `inline_data` (Blob with data +
        mime_type). The `bytes`/`mime_type` in the result are read back
        from here.
      scope: `"session"` (default elsewhere) ties the artifact to the
        current session and records `event.actions.artifactDelta` so the
        UI shows a download chip; `"user"` persists across the user's
        sessions (saved with `session_id=None`, no per-event chip).

    Returns:
      `{"status":"ok","filename","version","scope","bytes","mime_type"}`
      on success, or `{"status":"error","error":...}` for an unknown
      scope, a missing artifact service, or a save failure. Never raises.
    """
    scope = (scope or "session").lower()
    if scope not in ("session", "user"):
        return {
            "status": "error",
            "error": f"unknown scope {scope!r}; valid: session|user",
        }

    inline = getattr(part, "inline_data", None)
    data = getattr(inline, "data", None) if inline is not None else None
    mime = getattr(inline, "mime_type", None) if inline is not None else None
    if data is None:
        return {
            "status": "error",
            "error": "part has no inline_data.data to save",
        }

    ic = getattr(ctx, "_invocation_context", None)
    artifact_service = (
        getattr(ic, "artifact_service", None) if ic is not None else None
    )
    if artifact_service is None:
        return {
            "status": "error",
            "error": (
                "artifact_service is not configured on this runtime — "
                "set ADK_CC_ARTIFACT_STORAGE_URI or wire a "
                "BaseArtifactService into get_fast_api_app()"
            ),
        }

    try:
        if scope == "session":
            # ctx.save_artifact also records artifact_delta on the event
            # so the UI sees it via the SSE stream.
            version = await ctx.save_artifact(filename, part)
        else:
            # session_id=None → user-scoped. No artifact_delta side-
            # effect; the UI surfaces user-scoped artifacts via a future
            # "library" view, not the per-event chip.
            version = await artifact_service.save_artifact(
                app_name=ic.app_name,
                user_id=ic.user_id,
                session_id=None,
                filename=filename,
                artifact=part,
            )
    except Exception as e:  # noqa: BLE001 — surface as tool result
        return {
            "status": "error",
            "error": f"save_artifact failed for {filename!r}: {e}",
        }

    return {
        "status": "ok",
        "filename": filename,
        "version": version,
        "scope": scope,
        "bytes": len(data),
        "mime_type": mime,
    }
