"""Auto-load project memory (CLAUDE.md / `.adk-cc/CONTEXT.md`) into
the system_instruction at the top of every turn.

Mirrors upstream Claude Code's CLAUDE.md behavior: an operator can
drop a markdown file at the project root (or in `~/.adk-cc/`) and the
model picks up the conventions, custom command preferences, and
codebase notes without anyone re-typing them into a prompt.

## Source precedence

The plugin loads files in this order (top-to-bottom in the final
system_instruction — most project-specific first):

  1. Project (walked up from cwd until home or `/`):
       - Per directory, ONE OF (priority order): `CLAUDE.md`,
         `AGENTS.md`. First existing wins — CLAUDE.md takes priority
         over AGENTS.md when both are present in the same dir so
         operators with the upstream-Claude-Code file get the
         existing behavior unchanged.
       - PLUS (always, independent): `<dir>/.adk-cc/CONTEXT.md` —
         adk-cc-namespaced layer; different path, different concern.
  2. Tenant (multi-tenant deploys only; reads `temp:tenant_context`
     from state which `TenancyPlugin` populates):
       - `<tenant_workspace_root>/CONTEXT.md`
       - `<tenant_workspace_root>/<user_id>/CONTEXT.md`
  3. User:
       - `~/.adk-cc/CONTEXT.md`
       - ONE OF (priority order): `~/.claude/CLAUDE.md`,
         `~/.agents/AGENTS.md`.
  4. Operator extras (absolute paths only):
       - `ADK_CC_CONTEXT_FILES=/path/a,/path/b`

Why the pick-one rule for CLAUDE.md / AGENTS.md: projects that
adopt both conventions (Claude Code + the vendor-neutral AGENTS.md
spec) would otherwise have their context double-counted in every
turn. Picking one per directory keeps the prompt clean; the priority
order (`CLAUDE.md` first) means existing CLAUDE.md projects don't
change behavior when AGENTS.md support lands.

Missing / empty files are silently skipped. Files exceeding the
per-file byte cap (`ADK_CC_CONTEXT_MAX_BYTES`, default 50000) are
loaded truncated with a marker. Duplicate paths (operator extras
matching a discovered project file) are deduplicated.

## Plugin chain order

Registered BEFORE `PlanModeReminderPlugin` / `TaskReminderPlugin` so
the final system_instruction reads:

    [project context block]              ← THIS plugin
    [agent.instruction text]              ← original
    [plan-mode reminder, if applicable]
    [active-task reminder, if applicable]

Project context is stable across turns; per-turn injections come
after, where the model focuses on the freshest signal.

## Hot reload

File contents are cached per-process and re-read when the on-disk
mtime drifts. No restart needed to pick up a CLAUDE.md edit.

## Audit

Emits `project_context_loaded` to the audit sink on:
  - First successful load this process.
  - Any subsequent turn where a cached source's mtime drifted (i.e.
    operator edited the file).

Payload: `sources=[{path, bytes, mtime}, ...]`, `total_bytes`, plus
the standard ctx fields (`session_id`, `agent_name`, etc).

## Opt-out

`ADK_CC_DISABLE_PROJECT_CONTEXT=1` → plugin no-ops on every turn.
Plugin construction is still cheap (one env-var read).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from .audit import AuditPlugin, emit_audit_event

_log = logging.getLogger(__name__)

# Per project directory, exactly ONE of these is loaded — the first
# existing wins. `CLAUDE.md` takes priority over `AGENTS.md` so
# Claude Code projects already using the upstream convention see no
# behavior change.
_PROJECT_PICK_ONE_FILENAMES = ("CLAUDE.md", "AGENTS.md")

# Always-loaded adk-cc-namespaced project file. Loaded independently
# from the pick-one above — different path (`.adk-cc/CONTEXT.md`),
# different concern (adk-cc-specific notes layered on top of generic
# CLAUDE.md / AGENTS.md). An operator using both gets both.
_PROJECT_ADDITIONAL_FILENAMES = (".adk-cc/CONTEXT.md",)

# Always-loaded user-level files.
_USER_ALWAYS_FILENAMES = ("~/.adk-cc/CONTEXT.md",)

# Per user-level location, ONE OF these — same priority rule as
# project. `~/.claude/CLAUDE.md` takes priority over
# `~/.agents/AGENTS.md` for the same reason as the project pair.
_USER_PICK_ONE_FILENAMES = ("~/.claude/CLAUDE.md", "~/.agents/AGENTS.md")

_DEFAULT_MAX_BYTES = 50_000


class ProjectContextPlugin(BasePlugin):
    """Prepend project / user / tenant context files to every
    `system_instruction`. Hot-reloads on file mtime drift. See module
    docstring for source precedence and the audit/log contract."""

    def __init__(self, name: str = "adk_cc_project_context") -> None:
        super().__init__(name=name)
        self._enabled = (
            os.environ.get("ADK_CC_DISABLE_PROJECT_CONTEXT", "").strip() != "1"
        )
        self._max_bytes = _parse_int(
            os.environ.get("ADK_CC_CONTEXT_MAX_BYTES"),
            default=_DEFAULT_MAX_BYTES,
        )
        self._extra_paths = _parse_extra_paths(
            os.environ.get("ADK_CC_CONTEXT_FILES")
        )
        # path -> (mtime, content). Used for both the cache hit
        # (mtime unchanged → reuse content) and the audit-emit
        # trigger (mtime drifted → re-read AND emit).
        self._cache: dict[Path, tuple[float, str]] = {}
        # path -> mtime of the version last reported in an audit
        # event. When this differs from the current mtime, we emit
        # again. None means "never emitted for this path yet".
        self._last_emitted_mtime: dict[Path, float] = {}

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        if not self._enabled:
            return None
        sources = self._resolve_sources(callback_context)
        loaded = self._load_all(sources)
        if not loaded:
            return None
        context_block = self._format_block(loaded)
        _prepend_to_system_instruction(llm_request, context_block)
        self._maybe_emit_audit(loaded, callback_context)
        return None

    # --- Source resolution -----------------------------------------

    def _resolve_sources(self, ctx: CallbackContext) -> list[Path]:
        """Ordered list of candidate paths to try (top of returned
        list = top of context block in system_instruction).

        Per-directory pick-one rule applies for the
        `CLAUDE.md`/`AGENTS.md` pair (and the user-level
        `~/.claude/CLAUDE.md`/`~/.agents/AGENTS.md` pair) — that
        decision requires statting, so we do it inline. The
        `.adk-cc/CONTEXT.md` track is added unconditionally and
        `_load_all` handles missing files silently.
        """
        out: list[Path] = []

        # 1. Project — walk up from cwd until home dir or filesystem root.
        cwd = Path.cwd()
        home = Path.home()
        cursor = cwd.resolve()
        # Cap the walk at the home dir AND at the filesystem root to
        # avoid scanning /etc, /var, etc on weird CWDs.
        while True:
            # Per dir, ONE OF (CLAUDE.md, AGENTS.md) — first existing wins.
            for filename in _PROJECT_PICK_ONE_FILENAMES:
                candidate = (cursor / filename).resolve()
                if _exists_silently(candidate):
                    out.append(candidate)
                    break
            # PLUS (always) the adk-cc-namespaced file. Separate path,
            # separate concern from the pick-one above.
            for filename in _PROJECT_ADDITIONAL_FILENAMES:
                candidate = (cursor / filename).resolve()
                out.append(candidate)
            if cursor == home or cursor == cursor.parent:
                break
            cursor = cursor.parent

        # 2. Tenant — multi-tenant deploys.
        tenant_paths = _tenant_paths(ctx)
        out.extend(tenant_paths)

        # 3. User-level.
        for raw in _USER_ALWAYS_FILENAMES:
            out.append(Path(os.path.expanduser(raw)))
        for raw in _USER_PICK_ONE_FILENAMES:
            candidate = Path(os.path.expanduser(raw))
            if _exists_silently(candidate):
                out.append(candidate)
                break

        # 4. Operator-specified extras.
        out.extend(self._extra_paths)

        # Dedup while preserving order.
        seen: set[Path] = set()
        deduped: list[Path] = []
        for p in out:
            if p in seen:
                continue
            seen.add(p)
            deduped.append(p)
        return deduped

    # --- File loading + caching ------------------------------------

    def _load_all(self, sources: list[Path]) -> list[dict[str, Any]]:
        """Read each path. Returns a list of `{path, bytes, mtime,
        content, truncated}` dicts in source order, skipping missing
        / empty files. Hits the per-process cache when on-disk mtime
        matches the cached one."""
        loaded: list[dict[str, Any]] = []
        for path in sources:
            try:
                stat = path.stat()
            except (FileNotFoundError, NotADirectoryError, OSError):
                # Missing file or unreadable parent dir — silent skip.
                continue
            if not stat.st_size:
                continue
            mtime = stat.st_mtime
            cached = self._cache.get(path)
            if cached is not None and cached[0] == mtime:
                content = cached[1]
            else:
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                content, truncated = _truncate(raw, self._max_bytes)
                # Cache even truncated content — re-truncating on
                # every turn is wasted work.
                self._cache[path] = (mtime, content)
                if truncated:
                    _log.warning(
                        "ProjectContextPlugin: %s truncated to %d bytes",
                        path,
                        self._max_bytes,
                    )
            if not content.strip():
                continue
            loaded.append({
                "path": path,
                "bytes": len(content.encode("utf-8")),
                "mtime": mtime,
                "content": content,
            })
        return loaded

    # --- Formatting ------------------------------------------------

    @staticmethod
    def _format_block(loaded: list[dict[str, Any]]) -> str:
        """Join file contents with HTML-comment section markers. The
        markers are easy to spot in `model_io_trace` debug dumps and
        survive markdown rendering without being visible to operators
        reading rendered output."""
        sections: list[str] = []
        for item in loaded:
            marker = (
                f"<!-- adk-cc:context source={item['path']} "
                f"bytes={item['bytes']} mtime={item['mtime']:.0f} -->"
            )
            sections.append(f"{marker}\n{item['content'].rstrip()}")
        return "\n\n".join(sections)

    # --- Audit emit ------------------------------------------------

    def _maybe_emit_audit(
        self, loaded: list[dict[str, Any]], ctx: CallbackContext
    ) -> None:
        """Emit `project_context_loaded` on first successful load AND
        whenever any cached source's mtime has changed since the last
        emit. No-op when the set of (path, mtime) pairs hasn't moved."""
        any_change = False
        for item in loaded:
            prev = self._last_emitted_mtime.get(item["path"])
            if prev is None or prev != item["mtime"]:
                any_change = True
                self._last_emitted_mtime[item["path"]] = item["mtime"]
        if not any_change:
            return
        total = sum(int(x["bytes"]) for x in loaded)
        event: dict[str, Any] = {
            "ts": time.time(),
            "event": "project_context_loaded",
            "sources": [
                {"path": str(x["path"]), "bytes": x["bytes"], "mtime": x["mtime"]}
                for x in loaded
            ],
            "total_bytes": total,
        }
        try:
            event.update(AuditPlugin._ctx_fields(ctx))
        except Exception:
            # Defensive — ctx fields must never crash a turn.
            pass
        emit_audit_event(event)
        _log.info(
            "ProjectContextPlugin loaded %d source(s), total_bytes=%d",
            len(loaded),
            total,
        )


# --- Helpers --------------------------------------------------------


def _prepend_to_system_instruction(req: LlmRequest, text: str) -> None:
    """Mirrors `task_reminder._append_to_system_instruction` but
    prepends. `system_instruction` can be None / str / Part / list[Part];
    handle all four shapes."""
    existing = req.config.system_instruction
    if existing is None:
        req.config.system_instruction = text
    elif isinstance(existing, str):
        req.config.system_instruction = text + "\n\n" + existing
    else:
        try:
            parts = (
                list(existing) if isinstance(existing, list) else [existing]
            )
            parts.insert(0, types.Part(text=text))
            req.config.system_instruction = parts
        except Exception:
            # Defensive — corrupted shape; leave it alone rather than
            # crash the turn.
            pass


def _exists_silently(path: Path) -> bool:
    """`Path.is_file()` swallowing OSError. Permission errors / dead
    symlinks / unreadable parent dirs all collapse to False so a
    weird filesystem entry can never crash the turn."""
    try:
        return path.is_file()
    except OSError:
        return False


def _tenant_paths(ctx: CallbackContext) -> list[Path]:
    """Return tenant-scoped CONTEXT.md candidates if a TenantContext
    is in session state. Multi-tenant deployments populate the
    `temp:tenant_context` key via `TenancyPlugin` before any tool
    fires; local CLI sessions don't have it."""
    out: list[Path] = []
    try:
        session = getattr(ctx, "session", None)
        if session is None:
            return out
        state = getattr(session, "state", None)
        if state is None:
            return out
        tc = state.get("temp:tenant_context") if hasattr(state, "get") else None
        if tc is None:
            return out
        root = getattr(tc, "workspace_root_path", None)
        if root is not None:
            out.append(Path(root) / "CONTEXT.md")
            user_id = getattr(tc, "user_id", None)
            if user_id:
                out.append(Path(root) / str(user_id) / "CONTEXT.md")
    except Exception:
        # Tenant context lookup must never crash a turn.
        return []
    return out


def _parse_extra_paths(raw: Optional[str]) -> list[Path]:
    """Comma-separated absolute paths; relative / empty entries are
    silently dropped."""
    if not raw:
        return []
    out: list[Path] = []
    for chunk in raw.split(","):
        s = chunk.strip()
        if not s:
            continue
        p = Path(s)
        if not p.is_absolute():
            _log.warning(
                "ProjectContextPlugin: ignoring non-absolute ADK_CC_CONTEXT_FILES entry %r",
                s,
            )
            continue
        out.append(p)
    return out


def _parse_int(raw: Optional[str], *, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    if v < 0:
        return default
    return v


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate at byte boundary, decoding-safe. Returns (text,
    was_truncated). Appends a marker line so the model knows it's
    looking at a partial file."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    cut = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return cut + "\n\n(... truncated by ProjectContextPlugin ...)", True
