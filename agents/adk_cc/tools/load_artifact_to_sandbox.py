"""Copy an artifact's bytes into the sandbox workspace.

The reverse of `save_as_artifact`. Pulls a stored artifact (a user
upload, or anything previously published) out of ADK's artifact service
and materializes it as a file inside the agent's sandbox, so the agent
can read / run / process it with the normal file + bash tools.

Snapshot semantics: this is a one-shot byte copy at a chosen version.
The sandbox file and the artifact have NO ongoing link afterward —
editing the sandbox copy doesn't change the artifact, and saving a new
artifact version doesn't change an already-written sandbox file. Pass
`version` to pin a specific revision (default: latest).

`is_read_only=False`: it writes into the workspace (a real filesystem
mutation), unlike `save_as_artifact` which only reads from it. The
workspace `fs_write_config` still gates which paths may be written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import SandboxViolation, get_backend, get_workspace
from ._fs import display_path, resolve
from .base import AdkCcTool, ToolMeta
from .schemas import LoadArtifactToSandboxArgs


class LoadArtifactToSandboxTool(AdkCcTool):
    meta = ToolMeta(
        name="load_artifact_to_sandbox",
        is_read_only=False,  # writes a file into the workspace
        is_concurrency_safe=False,
    )
    input_model = LoadArtifactToSandboxArgs
    description = (
        "Copy a stored artifact (a user-uploaded file, or one previously "
        "saved with save_as_artifact) into the sandbox so you can read, "
        "run, or process it. Writes a point-in-time copy at the given "
        "version (default latest) to `dest_path`. Refuses to overwrite an "
        "existing file unless `overwrite` is true. The copy is independent "
        "of the artifact afterward (editing one doesn't change the other)."
    )

    async def _execute(
        self, args: LoadArtifactToSandboxArgs, ctx: ToolContext
    ) -> dict[str, Any]:
        ws = get_workspace(ctx)
        backend = get_backend(ctx)

        scope = (args.scope or "session").lower()
        if scope not in ("session", "user"):
            return {
                "status": "error",
                "error": f"unknown scope {args.scope!r}; valid: session|user",
            }

        # Resolve dest the same way write_file does — relative anchors at
        # the workspace root, absolute is taken verbatim, and the result
        # passes the backend's allow-check.
        dest = resolve(args.dest_path, ctx)

        # Load the artifact Part. ctx.load_artifact is session-scoped;
        # for user scope go through the artifact_service directly with
        # session_id=None (mirrors save_as_artifact's two-scope split).
        try:
            if scope == "session":
                part = await ctx.load_artifact(args.filename, version=args.version)
            else:
                ic = getattr(ctx, "_invocation_context", None)
                artifact_service = (
                    getattr(ic, "artifact_service", None)
                    if ic is not None
                    else None
                )
                if artifact_service is None:
                    return {
                        "status": "error",
                        "error": (
                            "artifact_service is not configured on this "
                            "runtime — set ADK_CC_ARTIFACT_STORAGE_URI"
                        ),
                    }
                part = await artifact_service.load_artifact(
                    app_name=ic.app_name,
                    user_id=ic.user_id,
                    session_id=None,
                    filename=args.filename,
                    version=args.version,
                )
        except Exception as e:  # noqa: BLE001 — surface as tool result
            return {
                "status": "error",
                "error": f"load_artifact failed for {args.filename!r}: {e}",
            }

        if part is None or getattr(part, "inline_data", None) is None:
            return {
                "status": "not_found",
                "error": (
                    f"artifact {args.filename!r}"
                    + (f" v{args.version}" if args.version is not None else "")
                    + f" not found in {scope} scope"
                ),
            }

        data = part.inline_data.data
        mime = part.inline_data.mime_type

        # Clobber guard: refuse to overwrite unless asked. Best-effort —
        # if the existence check itself errors (backend hiccup), fall
        # through to the write and let it surface any real failure.
        if not args.overwrite:
            try:
                existing = await backend.read_bytes(
                    str(dest), fs_read=ws.fs_read_config()
                )
            except FileNotFoundError:
                existing = None
            except Exception:  # noqa: BLE001 — don't block on a flaky probe
                existing = None
            if existing is not None:
                return {
                    "status": "exists",
                    "error": (
                        f"{display_path(dest, ctx)} already exists; pass "
                        f"overwrite=true to replace it"
                    ),
                }

        try:
            await backend.write_bytes(
                str(dest), data, fs_write=ws.fs_write_config()
            )
        except SandboxViolation as e:
            return {"status": "sandbox_denied", "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "error": f"write_bytes failed for {args.dest_path!r}: {e}",
            }

        return {
            "status": "ok",
            "filename": args.filename,
            "dest_path": display_path(dest, ctx),
            "version": args.version,
            "scope": scope,
            "bytes": len(data),
            "mime_type": mime,
        }
