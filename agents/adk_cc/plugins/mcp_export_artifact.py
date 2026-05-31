"""Auto-persist files returned by MCP tool calls as artifacts (Pattern C).

ADK's `McpTool` returns a tool result as a plain `response.model_dump()`
dict — so when an MCP tool returns a file (an embedded resource, or a
`resource_link` to an export), the bytes either bloat the model's context
(embedded) or are never persisted (link). This plugin closes that gap:
after any `mcp__*` tool call, it detects file-bearing content blocks and
saves them to the artifact store (S3/MinIO), so DB-export-style tools
"just work" with a downloadable artifact and no agent decision.

Enabled by DEFAULT — set `ADK_CC_MCP_AUTOSAVE_EXPORTS=0` to turn it off.
When disabled the per-call cost is a single attribute check (the gate at
the top of after_tool_callback), so it's cheap to leave on. The audience
filter (default on) keeps it from sweeping up the model's own working
blobs. Registered in `App.plugins` (agent.py).

Handled content:
  - `EmbeddedResource` (inline text/blob) → saved; the inline bytes are
    STRIPPED from the returned result (they're already persisted) to keep
    them out of the model's context.
  - `ResourceLink` → saved by URI scheme, link left in the result
    augmented with the artifact ref:
      * `https://` — client-fetchable per spec → httpx GET.
      * `s3://`    — bytes already in object storage; register-by-reference
        isn't wired yet, so it's skipped + logged (NOT faked as an
        artifact).
      * `mcp://` / `file://` / custom — per the spec these mean "read it
        back through the server", so we do a raw-URI read on the PRODUCING
        tool's MCP session (`session.read_resource(uri=...)`), which works
        even for URIs not advertised in `resources/list`.

Never raises — a failed autosave logs a warning and leaves the tool
result unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..tools._artifact import save_part_as_artifact
from ..tools._mcp_content import mcp_content_to_part, safe_artifact_name

_log = logging.getLogger(__name__)


class McpExportArtifactPlugin(BasePlugin):
    """Auto-saves file-bearing MCP tool-result content as artifacts."""

    def __init__(self, name: str = "adk_cc_mcp_export_artifact") -> None:
        super().__init__(name=name)
        self._enabled = os.environ.get("ADK_CC_MCP_AUTOSAVE_EXPORTS", "1") != "0"
        # Default ON: only auto-save content meant for the user (audience
        # includes "user"), so the model's own working blobs aren't swept up.
        self._user_only = (
            os.environ.get("ADK_CC_MCP_AUTOSAVE_AUDIENCE_USER_ONLY", "1") != "0"
        )

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        # --- cheap gates (this runs after EVERY tool call) ---
        if not self._enabled:
            return None
        name = getattr(tool, "name", "") or ""
        if not name.startswith("mcp__"):
            return None
        if not isinstance(result, dict):
            return None

        try:
            from mcp.types import (
                CallToolResult,
                EmbeddedResource,
                ResourceLink,
            )

            parsed = CallToolResult.model_validate(result)
        except Exception:  # noqa: BLE001 — not an MCP tool result we recognize
            return None

        content = parsed.content or []
        has_target = any(
            isinstance(c, (EmbeddedResource, ResourceLink)) and self._wanted(c)
            for c in content
        )
        if not has_target:
            return None

        saved: list[dict] = []
        strip_indices: set[int] = set()  # embedded-resource items to drop
        for idx, c in enumerate(content):
            if not (
                isinstance(c, (EmbeddedResource, ResourceLink)) and self._wanted(c)
            ):
                continue
            try:
                if isinstance(c, EmbeddedResource):
                    part = mcp_content_to_part(c.resource)
                    if part is None:
                        continue
                    fname = safe_artifact_name(
                        str(getattr(c.resource, "uri", None) or f"{name}_embedded")
                    )
                    res = await save_part_as_artifact(
                        tool_context, filename=fname, part=part, scope="session"
                    )
                    if res.get("status") == "ok":
                        saved.append(res)
                        strip_indices.add(idx)
                else:  # ResourceLink
                    res = await self._save_link(c, tool_context, tool)
                    if res is not None and res.get("status") == "ok":
                        saved.append(res)
            except Exception as e:  # noqa: BLE001 — never break the tool chain
                _log.warning(
                    "mcp_export_artifact: failed to persist content from %s: %s",
                    name,
                    e,
                )

        if not saved:
            return None

        # Build the augmented/stripped result. result is a plain dict from
        # model_dump; mutate a copy so we don't disturb other consumers.
        new_result = dict(result)
        artifacts_note = [
            {"filename": s["filename"], "version": s["version"], "bytes": s.get("bytes")}
            for s in saved
        ]
        new_result["_artifacts"] = artifacts_note
        if strip_indices:
            # Drop the inline bytes of saved embedded resources from the
            # content array so they don't sit in the model's context.
            kept = [
                item
                for i, item in enumerate(new_result.get("content", []))
                if i not in strip_indices
            ]
            new_result["content"] = kept
            new_result["_note"] = (
                "embedded resource(s) saved as artifact; inline bytes removed"
            )
        return new_result

    def _wanted(self, content: Any) -> bool:
        """Audience filter: when user_only, require audience to include 'user'."""
        if not self._user_only:
            return True
        ann = getattr(content, "annotations", None)
        # EmbeddedResource may carry annotations on the inner resource too.
        if ann is None:
            inner = getattr(content, "resource", None)
            ann = getattr(inner, "annotations", None) if inner is not None else None
        audience = getattr(ann, "audience", None) if ann is not None else None
        return bool(audience) and "user" in audience

    async def _save_link(
        self, link: Any, tool_context: ToolContext, tool: Any
    ) -> Optional[dict]:
        """Persist a ResourceLink by URI scheme.

        - http/https : client-fetchable per spec → httpx GET.
        - s3         : already in object storage → record a reference.
        - everything else (mcp/file/custom) : per the MCP spec these are
          "read it back through the server" URIs → raw-URI read on the
          PRODUCING tool's MCP session (`session.read_resource(uri=...)`),
          which works even for URIs not in resources/list.
        """
        uri = str(getattr(link, "uri", "") or "")
        scheme = urlparse(uri).scheme.lower()
        fname = safe_artifact_name(getattr(link, "name", None) or uri)
        mime = getattr(link, "mimeType", None) or "application/octet-stream"

        if scheme in ("http", "https"):
            try:
                import httpx
                from google.genai import types

                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(uri)
                    resp.raise_for_status()
                    data = resp.content
                part = types.Part(inline_data=types.Blob(data=data, mime_type=mime))
                return await save_part_as_artifact(
                    tool_context, filename=fname, part=part, scope="session"
                )
            except Exception as e:  # noqa: BLE001
                _log.warning("mcp_export_artifact: https fetch failed for %s: %s", uri, e)
                return None

        if scheme == "s3":
            # The bytes already live in object storage. Registering an
            # existing object as an artifact by reference (no re-copy) isn't
            # wired yet, so we DON'T fake an artifact entry — we skip and
            # log. (A real impl would register the canonical s3:// uri with
            # the artifact index.) Returning None leaves the link in the
            # result untouched; nothing claims to be downloadable that isn't.
            _log.info(
                "mcp_export_artifact: s3 resource_link not auto-persisted "
                "(register-by-reference not implemented): %s", uri,
            )
            return None

        # mcp:// / file:// / custom scheme → read it back through the
        # producing tool's MCP session. The tool ADK passed us IS the
        # McpTool that returned this link, so it carries the session
        # manager bound to the right server. (External schemes like s3://
        # are handled above; only server-resolved schemes reach here.)
        return await self._save_via_session(uri, fname, tool, tool_context)

    async def _save_via_session(
        self, uri: str, fname: str, tool: Any, tool_context: ToolContext
    ) -> Optional[dict]:
        """Raw-URI read on the producing McpTool's session → artifact."""
        mgr = getattr(tool, "_mcp_session_manager", None)
        if mgr is None:
            _log.info(
                "mcp_export_artifact: cannot read %s — tool %r exposes no MCP "
                "session manager", uri, getattr(tool, "name", "?"),
            )
            return None
        try:
            from pydantic import AnyUrl

            session = await mgr.create_session()
            result = await session.read_resource(uri=AnyUrl(uri))
            contents = getattr(result, "contents", None) or []
        except Exception as e:  # noqa: BLE001
            _log.warning("mcp_export_artifact: read_resource(%s) failed: %s", uri, e)
            return None

        # A resource may return multiple contents; save the first usable one
        # (the link names a single file). mcp_content_to_part handles
        # text/blob; unsupported → None.
        for content in contents:
            part = mcp_content_to_part(content)
            if part is not None:
                return await save_part_as_artifact(
                    tool_context, filename=fname, part=part, scope="session"
                )
        _log.info("mcp_export_artifact: %s returned no usable content", uri)
        return None
