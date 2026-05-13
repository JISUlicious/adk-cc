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

import logging
import os
from pathlib import Path
from typing import Any, Optional

from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.skills import (
    Skill,
    list_skills_in_dir,
    load_skill_from_dir,
)
from google.adk.tools.skill_toolset import LoadSkillResourceTool, SkillToolset
from google.adk.tools.tool_context import ToolContext

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
            "Loads a resource file from within a skill. Canonical paths "
            "start with 'references/', 'assets/', or 'scripts/'. As a "
            "fallback, files at the skill root (e.g. 'README.md', "
            "'pptxgenjs.md') can also be accessed — pass either the "
            "bare filename or the full relative path."
        )
        self._skill_dirs = skill_dirs

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        result = await super().run_async(args=args, tool_context=tool_context)
        if not isinstance(result, dict):
            return result
        if result.get("error_code") not in (
            "RESOURCE_NOT_FOUND",
            "INVALID_RESOURCE_PATH",
        ):
            return result

        skill_name = args.get("skill_name") or ""
        file_path = args.get("file_path") or ""
        fallback = self._scan(skill_name, file_path)
        if fallback is not None:
            return fallback
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
            return path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None


def _patch_load_skill_resource(
    toolset: SkillToolset, skill_dirs: dict[str, str]
) -> None:
    """Replace ADK's strict LoadSkillResourceTool with the lenient one.

    SkillToolset's `_tools` is a regular list constructed in `__init__`;
    we swap the strict tool out for our subclass. No behavioral change
    if `skill_dirs` is empty (the fallback finds nothing to scan).
    """
    for i, tool in enumerate(toolset._tools):
        if isinstance(tool, LoadSkillResourceTool) and not isinstance(
            tool, _LenientLoadSkillResourceTool
        ):
            toolset._tools[i] = _LenientLoadSkillResourceTool(toolset, skill_dirs)
            return


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
    _patch_load_skill_resource(toolset, _build_skill_dir_index(pairs))
    return toolset
