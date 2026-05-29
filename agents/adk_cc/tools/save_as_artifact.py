"""Publish a sandbox file as an ADK artifact.

`write_file` lands content inside the per-session sandbox volume.
That's enough for the agent to read it again on a later turn, but
the chat UI has no first-class way to download it — and the sandbox
volume is wiped per `Limits.hard_destroy_ttl_s` (Daytona /
SandboxService) or when the session ends (Docker).

`save_as_artifact` bridges the two stores: read the bytes from the
sandbox, save into ADK's artifact service. The service mirrors what
the UI consumes via `event.actions.artifactDelta` and serves over
the existing `/apps/{app}/users/{u}/sessions/{s}/artifacts/...`
REST surface, so the UI can render a download chip.

Two scopes:
  - `session` (default): artifact lives with the current session;
    cleaned with the session.
  - `user`: artifact persists across this user's sessions (saved
    with `session_id=None`).

The agent reads bytes (not text) from the sandbox so binary
artifacts (PDFs, images, zips) survive without utf-8 corruption.
The MIME type is guessed from the filename extension; falls back
to `application/octet-stream`.

This tool is `is_read_only=True` because it doesn't mutate the
project — it only reads from the sandbox and writes into a
separate artifact store. The fs_read_config of the active
workspace still gates which sandbox paths can be published.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import types

from ..sandbox import SandboxViolation, get_backend, get_workspace
from ._fs import resolve
from .base import AdkCcTool, ToolMeta
from .schemas import SaveAsArtifactArgs


class SaveAsArtifactTool(AdkCcTool):
    meta = ToolMeta(
        name="save_as_artifact",
        is_read_only=True,  # see module docstring
        is_concurrency_safe=False,
    )
    input_model = SaveAsArtifactArgs
    description = (
        "Publish a file from the sandbox as a downloadable artifact in "
        "the chat UI. Use when the user explicitly asks to download "
        "what you produced, or when a generated file (report, chart, "
        "image, zip) needs to outlive the sandbox volume. "
        "Binary-safe — reads the raw bytes from the sandbox and stores "
        "them with a MIME type guessed from the filename. "
        "Scope `session` (default) ties the artifact to this session; "
        "scope `user` persists across this user's future sessions."
    )

    async def _execute(
        self, args: SaveAsArtifactArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        ws = get_workspace(ctx)
        backend = get_backend(ctx)

        # Resolve the path the same way read_file / write_file do, so a
        # workspace-relative path (`hello.py`) anchors at the workspace
        # root and passes the backend's allow-check. Without this, a
        # relative path slips through unanchored and an absolute path is
        # taken verbatim — both common failure modes the other file
        # tools avoid by resolving first.
        p = resolve(args.path, ctx)

        filename = args.filename or Path(args.path).name
        if not filename:
            return {
                "status": "error",
                "error": (
                    "could not derive a filename from "
                    f"path={args.path!r}; pass `filename` explicitly"
                ),
            }

        scope = (args.scope or "session").lower()
        if scope not in ("session", "user"):
            return {
                "status": "error",
                "error": f"unknown scope {args.scope!r}; valid: session|user",
            }

        # Read raw bytes — never text. The default ABC impl round-trips
        # via read_text/utf-8 for backends that haven't overridden;
        # DaytonaBackend's override skips the decode so PDFs etc.
        # survive.
        try:
            raw = await backend.read_bytes(str(p), fs_read=ws.fs_read_config())
        except FileNotFoundError:
            return {
                "status": "not_found",
                "error": f"file not found in sandbox: {args.path}",
            }
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        except Exception as e:  # noqa: BLE001 — surface as tool result
            return {
                "status": "error",
                "error": f"read_bytes failed for {args.path!r}: {e}",
            }

        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        part = types.Part(inline_data=types.Blob(data=raw, mime_type=mime))

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
                # ctx.save_artifact also records artifact_delta on the
                # event so the UI sees it via the SSE stream.
                version = await ctx.save_artifact(filename, part)
            else:
                # session_id=None → user-scoped. We don't get the
                # artifact_delta side-effect here; the UI surfaces user-
                # scoped artifacts via a future "library" view, not via
                # the per-event chip.
                version = await artifact_service.save_artifact(
                    app_name=ic.app_name,
                    user_id=ic.user_id,
                    session_id=None,
                    filename=filename,
                    artifact=part,
                )
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "error": f"save_artifact failed for {filename!r}: {e}",
            }

        return {
            "status": "ok",
            "filename": filename,
            "version": version,
            "scope": scope,
            "bytes": len(raw),
            "mime_type": mime,
        }
