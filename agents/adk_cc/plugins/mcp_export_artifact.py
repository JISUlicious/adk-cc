"""Auto-persist files returned by MCP tool calls as artifacts (Pattern C).

ADK's `McpTool` returns a tool result as a plain `response.model_dump()`
dict — so when an MCP tool returns a file (an embedded resource, or a
`resource_link` to an export), the bytes either bloat the model's context
(embedded) or are never persisted (link). This plugin closes that gap:
after any `mcp__*` tool call, it detects file-bearing content blocks and
saves them to the artifact store (S3/MinIO), so DB-export-style tools
"just work" with a downloadable artifact and no agent decision.

Gated OFF by default (`ADK_CC_MCP_AUTOSAVE_EXPORTS=1` to enable) — like
ModelIOTracePlugin, the per-call cost when disabled is one attribute
check. Registered in `App.plugins` (agent.py).

v1 scope:
  - `EmbeddedResource` (inline text/blob) → saved; the inline bytes are
    STRIPPED from the returned result (they're already persisted) to keep
    them out of the model's context.
  - `ResourceLink` with an `https://` or `s3://` URI → fetched/referenced
    and saved; the link is left in the result, augmented with the
    artifact ref so the model knows it's downloadable.
  - `ResourceLink` with `file://` / a custom scheme → needs a raw-URI read
    through the producing server's MCP session; logged + skipped in v1
    (follow-up). The link stays in the result untouched.

Never raises — a failed autosave logs a warning and leaves the tool
result unchanged.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from ..tools._artifact import save_part_as_artifact
from ..tools._mcp_content import mcp_content_to_part

_log = logging.getLogger(__name__)


def _safe_name(name_or_uri: str) -> str:
    """Flat artifact filename from a resource name / URI (basename-ish)."""
    raw = name_or_uri
    parsed = urlparse(name_or_uri)
    if parsed.scheme and parsed.path:
        raw = parsed.path
    raw = raw.strip("/").split("/")[-1] or name_or_uri
    raw = re.sub(r"[/\\:\s]+", "_", raw)
    raw = re.sub(r"[^A-Za-z0-9._\-]", "", raw)
    return raw or "export"


class McpExportArtifactPlugin(BasePlugin):
    """Auto-saves file-bearing MCP tool-result content as artifacts."""

    def __init__(self, name: str = "adk_cc_mcp_export_artifact") -> None:
        super().__init__(name=name)
        self._enabled = os.environ.get("ADK_CC_MCP_AUTOSAVE_EXPORTS") == "1"
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
                    fname = _safe_name(
                        str(getattr(c.resource, "uri", None) or f"{name}_embedded")
                    )
                    res = await save_part_as_artifact(
                        tool_context, filename=fname, part=part, scope="session"
                    )
                    if res.get("status") == "ok":
                        saved.append(res)
                        strip_indices.add(idx)
                else:  # ResourceLink
                    res = await self._save_link(c, tool_context)
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
        self, link: Any, tool_context: ToolContext
    ) -> Optional[dict]:
        """Persist a ResourceLink by URI scheme (v1: https / s3 only)."""
        uri = str(getattr(link, "uri", "") or "")
        scheme = urlparse(uri).scheme.lower()
        fname = _safe_name(getattr(link, "name", None) or uri)
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
            # The export already lives in object storage. v1: record a
            # reference rather than re-downloading + re-uploading. A full
            # implementation would register it in the artifact index by
            # canonical URI; for now we surface it so the model/UI sees it.
            _log.info("mcp_export_artifact: s3 resource_link recorded (no re-copy): %s", uri)
            return {"status": "ok", "filename": fname, "version": -1,
                    "scope": "reference", "bytes": getattr(link, "size", None),
                    "canonical_uri": uri}

        # file:// or custom scheme → needs a raw-URI MCP session read.
        _log.info(
            "mcp_export_artifact: skipping %s resource_link (raw-URI session "
            "read not implemented in v1): %s", scheme or "?", uri,
        )
        return None
