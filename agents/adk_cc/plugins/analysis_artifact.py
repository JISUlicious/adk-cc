"""Surface analysis outputs in the chat (W6.1 — the missing trigger).

Rendering already works: `ArtifactChip` auto-previews an HTML artifact in a
sandboxed iframe, and a pixel-level e2e confirms a JS-drawn chart really paints
(`tests/e2e_chart_preview_ui.py`). What was missing is the path from "the agent
wrote analysis/dashboard.html" to "the user sees it": a workspace file produces
no `actions.artifactDelta`, so nothing appears in the conversation and the user
has to go hunting through the file tree for an artifact they just asked for.

This plugin closes that gap. After a tool call that plausibly wrote a
previewable file, it registers that file as a SESSION artifact, which makes ADK
record the artifactDelta the existing chip already listens for. No new UI, no
new model instruction — and no dependence on the model remembering to call
`save_as_artifact`, which is the kind of discipline that works in testing and
evaporates in real turns.

Cost control matters because this runs after every tool call:

* Candidate paths come from the tool's OWN args and output (the command line
  that wrote the file, the path argument, a path printed to stdout). No
  directory scan, so a call that produced nothing costs one regex.
* Files are read at most once per (path, content-hash) per session — a chart
  regenerated unchanged is not stored twice.
* Anything over the size cap is skipped with a log line rather than silently
  bloating the artifact store.

Scope guards, in order: the path must resolve INSIDE the workspace root, must
not sit in a dot-directory (`.adk-cc/analysis-env` alone contains matplotlib's
bundled HTML templates — a real false positive already observed in a test
harness), and must carry a previewable extension.

`ADK_CC_ANALYSIS_ARTIFACTS=0` disables it.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from ..config.schema import env_bool
from ..tools._artifact import save_part_as_artifact

_log = logging.getLogger(__name__)

# Tools that can put a file on disk. Everything else short-circuits.
_WRITING_TOOLS = frozenset({
    "write_file", "edit_file", "run_bash", "run_skill_script",
})

_PREVIEWABLE = (".html", ".htm", ".png", ".svg", ".jpg", ".jpeg", ".webp", ".pdf")

# Paths as they appear in a command line or in stdout.
_PATH_RE = re.compile(
    r"[\w./\-]+\.(?:html?|png|svg|jpe?g|webp|pdf)\b", re.IGNORECASE
)

_STATE_SEEN = "temp:analysis_artifacts_seen"
_MAX_PER_CALL = 3


def _enabled() -> bool:
    return env_bool("ADK_CC_ANALYSIS_ARTIFACTS", True)


def _max_bytes() -> int:
    try:
        mb = float(os.environ.get("ADK_CC_ANALYSIS_ARTIFACT_MAX_MB", "12"))
    except ValueError:
        mb = 12.0
    return int(mb * 1024 * 1024)


def _text_of(value: Any) -> str:
    """Flatten a tool result into searchable text without importing its shape."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text_of(v) for v in value.values() if v is not None)
    if isinstance(value, (list, tuple)):
        return " ".join(_text_of(v) for v in value)
    return ""


def _candidates(tool_name: str, args: dict, result: Any) -> list[str]:
    """Paths this call might have written, newest-looking first."""
    out: list[str] = []
    args = args or {}
    for key in ("path", "file_path", "output", "filename"):
        v = args.get(key)
        if isinstance(v, str) and v.lower().endswith(_PREVIEWABLE):
            out.append(v)
    haystack = " ".join([
        str(args.get("command") or ""),
        str(args.get("code") or ""),
        _text_of(result),
    ])
    for m in _PATH_RE.finditer(haystack):
        out.append(m.group(0))
    # preserve order, drop duplicates
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


class AnalysisArtifactPlugin(BasePlugin):
    """Register previewable files a tool call produced. See module doc."""

    def __init__(self, *, name: str = "adk_cc_analysis_artifact") -> None:
        super().__init__(name=name)

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict,
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        if not _enabled() or getattr(tool, "name", "") not in _WRITING_TOOLS:
            return None
        try:
            await self._register(tool, tool_args, tool_context, result)
        except Exception:  # noqa: BLE001 — never fail a tool call over a preview
            _log.debug("analysis artifact: registration failed", exc_info=True)
        return None  # the tool result is never modified

    async def _register(self, tool, tool_args, ctx, result) -> None:
        from ..sandbox import get_backend
        from ..sandbox.workspace import get_workspace
        from ..tools._fs import resolve

        cands = _candidates(getattr(tool, "name", ""), tool_args, result)
        if not cands:
            return
        ws = get_workspace(ctx)
        backend = get_backend(ctx)
        root = Path(ws.abs_path).resolve()
        state = getattr(ctx, "state", None)
        seen = {}
        if state is not None:
            try:
                seen = dict(state.get(_STATE_SEEN) or {})
            except Exception:  # noqa: BLE001
                seen = {}

        saved = 0
        for raw_path in cands:
            if saved >= _MAX_PER_CALL:
                break
            try:
                p = resolve(raw_path, ctx).resolve()
            except Exception:  # noqa: BLE001 — unresolvable candidate, skip
                continue
            # Containment: never leave the workspace, never descend into a
            # dot-directory (the uv analysis-env ships library HTML templates).
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue

            try:
                data = await backend.read_bytes(str(p), fs_read=ws.fs_read_config())
            except Exception:  # noqa: BLE001 — not written, not readable: fine
                continue
            if not data:
                continue
            if len(data) > _max_bytes():
                _log.info("analysis artifact: %s is %.1fMB — over the cap, skipped",
                          rel, len(data) / 1024 / 1024)
                continue

            digest = hashlib.sha256(data).hexdigest()[:16]
            key = str(rel)
            if seen.get(key) == digest:
                continue  # unchanged since we last stored it

            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            res = await save_part_as_artifact(
                ctx,
                filename=p.name,
                part=types.Part(inline_data=types.Blob(data=data, mime_type=mime)),
                scope="session",
            )
            if res.get("status") == "ok":
                seen[key] = digest
                saved += 1
                _log.info("analysis artifact: surfaced %s (%d bytes)", rel, len(data))

        if state is not None and saved:
            try:
                state[_STATE_SEEN] = seen
            except Exception:  # noqa: BLE001 — best-effort dedupe
                pass
