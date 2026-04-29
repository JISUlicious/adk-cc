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
`SkillToolset` ready to drop into the coordinator's `tools=[...]`.

Skill scripts execute under the `code_executor` you pass. For a multi-
tenant deployment, plug in a code_executor that delegates to the active
`SandboxBackend`. Until that wiring is in place, scripts run via ADK's
default executor (host-side); pair with the permission engine if that
matters for your deployment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.skills import (
    Skill,
    list_skills_in_dir,
    load_skill_from_dir,
)
from google.adk.tools.skill_toolset import SkillToolset


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
    skills = discover_skills(skills_dir)
    if not skills:
        return None
    return SkillToolset(
        skills=skills,
        code_executor=code_executor,
        script_timeout=script_timeout,
    )
