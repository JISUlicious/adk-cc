"""Skill loading and toolset construction.

Skills are operator-defined parameterized prompts (Anthropic's skill
format) that adk-cc surfaces as tools the coordinator can invoke.

ADK ships:
  - `google.adk.skills.list_skills_in_dir(path)` → {name: Frontmatter}
  - `google.adk.skills.load_skill_from_dir(skill_dir)` → Skill
  - `google.adk.tools.skill_toolset.SkillToolset(skills, code_executor=...,
    script_timeout=300, additional_tools=...)`

This module discovers skills under a directory (default:
`adk_cc/skills/` if it exists, else nothing), loads them, and returns a
`SkillToolset` with a lenient `load_skill_resource` that adds a
filesystem-scan fallback for skills that don't strictly follow the
references/scripts/assets layout.

Why the lenient tool: ADK's stock `LoadSkillResourceTool` only resolves
paths starting with `references/`, `assets/`, or `scripts/`. Anthropic's
own official skills repo sometimes places auxiliary docs at the skill
root (e.g. `pptx/pptxgenjs.md`, `pptx/editing.md`). The model
reasonably guesses `scripts/<file>.md` or `references/<file>.md` and
the strict tool returns RESOURCE_NOT_FOUND. The fallback scans the
real on-disk skill directory by basename, so the model's guess
resolves to the actual file regardless of which subfolder it picked.

Skill scripts execute under the `code_executor` you pass. For a multi-
tenant deployment, plug in a code_executor that delegates to the active
`SandboxBackend`. Until that wiring is in place, scripts run via ADK's
default executor (host-side); pair with the permission engine if that
matters for your deployment.
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


def _default_skills_dir() -> Optional[Path]:
    raw = os.environ.get("ADK_CC_SKILLS_DIR")
    if raw:
        p = Path(raw).expanduser().resolve()
        return p if p.is_dir() else None
    # Fallback: an `skills/` folder co-located with the agent module.
    here = Path(__file__).resolve().parent.parent / "skills"
    return here if here.is_dir() else None


def discover_skills(skills_dir: Optional[Path] = None) -> list[Skill]:
    """Load every skill under skills_dir. Empty list if no dir or no skills."""
    base = skills_dir if skills_dir is not None else _default_skills_dir()
    if base is None:
        return []
    out: list[Skill] = []
    for name in list_skills_in_dir(base).keys():
        skill_dir = base / name
        try:
            out.append(load_skill_from_dir(skill_dir))
        except Exception:
            # Skip malformed skills rather than refusing to start.
            continue
    return out


def _build_skill_dir_index(
    skills: list[Skill], base: Optional[Path]
) -> dict[str, str]:
    """Map skill_name → absolute on-disk skill directory.

    Used by `_LenientLoadSkillResourceTool` to scan the real filesystem
    when ADK's strict path lookup misses. Skipped silently if `base`
    can't be resolved (e.g. tests with synthesized Skill objects).
    """
    if base is None:
        return {}
    base_resolved = Path(base).resolve()
    out: dict[str, str] = {}
    for skill in skills:
        try:
            name = skill.frontmatter.name
        except Exception:
            continue
        candidate = (base_resolved / name).resolve()
        # Don't index a path outside the skills root — defense-in-depth
        # against pathologically named skills.
        try:
            candidate.relative_to(base_resolved)
        except ValueError:
            continue
        if candidate.is_dir():
            out[name] = str(candidate)
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
    """Build a SkillToolset from a directory of skills, or None if empty.

    Returning None lets `agent.py` skip adding the toolset entirely when
    no skills are configured — keeps the coordinator's tool surface
    deterministic in the empty case.
    """
    base = skills_dir if skills_dir is not None else _default_skills_dir()
    skills = discover_skills(base)
    if not skills:
        return None
    toolset = SkillToolset(
        skills=skills,
        code_executor=code_executor,
        script_timeout=script_timeout,
    )
    _patch_load_skill_resource(toolset, _build_skill_dir_index(skills, base))
    return toolset
