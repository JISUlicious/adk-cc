"""Skill loading and toolset construction.

Skills are operator-defined parameterized prompts (Anthropic's skill
format) that adk-cc surfaces as tools the coordinator can invoke.

ADK ships:
  - `google.adk.skills.list_skills_in_dir(path)` → {name: Frontmatter}
  - `google.adk.skills.load_skill_from_dir(skill_dir)` → Skill
  - `google.adk.tools.skill_toolset.SkillToolset(skills, code_executor=...,
    script_timeout=300, additional_tools=...)`

This module discovers skills from MULTIPLE directories, in priority
order, and returns a `SkillToolset` with a lenient
`load_skill_resource` that adds a filesystem-scan fallback for
skills that don't strictly follow the references/scripts/assets
layout.

## Discovery precedence

`_resolve_skills_dirs()` returns an ordered list. When the same
skill name appears in multiple dirs, the FIRST discovered wins
(higher-precedence source overrides lower).

  1. **`ADK_CC_SKILLS_DIR`** (operator explicit) — if set and the
     dir exists, included first.
  2. **Project walk-up** (cwd up to home / filesystem root):
       - Per directory, ONE OF (priority order):
         `.adk-cc/skills/`, `.claude/skills/`. First existing wins
         for that dir.
       - Skipped entirely when `ADK_CC_DISABLE_PROJECT_SKILLS=1`.
  3. **Install fallback** `<install>/adk_cc/skills/` — the dir
     co-located with the agent module, used when no env var / no
     project skills are discovered.

Why the pick-one rule for `.adk-cc/skills` vs `.claude/skills`:
projects adopting both conventions would otherwise double-register
the same skills (same SKILL.md files copied or symlinked). Picking
one per directory keeps the tool surface clean; `.adk-cc/skills/`
wins so adk-cc-specific overrides take precedence over generic
Claude Code skills in mixed projects.

Mirrors the file-discovery + per-dir pick-one rule from
`ProjectContextPlugin` (PR #24) — same precedence shape for
CLAUDE.md / AGENTS.md.

Why the lenient tool: ADK's stock `LoadSkillResourceTool` only resolves
paths starting with `references/`, `assets/`, or `scripts/`. Anthropic's
own official skills repo sometimes places auxiliary docs at the skill
root (e.g. `pptx/pptxgenjs.md`, `pptx/editing.md`). The model
reasonably guesses `scripts/<file>.md` or `references/<file>.md` and
the strict tool returns RESOURCE_NOT_FOUND. The fallback scans the
real on-disk skill directory by basename, so the model's guess
resolves to the actual file regardless of which subfolder it picked.

Skill scripts execute under a `code_executor`. By default this factory
wires `SandboxBackedCodeExecutor`, which routes script execution through
the active session's `SandboxBackend` — same isolation as `run_bash`
(NoopBackend on dev, DockerBackend / SandboxServiceBackend in prod).
Without this default, ADK's `RunSkillScriptTool` returns
`NO_CODE_EXECUTOR` and skills with `scripts/` go unused. Pass an
explicit `code_executor=` to override.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.skills import (
    Skill,
    list_skills_in_dir,
    load_skill_from_dir,
)
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.skill_toolset import (
    LoadSkillResourceTool,
    LoadSkillTool,
    RunSkillScriptTool,
    SkillToolset,
)
from google.adk.tools.tool_context import ToolContext
from google.genai import types

_log = logging.getLogger(__name__)


# Per-directory pick-one rule. Walk-up checks these in priority order;
# the first existing subdir for a given walked dir is added, the other
# is skipped. `.adk-cc/skills/` wins so adk-cc-specific overrides take
# precedence over generic Claude Code skills when both are present.
_PROJECT_SKILLS_PICK_ONE = (".adk-cc/skills", ".claude/skills")


def _resolve_skills_dirs() -> list[Path]:
    """Ordered list of skills directories to scan. First-found skill
    name wins across all returned dirs.

    Order:
      1. `ADK_CC_SKILLS_DIR` (operator explicit, highest precedence).
      2. Project walk-up (`.adk-cc/skills/` or `.claude/skills/` per
         directory, walked from cwd up to home / filesystem root).
         Skipped entirely when `ADK_CC_DISABLE_PROJECT_SKILLS=1`.
      3. Install fallback `<install>/adk_cc/skills/`.

    Each dir is included at most once (dedup by resolved path). A dir
    that doesn't exist or isn't a directory is silently dropped.
    """
    dirs: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            resolved = p.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        if not _is_dir_silently(resolved):
            return
        seen.add(resolved)
        dirs.append(resolved)

    # 1. Operator-explicit env var.
    raw = os.environ.get("ADK_CC_SKILLS_DIR")
    if raw:
        _add(Path(raw).expanduser())

    # 2. Project walk-up — unless opted out.
    if os.environ.get("ADK_CC_DISABLE_PROJECT_SKILLS", "").strip() != "1":
        try:
            cwd = Path.cwd().resolve()
        except OSError:
            cwd = None
        if cwd is not None:
            home = Path.home()
            cursor = cwd
            while True:
                for sub in _PROJECT_SKILLS_PICK_ONE:
                    candidate = cursor / sub
                    if _is_dir_silently(candidate):
                        _add(candidate)
                        break  # pick-one per directory
                if cursor == home or cursor == cursor.parent:
                    break
                cursor = cursor.parent

    # 3. Install fallback.
    here = Path(__file__).resolve().parent.parent / "skills"
    _add(here)

    return dirs


def _is_dir_silently(p: Path) -> bool:
    """`Path.is_dir()` swallowing OSError — same defensive pattern as
    `ProjectContextPlugin._exists_silently`."""
    try:
        return p.is_dir()
    except OSError:
        return False


def discover_skills(skills_dir: Optional[Path] = None) -> list[Skill]:
    """Load every skill under skills_dir. Empty list if no dir or no
    skills.

    Backward-compat: when `skills_dir` is None and there are no
    discoverable dirs (no env var, no project walk hits, no install
    fallback), returns []. Callers wanting the multi-dir aggregated
    flow with skill→dir pairs should use
    `discover_skills_with_sources` directly.
    """
    if skills_dir is not None:
        return [s for s, _ in _load_skills_from_dir(skills_dir)]
    return [s for s, _ in discover_skills_with_sources()]


def discover_skills_with_sources(
    skills_dirs: Optional[list[Path]] = None,
) -> list[tuple[Skill, Path]]:
    """Aggregate skills across all resolved dirs. Returns
    `(skill, source_dir)` pairs so the lenient resource-loader can
    map each skill to its actual on-disk location regardless of
    which root it came from.

    Dedup by skill name: first-discovered wins. With the default
    resolution order, that means `ADK_CC_SKILLS_DIR` overrides
    project skills, which override install-fallback skills.
    """
    dirs = skills_dirs if skills_dirs is not None else _resolve_skills_dirs()
    seen_names: set[str] = set()
    out: list[tuple[Skill, Path]] = []
    for base in dirs:
        for skill, skill_dir in _load_skills_from_dir(base):
            try:
                name = skill.frontmatter.name
            except Exception:
                continue
            if name in seen_names:
                _log.info(
                    "skills: '%s' from %s shadowed by earlier source",
                    name,
                    skill_dir,
                )
                continue
            seen_names.add(name)
            out.append((skill, skill_dir))
    return out


def _load_skills_from_dir(base: Path) -> list[tuple[Skill, Path]]:
    """Load every skill under one directory. Returns (skill, dir)
    pairs so callers can build a name→path index across multiple
    sources."""
    out: list[tuple[Skill, Path]] = []
    if not _is_dir_silently(base):
        return out
    try:
        names = list(list_skills_in_dir(base).keys())
    except Exception:
        return out
    for name in names:
        skill_dir = (base / name).resolve()
        try:
            out.append((load_skill_from_dir(skill_dir), skill_dir))
        except Exception:
            # Skip malformed skills rather than refusing to start.
            continue
    return out


def _build_skill_dir_index(
    skills_with_sources: list[tuple[Skill, Path]],
) -> dict[str, str]:
    """Map skill_name → absolute on-disk skill directory.

    Used by `_LenientLoadSkillResourceTool` to scan the real
    filesystem when ADK's strict path lookup misses. Driven by the
    `(skill, source_dir)` pairs from `discover_skills_with_sources`
    so each skill points at its actual root regardless of which
    discovery source contributed it.
    """
    out: dict[str, str] = {}
    for skill, skill_dir in skills_with_sources:
        try:
            name = skill.frontmatter.name
        except Exception:
            continue
        if not skill_dir.is_dir():
            continue
        out[name] = str(skill_dir.resolve())
    return out


# --- bounded / paginated resource loading -------------------------------
#
# ADK's load_skill_resource / load_skill return whole files with no size cap
# or pagination — a large reference dumps wholesale into the model's context.
# adk-cc's own read_file tool already solved this (line offset/limit + a
# per-line cap + a "paginate to continue" envelope); we mirror that exact
# discipline here so skill resources read like every other file, and so the
# model has ONE mental model. Mirrors patterns in mature frameworks (Claude
# reads skill resources via bounded file tools; MCP caps + paginates resource
# reads). Tunable; same per-line cap constant as read_file.py.

_MAX_LINE_LENGTH = 2000  # mirrors read_file.py
_LINE_TRUNCATION_SUFFIX = "… [truncated]"


def _int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resource_default_limit() -> int:
    return _int_env("ADK_CC_SKILL_RESOURCE_DEFAULT_LINES", 200)


def _resource_max_lines() -> int:
    return _int_env("ADK_CC_SKILL_RESOURCE_MAX_LINES", 400)


def _instructions_max_chars() -> int:
    return _int_env("ADK_CC_SKILL_INSTRUCTIONS_MAX_CHARS", 60000)


def _file_max_bytes() -> int:
    return _int_env("ADK_CC_SKILL_FILE_MAX_BYTES", 262144)


def _resource_read_max_bytes() -> int:
    """Hard cap on bytes read from disk for ONE resource (search or the
    load_skill_resource disk fallback). Bounds memory: a file larger than
    this is skipped/not inlined rather than read whole into RAM. Default 4MB."""
    return _int_env("ADK_CC_SKILL_RESOURCE_READ_MAX_BYTES", 4194304)


def _guards_on() -> bool:
    """Phase-2 guards (script-on-noop refusal + untrusted-content delimiters),
    toggled together. Off by default — opt in with ADK_CC_SKILL_GUARDS=1."""
    return os.environ.get("ADK_CC_SKILL_GUARDS") == "1"


def _wrap_untrusted(content: str, source: str) -> str:
    """Phase-2: mark model-bound skill content as untrusted DATA so an
    injected instruction in a (possibly third-party) skill is less likely to
    be obeyed. No-op unless guards are on.

    The content is UNTRUSTED, so it can contain a forged <skill_content> /
    </skill_content> tag to open or (more dangerously) close the wrapper early
    and smuggle text out as trusted. Neutralize any such tag in the content
    (case-insensitive) by escaping its '<', and escape the source attribute,
    so the only real delimiters are the ones we emit."""
    if not _guards_on():
        return content
    safe_content = re.sub(r"(?i)<(/?\s*skill_content)", r"&lt;\1", content)
    safe_source = (
        source.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        f'<skill_content trust="untrusted" source="{safe_source}">\n'
        f"{safe_content}\n</skill_content>"
    )


def _clip_lines(text: str, *, offset: int, limit: int) -> tuple[str, int, int, int, int, int]:
    """Slice `text` to `limit` lines from 1-indexed `offset`, capping each line
    at _MAX_LINE_LENGTH. Mirrors read_file.py. Returns (clipped, start_line,
    end_line, total_lines, total_chars, lines_truncated)."""
    lines = text.splitlines()  # mirrors read_file.py; no phantom trailing line
    total_lines = len(lines)
    total_chars = len(text)
    start = max(1, offset)
    start_idx = start - 1
    if start_idx >= total_lines:
        # offset past EOF → empty slice with a coherent (end < start) envelope.
        return "", start, start - 1, total_lines, total_chars, 0
    end_idx = min(start_idx + max(1, limit), total_lines)
    out: list[str] = []
    lines_truncated = 0
    for ln in lines[start_idx:end_idx]:
        if len(ln) > _MAX_LINE_LENGTH:
            out.append(ln[:_MAX_LINE_LENGTH] + _LINE_TRUNCATION_SUFFIX)
            lines_truncated += 1
        else:
            out.append(ln)
    clipped = "\n".join(out)
    end_line = start_idx + len(out)  # 1-indexed inclusive; start-1 if empty
    return clipped, start, end_line, total_lines, total_chars, lines_truncated


def _bounded_resource_payload(
    skill_name: str,
    file_path: str,
    content: str,
    *,
    offset: int,
    limit: int,
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    """Build the bounded, paginated resource result (read_file-style envelope)."""
    clipped, start, end, total_lines, total_chars, lt = _clip_lines(
        content, offset=offset, limit=limit
    )
    payload: dict[str, Any] = {
        "skill_name": skill_name,
        "file_path": file_path,
        "content": _wrap_untrusted(clipped, f"{skill_name}/{file_path}"),
        "start_line": start,
        "end_line": end,
        "total_lines": total_lines,
        "total_chars": total_chars,
        "lines_truncated": lt,
        "truncated": end < total_lines,
    }
    if end < total_lines:
        payload["next_offset"] = end + 1
        payload["hint"] = (
            f"showing lines {start}-{end} of {total_lines}; read more with "
            f"offset={end + 1}, or narrow with search_skill_resource."
        )
    if extra:
        payload.update(extra)
    return payload


def _prune_oversized_resources(skill: Skill, max_bytes: int) -> None:
    """Lazy/memory guard: drop large TEXT references & assets from the
    in-memory dicts. They stay on disk and are served on demand (bounded) by
    the lenient loader's disk fallback — so RAM is bounded without losing
    access. NOT pruned:
      - binary (bytes) entries — the utf-8 disk fallback can't serve them and
        ADK's binary-injection re-fetches them from this dict, so pruning
        would make them unreachable. They stay in memory.
      - scripts — run_skill_script executes them from memory; never context."""
    for bucket in (skill.resources.references, skill.resources.assets):
        for key in list(bucket.keys()):
            val = bucket[key]
            if not isinstance(val, str):
                continue  # keep binary/non-text in memory (see docstring)
            size = len(val.encode("utf-8"))  # BYTES, not characters
            if size > max_bytes:
                del bucket[key]
                _log.info(
                    "skills: pruned oversized in-memory resource %r (%d B > %d) "
                    "from skill %r — served on demand from disk",
                    key,
                    size,
                    max_bytes,
                    skill.name,
                )


class _LenientLoadSkillResourceTool(LoadSkillResourceTool):
    """`load_skill_resource` with an on-disk fallback for non-canonical layouts.

    Behavior:
      1. Try ADK's normal lookup (references/assets/scripts buckets).
      2. If that returns RESOURCE_NOT_FOUND or INVALID_RESOURCE_PATH,
         scan the real skill directory:
           a. Try the literal `file_path` relative to the skill root.
           b. If that misses, search for the basename anywhere under
              the skill dir; if exactly one match, return it.
      3. Path-traversal-safe: every candidate must resolve inside the
         skill directory.
      4. Returns `fallback_resolved=True` so the model can see the
         resolution wasn't via the canonical path. Includes
         `actual_path` when the basename match was used.
    """

    def __init__(
        self, toolset: "SkillToolset", skill_dirs: dict[str, str]
    ) -> None:
        super().__init__(toolset)
        self.description = (
            "Loads a resource file from within a skill, in bounded slices. "
            "Canonical paths start with 'references/', 'assets/', or "
            "'scripts/'; files at the skill root or other subdirs also "
            "resolve (pass the relative path or bare filename). Returns up to "
            "`limit` lines starting at 1-indexed `offset`; for large files, "
            "paginate with `offset = end_line + 1`, or use "
            "search_skill_resource to jump to the relevant part."
        )
        self._skill_dirs = skill_dirs

    def _get_declaration(self) -> types.FunctionDeclaration | None:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "The name of the skill.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Relative path to the resource (e.g."
                            " 'references/api.md')."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-indexed first line to return (default 1).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max lines to return (default"
                            f" {_resource_default_limit()}, capped at"
                            f" {_resource_max_lines()})."
                        ),
                    },
                },
                "required": ["skill_name", "file_path"],
            },
        )

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        offset = _coerce_int(args.get("offset"), 1)
        limit = min(
            _coerce_int(args.get("limit"), _resource_default_limit()),
            _resource_max_lines(),
        )
        result = await super().run_async(args=args, tool_context=tool_context)

        # In-memory hit (ADK's dict lookup) → re-wrap as a bounded slice.
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            return _bounded_resource_payload(
                result.get("skill_name", args.get("skill_name", "")),
                result.get("file_path", args.get("file_path", "")),
                result["content"],
                offset=offset,
                limit=limit,
            )

        # Miss (incl. pruned-from-memory large files) → disk fallback, bounded.
        if isinstance(result, dict) and result.get("error_code") in (
            "RESOURCE_NOT_FOUND",
            "INVALID_RESOURCE_PATH",
        ):
            fb = await asyncio.to_thread(
                self._scan, args.get("skill_name") or "", args.get("file_path") or ""
            )
            if fb is not None:
                content = fb.pop("content", "")
                return _bounded_resource_payload(
                    fb.get("skill_name", ""),
                    fb.get("file_path", ""),
                    content,
                    offset=offset,
                    limit=limit,
                    extra={
                        k: v
                        for k, v in fb.items()
                        if k not in ("skill_name", "file_path")
                    },
                )
            return result

        # Binary-detected status or other non-content dict → pass through.
        return result

    def _scan(self, skill_name: str, file_path: str) -> Optional[dict]:
        skill_dir = self._skill_dirs.get(skill_name)
        if not skill_dir or not file_path:
            return None
        base = Path(skill_dir).resolve()
        if not base.is_dir():
            return None

        # 1. Literal path from skill root.
        try:
            literal = (base / file_path).resolve()
            literal.relative_to(base)  # path-traversal guard
            if literal.is_file():
                content = self._read_text(literal)
                if content is not None:
                    _log.info(
                        "load_skill_resource: literal path '%s' resolved at skill root for '%s'.",
                        file_path,
                        skill_name,
                    )
                    return {
                        "skill_name": skill_name,
                        "file_path": file_path,
                        "content": content,
                        "fallback_resolved": True,
                    }
        except (ValueError, OSError):
            pass

        # 2. Basename search anywhere under the skill dir.
        basename = Path(file_path).name
        if not basename:
            return None
        try:
            candidates = [
                p for p in base.rglob(basename) if p.is_file()
            ]
        except OSError:
            return None
        # Skip __pycache__ noise that rglob picks up.
        candidates = [p for p in candidates if "__pycache__" not in p.parts]
        if len(candidates) != 1:
            return None  # ambiguous or zero matches; let the original error stand
        chosen = candidates[0]
        # Path-traversal guard (paranoid — rglob shouldn't escape, but).
        try:
            chosen.relative_to(base)
        except ValueError:
            return None
        content = self._read_text(chosen)
        if content is None:
            return None
        actual_rel = str(chosen.relative_to(base))
        _log.info(
            "load_skill_resource: basename match '%s' → '%s' for skill '%s'.",
            basename,
            actual_rel,
            skill_name,
        )
        return {
            "skill_name": skill_name,
            "file_path": file_path,
            "content": content,
            "fallback_resolved": True,
            "actual_path": actual_rel,
        }

    @staticmethod
    def _read_text(path: Path) -> Optional[str]:
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > _resource_read_max_bytes():
            _log.warning(
                "load_skill_resource: %s exceeds the read cap (%d > %d B) — "
                "not inlined; raise ADK_CC_SKILL_RESOURCE_READ_MAX_BYTES if needed",
                path, size, _resource_read_max_bytes(),
            )
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None


class _BoundedLoadSkillTool(LoadSkillTool):
    """`load_skill` that caps the injected SKILL.md instructions.

    The body should be small by spec, but nothing enforces it — guard anyway
    so a pathological SKILL.md can't dump unbounded text into context. Caps at
    ADK_CC_SKILL_INSTRUCTIONS_MAX_CHARS with a pointer to load_skill_resource
    for the rest. Wraps in untrusted-content delimiters when guards are on.
    """

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        result = await super().run_async(args=args, tool_context=tool_context)
        if isinstance(result, dict) and isinstance(result.get("instructions"), str):
            instr = result["instructions"]
            cap = _instructions_max_chars()
            total = len(instr)
            if total > cap:
                instr = (
                    instr[:cap]
                    + f"\n\n… [SKILL.md truncated at {cap} of {total} chars; "
                    "read the rest via load_skill_resource.]"
                )
                result["instructions_truncated"] = True
                result["total_instruction_chars"] = total
            result["instructions"] = _wrap_untrusted(
                instr, f"{args.get('skill_name', '')}/SKILL.md"
            )
        return result


class _SkillResourceSearchTool(BaseTool):
    """`search_skill_resource`: substring search within a skill's bundled files.

    Relevance retrieval — the file_search/RAG idea in adk-cc's grep-native
    form. Instead of paging linearly through a large reference, the model
    searches for a substring and gets matching file/line locations + the line
    text, then `load_skill_resource(offset=...)` the exact slice it needs.

    LITERAL (case-insensitive) substring, NOT regex: an arbitrary model-
    supplied regex over file contents is a ReDoS vector that could pin a CPU
    indefinitely. The blocking walk + reads run in a worker thread (off the
    event loop), skip files over the read cap, and skip binaries.
    """

    def __init__(self, skill_dirs: dict[str, str]) -> None:
        super().__init__(
            name="search_skill_resource",
            description=(
                "Searches a skill's bundled files (references/assets/scripts "
                "and root) for a case-insensitive SUBSTRING; returns matching "
                "file paths + line numbers + line text. Use to locate the "
                "relevant part of a large resource, then "
                "load_skill_resource(offset=...) it."
            ),
        )
        self._skill_dirs = skill_dirs

    def _get_declaration(self) -> types.FunctionDeclaration | None:
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "The skill to search."},
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring to find (literal, not regex).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max matches to return (default 30).",
                    },
                },
                "required": ["skill_name", "query"],
            },
        )

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        skill_name = args.get("skill_name") or ""
        query = args.get("query") or ""
        max_results = _coerce_int(args.get("max_results"), 30)
        skill_dir = self._skill_dirs.get(skill_name)
        if not skill_dir:
            return {"error": f"Skill '{skill_name}' not found.", "error_code": "SKILL_NOT_FOUND"}
        if not query:
            return {"error": "Argument 'query' is required.", "error_code": "INVALID_ARGUMENTS"}
        base = Path(skill_dir).resolve()
        if not base.is_dir():
            return {"error": f"Skill '{skill_name}' not found.", "error_code": "SKILL_NOT_FOUND"}
        # Blocking filesystem walk + reads → run off the asyncio event loop.
        matches, truncated = await asyncio.to_thread(
            self._search_sync, base, query, max_results
        )
        return {
            "skill_name": skill_name,
            "query": query,
            "matches": matches,
            "total_returned": len(matches),
            "truncated": truncated,
        }

    @staticmethod
    def _search_sync(
        base: Path, query: str, max_results: int
    ) -> tuple[list[dict], bool]:
        needle = query.lower()
        read_cap = _resource_read_max_bytes()
        matches: list[dict] = []
        truncated = False
        try:
            for fp in sorted(base.rglob("*")):
                if "__pycache__" in fp.parts or not fp.is_file():
                    continue
                try:
                    if fp.stat().st_size > read_cap:
                        continue  # skip files over the read cap (memory bound)
                    text = fp.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue  # binary / unreadable
                rel = str(fp.relative_to(base))
                for i, line in enumerate(text.splitlines(), start=1):
                    if needle in line.lower():
                        if len(matches) >= max_results:
                            truncated = True
                            break
                        matches.append({
                            "file_path": rel,
                            "line": i,
                            "text": line[:_MAX_LINE_LENGTH].strip(),
                        })
                if truncated:
                    break
        except OSError:
            pass
        return matches, truncated


class _NoopGuardedRunSkillScriptTool(RunSkillScriptTool):
    """`run_skill_script` that refuses under the noop backend (host exec).

    Phase-2 guard (only installed when ADK_CC_SKILL_GUARDS=1): under noop a
    skill's script runs on the HOST. Refuse unless explicitly acknowledged
    with ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC=1. Mirrors how the artifact tools
    gate on the noop backend.
    """

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        if os.environ.get("ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC") != "1":
            from ..sandbox import get_backend, is_noop_backend

            try:
                backend = get_backend(tool_context)
            except Exception:
                backend = None
            if backend is not None and is_noop_backend(backend):
                return {
                    "error": (
                        "run_skill_script is disabled under the noop backend — "
                        "the script would execute on the host. Configure a real "
                        "sandbox (ADK_CC_SANDBOX_BACKEND=docker|daytona|...) or "
                        "set ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC=1 to override."
                    ),
                    "error_code": "SANDBOX_REQUIRED",
                }
        return await super().run_async(args=args, tool_context=tool_context)


def _patch_skill_tools(
    toolset: SkillToolset, skill_dirs: dict[str, str]
) -> None:
    """Swap ADK's skill tools for adk-cc's bounded/guarded variants in-place.

    `SkillToolset._tools` is a regular list built in `__init__`. We replace:
      - LoadSkillResourceTool → _LenientLoadSkillResourceTool (bounded + disk
        fallback)
      - LoadSkillTool         → _BoundedLoadSkillTool (caps instructions)
      - RunSkillScriptTool    → _NoopGuardedRunSkillScriptTool (only when
        ADK_CC_SKILL_GUARDS=1)
    Idempotent: already-swapped subclasses are skipped.
    """
    guards = _guards_on()
    for i, tool in enumerate(toolset._tools):
        if isinstance(tool, LoadSkillResourceTool) and not isinstance(
            tool, _LenientLoadSkillResourceTool
        ):
            toolset._tools[i] = _LenientLoadSkillResourceTool(toolset, skill_dirs)
        elif isinstance(tool, LoadSkillTool) and not isinstance(
            tool, _BoundedLoadSkillTool
        ):
            toolset._tools[i] = _BoundedLoadSkillTool(toolset)
        elif (
            guards
            and isinstance(tool, RunSkillScriptTool)
            and not isinstance(tool, _NoopGuardedRunSkillScriptTool)
        ):
            toolset._tools[i] = _NoopGuardedRunSkillScriptTool(toolset)


def make_skill_toolset(
    *,
    skills_dir: Optional[Path] = None,
    code_executor: Optional[BaseCodeExecutor] = None,
    script_timeout: int = 300,
) -> Optional[SkillToolset]:
    """Build a SkillToolset from discovered skills, or None if empty.

    With `skills_dir=None`, runs the full multi-source aggregation
    (`_resolve_skills_dirs()` → operator env var, project walk-up,
    install fallback). With `skills_dir=<Path>`, scans only that
    one directory — backward-compat for tests passing a fixed dir.

    Returning None lets `agent.py` skip adding the toolset entirely
    when no skills are configured — keeps the coordinator's tool
    surface deterministic in the empty case.
    """
    if skills_dir is not None:
        pairs = _load_skills_from_dir(skills_dir)
    else:
        pairs = discover_skills_with_sources()
    if not pairs:
        return None
    # Lazy/memory guard: keep oversized references/assets OUT of RAM; the
    # bounded disk-fallback serves them on demand (scripts are left intact —
    # run_skill_script executes them from memory).
    max_bytes = _file_max_bytes()
    for skill, _ in pairs:
        _prune_oversized_resources(skill, max_bytes)
    skill_dirs = _build_skill_dir_index(pairs)
    if code_executor is None:
        # Lazy import keeps `tools/skills.py` importable in tests that
        # don't need the sandbox layer. The executor reads the active
        # backend from session state at call time.
        from ..sandbox.code_executor import SandboxBackedCodeExecutor

        code_executor = SandboxBackedCodeExecutor()
    toolset = SkillToolset(
        skills=[s for s, _ in pairs],
        code_executor=code_executor,
        script_timeout=script_timeout,
    )
    # Phase 1.5: always-on grep-within-resource retrieval tool. Appended to
    # `_tools` directly — `additional_tools=` would gate it behind a skill's
    # adk_additional_tools metadata (it lands in _provided_tools_by_name).
    toolset._tools.append(_SkillResourceSearchTool(skill_dirs))
    _patch_skill_tools(toolset, skill_dirs)
    return toolset
