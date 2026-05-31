"""Convert an MCP content object into a `types.Part` with inline bytes.

Mirrors ADK's `load_mcp_resource_tool._mcp_content_to_part` (which builds
text/file Parts) but always produces an `inline_data` Blob, so the result
flows straight into `save_part_as_artifact` (which reads
`part.inline_data.{data,mime_type}`).

Handles the two shapes MCP delivers files in:
  - resource-read contents: `TextResourceContents` (`.text`) /
    `BlobResourceContents` (`.blob`, base64), each with `.mimeType`.
  - tool-result `EmbeddedResource`: the payload is nested one level under
    `.resource` (itself a Text/Blob ResourceContents) — callers may pass
    either the EmbeddedResource or its `.resource`; we unwrap if needed.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Optional
from urllib.parse import urlparse

from google.genai import types


def safe_artifact_name(name_or_uri: str) -> str:
    """Derive a flat, filesystem-safe artifact filename from a name / URI.

    Takes the basename of a URI path (so `db://schema/users` → `users`,
    `file:///a/b/c.csv` → `c.csv`), then replaces separators / hostile
    chars. Falls back to "resource" when nothing usable remains. Shared by
    both the save-resource tool and the auto-persist plugin so a given
    resource yields the SAME filename whichever path saves it.
    """
    raw = name_or_uri
    parsed = urlparse(name_or_uri)
    if parsed.scheme and parsed.path:
        raw = parsed.path
    raw = raw.strip("/").split("/")[-1] or name_or_uri
    raw = re.sub(r"[/\\:\s]+", "_", raw)
    raw = re.sub(r"[^A-Za-z0-9._\-]", "", raw)
    # Collapse a result that's only separators/dots (e.g. "///" → "_") to
    # the fallback so we never produce a junk filename.
    if not raw.strip("_.-"):
        return "resource"
    return raw


def mcp_content_to_part(content: Any) -> Optional[types.Part]:
    """Return a `types.Part` (inline_data Blob) for an MCP content object.

    Returns None when the content is neither text nor blob (e.g. an
    unsupported/empty content item) so callers can surface a clear error.
    """
    if content is None:
        return None

    # Unwrap an EmbeddedResource (its payload lives under `.resource`).
    inner = getattr(content, "resource", None)
    if inner is not None and (
        getattr(inner, "text", None) is not None
        or getattr(inner, "blob", None) is not None
    ):
        content = inner

    mime = getattr(content, "mimeType", None)

    text = getattr(content, "text", None)
    if text is not None:
        return types.Part(
            inline_data=types.Blob(
                data=text.encode("utf-8"),
                mime_type=mime or "text/plain",
            )
        )

    blob = getattr(content, "blob", None)
    if blob is not None:
        try:
            data = base64.b64decode(blob)
        except Exception:  # noqa: BLE001 — malformed base64
            return None
        return types.Part(
            inline_data=types.Blob(
                data=data,
                mime_type=mime or "application/octet-stream",
            )
        )

    return None
