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
  2. **PROJECT** — `.adk-cc/skills/`, walked up from the SESSION's
     project root to `$HOME`, so a monorepo can share skills a level
     above the package. Needs a project root; skipped entirely when
     `ADK_CC_DISABLE_PROJECT_SKILLS=1`.
  3. **GLOBAL** — belongs to the install, applies to every project:
       - `<run dir>/.adk-cc/skills/` — the directory the server process
         runs in (in dev, the adk-cc checkout).
       - `<desktop data>/skills/` — e.g. `~/.adk-cc-desktop/skills/`.
     Exact locations, no walk-up, so "global" stays predictable.
  4. **BUILT-IN** `<install>/adk_cc/skills/` — the dir co-located with
     the agent module. ALWAYS included (a base layer, not a fallback):
     a project/global/env skill of the same name wins by the
     first-found rule above, so users override built-ins by name
     without losing the rest. Disable with `ADK_CC_BUILTIN_SKILLS=0`.

`.claude/skills/` is NOT a source. It was accepted as project scope
(first-existing-wins per walked dir, alongside `.adk-cc/skills/`), which
conflated a Claude Code folder with an adk-cc project scope. Scopes are
now explicit and `.adk-cc/skills/` is what "project skill" means.

The run dir being GLOBAL rather than project is the point of the split:
resolving it as project scope meant a desktop user got whatever sat above
wherever the app was launched from — for every project, and instead of
their own project's skills.

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
import contextvars
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.code_executors.code_execution_utils import CodeExecutionInput
from google.adk.skills import (
    Skill,
    list_skills_in_dir,
    load_skill_from_dir,
)
from google.adk.skills.models import Frontmatter as _FrontmatterModel
from google.adk.skills.models import Resources as _ResourcesModel
from google.adk.skills.models import Script as _ScriptModel
from google.adk.skills.models import Skill as _SkillModel
from google.adk.tools.base_tool import BaseTool
from google.adk.tools import skill_toolset as _adk_skill_toolset
from google.adk.tools.skill_toolset import (
    ListSkillsTool,
    LoadSkillResourceTool,
    LoadSkillTool,
    RunSkillScriptTool,
    SkillToolset,
    _SkillScriptCodeExecutor,
)
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from ..branding import NOTE_PREFIX
from ..config.schema import env_bool
from . import skill_deps, skill_enablement, skill_trust, skill_usage

_log = logging.getLogger(__name__)


# The one project-scoped location. `.claude/skills` used to be accepted here
# too (first-existing-wins per walked directory); scopes are now explicit and
# `.adk-cc/skills` is what "project skill" means, so a Claude Code skills folder
# is no longer picked up as one.
_PROJECT_SKILLS_SUBDIR = ".adk-cc/skills"

# The cross-client convention from the Agent Skills implementer guide: a client
# should scan its own directory AND `.agents/skills/`, so a skill installed by
# any compliant agent is visible to the others. Deliberately NOT `.claude`,
# which is one vendor's private location — this is the shared one.
_INTEROP_SKILLS_SUBDIR = ".agents/skills"
_INTEROP_HOME_SUBDIR = ".agents/skills"

# GLOBAL locations — tied to the adk-cc INSTALL, not to any bound project:
#   * `<run dir>/.adk-cc/skills` — the directory the server process runs in. In
#     dev that is the adk-cc checkout; whatever project happens to be the run
#     dir, its skills apply everywhere, which is what makes them global rather
#     than that project's.
#   * `<desktop data>/skills`   — e.g. `~/.adk-cc-desktop/skills`.
_GLOBAL_RUN_DIR_SUBDIR = ".adk-cc/skills"
_GLOBAL_DATA_SUBDIR = "skills"


def _resolve_skills_dirs(project_root: Optional[Path] = None) -> list[Path]:
    """Ordered list of skills directories to scan. First-found skill
    name wins across all returned dirs.

    Four scopes, most specific first:

      1. `ADK_CC_SKILLS_DIR` — operator explicit, wins over everything.
      2. PROJECT — `.adk-cc/skills` walked up from `project_root` to
         `$HOME` (so a monorepo can share skills a level above the package).
         Only meaningful with a project root; skipped entirely when
         `ADK_CC_DISABLE_PROJECT_SKILLS=1`.
      3. GLOBAL — `<run dir>/.adk-cc/skills` and `<desktop data>/skills`.
         These belong to the INSTALL: the run dir's skills apply to every
         project, which is what makes them global rather than that
         directory's own. No walk-up — exact locations, so "global" stays
         predictable.
      4. BUILT-INS — `<install>/adk_cc/skills`, always the base layer unless
         `ADK_CC_BUILTIN_SKILLS=0`.

    The scope split matters because the run dir used to be read as PROJECT
    scope: anchoring the walk-up at the cwd meant a desktop user got whatever
    sat above wherever the app was launched from, for every project, and never
    their own project's skills.

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

    # 2. PROJECT — walk up from the bound project only, and ONLY once the user
    # has trusted the folder. A project's skills arrive with the repository, so
    # a clone can otherwise inject instructions into the agent's context; both
    # the implementer guide and Anthropic's docs call this out. What gets
    # skipped is recorded below so it can be offered rather than lost.
    if project_root is not None and not env_bool("ADK_CC_DISABLE_PROJECT_SKILLS"):
        try:
            cursor = Path(project_root).resolve()
        except OSError:
            cursor = None
        if cursor is not None:
            home = Path.home()
            untrusted: list[Path] = []
            while True:
                # Trust belongs to the folder the skills LIVE IN, not to
                # wherever the walk started. Testing it ONCE against the
                # workspace root withheld a trusted repo's own skills whenever
                # the workspace sat BELOW that repo: desktop's workspace IS the
                # repo (trusted -> loads), while web/daytona's is
                # <repo>/.temp/<tenant>/<user> (never trusted -> the walk never
                # ran, and the log blamed a scratch dir for skills that live in
                # the repo). Reported live; desktop and web disagreed on the
                # same session.
                if skill_trust.is_trusted(cursor):
                    _add(cursor / _PROJECT_SKILLS_SUBDIR)
                    # …and the cross-client location at the same level, AFTER
                    # ours, so a skill that exists in both is adk-cc's.
                    if not env_bool("ADK_CC_DISABLE_INTEROP_SKILLS"):
                        _add(cursor / _INTEROP_SKILLS_SUBDIR)
                elif _has_project_skills(cursor):
                    untrusted.append(cursor)
                if cursor == home or cursor == cursor.parent:
                    break
                cursor = cursor.parent
            # Default-deny is unchanged: an untrusted folder contributes
            # nothing. What changes is WHO gets named — the folder holding the
            # skills, so "trust this" can point at the repo the user knows.
            for d in untrusted:
                _note_untrusted(d)

    # 3. GLOBAL — the install's own skills, wherever it runs and stores data.
    try:
        _add(Path.cwd() / _GLOBAL_RUN_DIR_SUBDIR)
    except OSError:
        pass
    try:
        from .. import deployment

        _add(Path(deployment.data_dir()) / _GLOBAL_DATA_SUBDIR)
    except Exception:  # noqa: BLE001 — no data dir configured (bare tests)
        pass
    # User-level cross-client skills, the counterpart to the project-level
    # `.agents/skills` above. Last of the global sources: an adk-cc install's
    # own skills win over one another agent dropped in the home directory.
    if not env_bool("ADK_CC_DISABLE_INTEROP_SKILLS"):
        try:
            _add(Path.home() / _INTEROP_HOME_SUBDIR)
        except (OSError, RuntimeError):
            pass

    # 3. Built-in skills — a base layer, always added (never a "fallback"):
    # higher-precedence sources override BY NAME via the first-found rule,
    # so a project skill shadows one built-in without hiding the others.
    if env_bool("ADK_CC_BUILTIN_SKILLS", True):
        here = Path(__file__).resolve().parent.parent / "skills"
        _add(here)

    return dirs


# project root → the skill dirs withheld from it, and what is in them.
_UNTRUSTED: dict[str, list[str]] = {}


def _note_untrusted(project_root: Path) -> None:
    """Record project skill dirs skipped for want of trust, with their names.

    Recorded rather than merely skipped: the whole point of this area is that a
    skill which silently is not there is the hardest failure to diagnose. The
    user should be offered the choice, not left wondering.
    """
    found = _project_skill_names(project_root)
    _UNTRUSTED[str(Path(project_root))] = found
    if found:
        _log.info("skills: %d project skill(s) in %s withheld until the folder "
                  "is trusted: %s", len(found), project_root, ", ".join(found))


def untrusted_project_skills() -> dict[str, list[str]]:
    """`{project_root: [skill names withheld]}` from the last discovery."""
    return {k: list(v) for k, v in _UNTRUSTED.items()}


def withheld_for(project_root: Optional[Path | str]) -> list[str]:
    """Skill names this project ships that are withheld for want of trust.

    Asked directly rather than read out of the discovery cache: the settings
    panel is often the FIRST thing to ask about a freshly opened project, and
    at that point nothing has resolved its skills yet — the banner simply never
    appeared. A question with an answer that does not depend on what happened
    to run earlier is the right shape here.
    """
    if project_root is None:
        return []
    # F2: answer for the whole ANCESTRY, not just this folder. A workspace
    # nested under the repo (web/daytona: <repo>/.temp/<tenant>/<user>) ships
    # no skills of its own — the ones being withheld live further up, and
    # naming the scratch dir sent the user to trust a folder they had never
    # seen while the repo stayed untrusted.
    out: list[str] = []
    try:
        cursor = Path(project_root).resolve()
    except OSError:
        return []
    home = Path.home()
    while True:
        if not skill_trust.is_trusted(cursor):
            names = _project_skill_names(cursor)
            if names:
                _note_untrusted(cursor)
                out += names
        if cursor == home or cursor == cursor.parent:
            break
        cursor = cursor.parent
    return out


def _project_skill_names(folder) -> list[str]:  # noqa: ANN001
    """Skill names directly under `folder`'s skills dirs — no walk-up."""
    out: list[str] = []
    for sub in (_PROJECT_SKILLS_SUBDIR, _INTEROP_SKILLS_SUBDIR):
        d = Path(folder) / sub
        if _is_dir_silently(d):
            try:
                out += [q.name for q in sorted(d.iterdir())
                        if (q / "SKILL.md").is_file()]
            except OSError:
                pass
    return out


def _has_project_skills(folder) -> bool:  # noqa: ANN001
    return bool(_project_skill_names(folder))


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


# A binary re-attached to a skill is embedded into the launcher payload on
# every run of that skill's scripts, so a huge one is a per-invocation cost
# (worse over SSH/Daytona than locally). Real ones are small — the tarball that
# exposed this is 20 KB — so cap and say what was left out.
_MAX_BINARY_RESOURCE_BYTES = 2 * 1024 * 1024


# ADK's own cap (`Frontmatter._validate_description`), restated because the
# repair below has to know the target length.
_MAX_DESCRIPTION_CHARS = 1024

_UNLOADABLE: dict[str, dict[str, str]] = {}

# name → [{severity, message}] for skills that DID load. `warning` means a spec
# breach adk-cc repaired or tolerated; `advice` is guidance aimed at the skill's
# author. Kept apart from _UNLOADABLE so the panel can show three states rather
# than "works" / "gone".
_DIAGNOSTICS: dict[str, list[dict[str, str]]] = {}


def skill_diagnostics() -> dict[str, list[dict[str, str]]]:
    """Per-skill notes from the most recent discovery."""
    return {k: [dict(d) for d in v] for k, v in _DIAGNOSTICS.items()}


def _note_unloadable(name: str, skill_dir: Path, reason: str) -> None:
    """Remember a skill that is installed but could not be loaded.

    Silence here is the worst outcome: the skill is on disk, absent from the
    catalogue, and nothing anywhere says why. Measured on a published skill —
    `claude-api` ships a description over ADK's 1024-character limit, so it was
    dropped with a warning on the ROOT logger and never appeared again.
    """
    short = " ".join(str(reason).split())
    if len(short) > 300:
        short = short[:300] + "…"
    _UNLOADABLE[name] = {"name": name, "dir": str(skill_dir), "reason": short}
    _log.warning("skills: '%s' in %s could not be loaded: %s",
                 name, skill_dir, short)


def unloadable_skills() -> list[dict[str, str]]:
    """Installed skills that failed to load, most recent discovery wins.

    Exposed so the skills UI can show an installed-but-broken skill instead of
    leaving the user to wonder where it went.
    """
    return [dict(v) for v in _UNLOADABLE.values()]


# The Agent Skills spec's hard limits, restated because the lenient loader has
# to know what it is repairing.
_MAX_NAME_CHARS = 64
_MAX_COMPATIBILITY_CHARS = 500
# Body-size guidance from the spec — recommendations to AUTHORS, never enforced
# here. `claude-api` ships ~18k tokens and works; trimming to these would break
# a published skill. They exist only to produce advice.
_ADVISORY_BODY_LINES = 500
_ADVISORY_BODY_TOKENS = 5000

# Fields the spec defines. Anything else is a vendor extension — Claude Code
# alone defines a dozen (`when_to_use`, `user-invocable`, `paths`, …). The
# reference validator calls an unknown field an ERROR; here it is at most a
# note, because rejecting them would reject most skills written for any
# specific client.
_SPEC_FIELDS = frozenset({
    "name", "description", "license", "allowed-tools", "allowed_tools",
    "metadata", "compatibility",
})

_UNQUOTED_COLON_RE = re.compile(r"^(\s*)([A-Za-z_-]+):\s+(.*:.*)$")


def _parse_frontmatter(text: str) -> Optional[dict]:
    """YAML frontmatter, with the guide's recovery for the common malformation.

    The implementer guide names it: a value containing an unquoted colon —
    `description: Use this skill when: the user asks about PDFs` — is invalid
    YAML that some clients' parsers happen to accept. Quote it and retry rather
    than lose the skill.
    """
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    for attempt in (0, 1):
        block = parts[1]
        if attempt:
            fixed = []
            for line in block.splitlines():
                m = _UNQUOTED_COLON_RE.match(line)
                if m and not m.group(3).lstrip().startswith(("'", '"', "|", ">")):
                    val = m.group(3).replace('"', '\\"')
                    fixed.append(f'{m.group(1)}{m.group(2)}: "{val}"')
                else:
                    fixed.append(line)
            block = "\n".join(fixed)
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _lenient_skill(skill_dir: Path) -> tuple[Optional[Skill], list[dict[str, str]]]:
    """Load a skill ADK rejected, the way the standard tells clients to.

    ADK enforces the spec strictly and refuses the whole skill on any breach.
    The Agent Skills implementer guide prescribes the opposite for a client:

        name doesn't match the parent directory  → warn, load anyway
        name exceeds 64 characters               → warn, load anyway
        description missing or empty             → skip, log the error
        YAML completely unparseable              → skip, log the error
        "record diagnostics … but don't block skill loading on cosmetic issues"

    A folder renamed during install is the commonest real-world breakage and it
    cost the whole skill. Returns `(skill_or_None, diagnostics)`; the caller
    reports the diagnostics whether or not the load succeeded.
    """
    diags: list[dict[str, str]] = []

    def note(severity: str, message: str) -> None:
        diags.append({"severity": severity, "message": message})

    md = skill_dir / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8")
    except OSError as e:
        note("error", f"SKILL.md could not be read: {e}")
        return None, diags

    data = _parse_frontmatter(text)
    if data is None:
        # Fatal per the guide: without frontmatter there is nothing to disclose.
        note("error", "frontmatter is missing or could not be parsed as YAML")
        return None, diags
    body = text.split("---", 2)[2]

    name = str(data.get("name") or "").strip()
    if not name:
        name = skill_dir.name
        note("warning", f"no name in frontmatter; using the directory name "
                        f"'{skill_dir.name}'")
    if name != skill_dir.name:
        note("warning", f"name '{name}' does not match the directory "
                        f"'{skill_dir.name}' — the spec requires them to match")
    if len(name) > _MAX_NAME_CHARS:
        note("warning", f"name is {len(name)} characters (limit "
                        f"{_MAX_NAME_CHARS})")
    if name != name.lower() or not all(c.isalnum() or c == "-" for c in name):
        note("warning", f"name '{name}' is not lowercase letters, digits and "
                        "hyphens")
    if name.startswith("-") or name.endswith("-") or "--" in name:
        note("warning", f"name '{name}' has a leading, trailing or doubled "
                        "hyphen")

    desc = str(data.get("description") or "").strip()
    if not desc:
        # Fatal per the guide: "a description is essential for disclosure".
        note("error", "description is missing or empty, so the model would "
                      "never know when to use this skill")
        return None, diags
    if len(desc) > _MAX_DESCRIPTION_CHARS:
        # Budget the marker BEFORE cutting: an earlier version reserved a
        # round 40 characters for a 52-character marker, so the "repaired"
        # description came out at 1036 and failed the very validator this
        # exists to satisfy — silently, since a failed repair just means no
        # repair.
        marker = " …(truncated to fit the catalogue limit)"
        note("warning", f"description is {len(desc)} characters (limit "
                        f"{_MAX_DESCRIPTION_CHARS}); truncated for the catalogue")
        desc = desc[: _MAX_DESCRIPTION_CHARS - len(marker)].rstrip() + marker

    compat = data.get("compatibility")
    if compat is not None and len(str(compat)) > _MAX_COMPATIBILITY_CHARS:
        note("warning", f"compatibility is {len(str(compat))} characters "
                        f"(limit {_MAX_COMPATIBILITY_CHARS}); truncated")
        data["compatibility"] = str(compat)[:_MAX_COMPATIBILITY_CHARS]

    try:
        # `model_construct` because the point is to bypass the validation that
        # rejected this skill in the first place; every constraint it would
        # have raised is already recorded above as a diagnostic.
        fm = _FrontmatterModel.model_construct(
            name=name, description=desc,
            license=data.get("license"),
            compatibility=data.get("compatibility"),
            allowed_tools=data.get("allowed-tools") or data.get("allowed_tools"),
            metadata=data.get("metadata") if isinstance(
                data.get("metadata"), dict) else {},
        )
        skill = _SkillModel(frontmatter=fm, instructions=body,
                            resources=_ResourcesModel())
        _attach_missing_resources(skill, skill_dir)
    except Exception as e:  # noqa: BLE001 — a repair that fails is just no repair
        note("error", f"could not be loaded even leniently: {type(e).__name__}")
        return None, diags

    diags.extend(_advisories(data, body))
    for d in diags:
        _log.warning("skills: '%s' in %s: %s", name, skill_dir, d["message"])
    return skill, diags


def _advisories(data: dict, body: str) -> list[dict[str, str]]:
    """Notes for the skill's AUTHOR: recommendations, never enforced.

    Kept strictly separate from the hard constraints above. The spec's body
    guidance is advice — two published skills exceed it and work — so this
    reports and stops there.
    """
    out: list[dict[str, str]] = []
    lines = body.count("\n")
    if lines > _ADVISORY_BODY_LINES:
        out.append({"severity": "advice", "message": (
            f"SKILL.md body is {lines} lines; the spec recommends under "
            f"{_ADVISORY_BODY_LINES}, since the whole body loads on activation")})
    approx = len(body) // 4
    if approx > _ADVISORY_BODY_TOKENS:
        out.append({"severity": "advice", "message": (
            f"body is roughly {approx} tokens; the spec recommends under "
            f"{_ADVISORY_BODY_TOKENS} — move detail into references/")})
    if data.get("allowed-tools") or data.get("allowed_tools"):
        out.append({"severity": "advice", "message": (
            "declares allowed-tools, which adk-cc does not honour; tool "
            "permissions come from your own settings")})
    unknown = sorted(k for k in data if k not in _SPEC_FIELDS)
    if unknown:
        out.append({"severity": "advice", "message": (
            f"frontmatter fields outside the Agent Skills spec, ignored here: "
            f"{', '.join(unknown)}")})
    return out


def _attach_missing_resources(skill: Skill, skill_dir: Path) -> None:
    """Re-attach files ADK's loader dropped because they are not UTF-8.

    `_load_dir` does `read_text(encoding="utf-8")` and skips whatever raises
    UnicodeDecodeError, for scripts, references AND assets alike. So a skill
    that ships a tarball, a font, an image or a sample .xlsx loses it silently,
    and the breakage surfaces much later inside the script as a missing file.

    Measured on a published skill: `web-artifacts-builder` ships
    `scripts/init-artifact.sh` next to `scripts/shadcn-components.tar.gz`, and
    running it through `run_skill_script` printed its own
    "❌ Error: shadcn-components.tar.gz not found in script directory".

    Both launchers materialise from `skill.resources`, and ADK's extraction
    loop already writes bytes with mode 'wb' — so putting the bytes back here
    fixes every path at once rather than at one of them.
    """
    for sub, store, wrap in (
        ("scripts", skill.resources.scripts, True),
        ("references", skill.resources.references, False),
        ("assets", skill.resources.assets, False),
    ):
        root = skill_dir / sub
        if not _is_dir_silently(root):
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(root))
            if rel in store:
                continue
            try:
                size = path.stat().st_size
                if size > _MAX_BINARY_RESOURCE_BYTES:
                    _log.warning(
                        "skills: '%s' ships %s/%s (%d bytes), too large to "
                        "attach — scripts needing it will not find it",
                        skill.frontmatter.name, sub, rel, size)
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            try:
                # Text stays text: `load_skill_resource` hands these to the
                # model, and only genuinely binary files should arrive as bytes.
                data: Any = raw.decode("utf-8")
            except UnicodeDecodeError:
                data = raw
            if not wrap:
                store[rel] = data
            elif isinstance(data, str):
                store[rel] = _ScriptModel(src=data)
            else:
                # `Script.src` is annotated `str`, so bytes cannot go through
                # validation — and ADK's materialiser already branches on
                # `isinstance(content, bytes)` to write mode 'wb'. Construct
                # without validation rather than lose the file, which is what
                # happened first: pydantic raised and the tarball stayed gone.
                store[rel] = _ScriptModel.model_construct(src=data)


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

    # Directories that hold a SKILL.md but did not survive validation. ADK
    # filters them out inside `list_skills_in_dir` with a warning on the root
    # logger, so without this they are invisible to us and to the user.
    rejected: list[str] = []
    try:
        for d in sorted(base.iterdir()):
            if (d.is_dir() and d.name not in names
                    and (d / "SKILL.md").is_file()):
                rejected.append(d.name)
    except OSError:
        pass

    for name in names + rejected:
        skill_dir = (base / name).resolve()
        _UNLOADABLE.pop(name, None)
        _DIAGNOSTICS.pop(name, None)
        try:
            skill = load_skill_from_dir(skill_dir)
        except Exception as exc:  # noqa: BLE001
            # ADK enforces the spec strictly; the standard tells CLIENTS to be
            # lenient and record diagnostics instead.
            skill, diags = _lenient_skill(skill_dir)
            if skill is None:
                # Only the two cases the guide keeps fatal reach here.
                reason = next((d["message"] for d in diags
                               if d["severity"] == "error"), str(exc))
                _note_unloadable(name, skill_dir, reason)
                continue
            _DIAGNOSTICS[skill.frontmatter.name] = diags
        try:
            _attach_missing_resources(skill, skill_dir)
        except Exception:  # noqa: BLE001 — never let this cost us the skill
            _log.warning("skills: could not attach binaries for '%s'", name)
        out.append((skill, skill_dir))
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
    return env_bool("ADK_CC_SKILL_GUARDS")


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
            "A skill's bundled files (references/, assets/, scripts/) live WITH "
            "the skill, NOT in your workspace or execution sandbox — you cannot "
            "reach them with read_file/run_bash or by any filesystem path; read "
            "them ONLY through this tool, by skill name + the file's relative "
            "path within the skill. "
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
        name = args.get("skill_name") or ""
        if name and _denied(name, tool_context):
            return skill_enablement.refusal(name)
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
        skill_dir = _skill_dir_for(skill_name, self._skill_dirs)
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


def _invocation_id_of(ctx: Any) -> Optional[str]:
    """The turn's id, so offers are counted per TURN rather than per request."""
    try:
        inv = getattr(ctx, "_invocation_context", None)
        return str(getattr(inv, "invocation_id", "") or "") or None
    except Exception:  # noqa: BLE001
        return None


def _already_active(name: str, tool_context: ToolContext) -> bool:
    """Has this session already loaded this skill?

    Tracked in SESSION state rather than in the process: two sessions are two
    contexts, and a skill loaded in one is not in the other's. Marks as a side
    effect — the caller is about to load it.
    """
    if not name:
        return False
    try:
        state = tool_context.state
        key = "temp:skills_loaded"
        seen = list(state.get(key) or [])
        if name in seen:
            return True
        state[key] = seen + [name]
    except Exception:  # noqa: BLE001 — no writable state: never block a load
        return False
    return False


def _local_skill_dir(name: str, ctx: Any) -> Optional[str]:
    """The skill's REAL directory, but only when bash could actually reach it.

    Claude Code injects `Base directory for this skill: <dir>` into a loaded
    skill and lets the agent run its scripts in place (SkillTool.ts). That
    works because one filesystem holds both the skill and the shell. adk-cc
    only sometimes has that: a container / ssh / daytona workspace executes
    somewhere the skill folder does not exist, and a path that is real on the
    server but absent in the sandbox is WORSE than no path — it looks valid
    and fails later.

    So: return the dir for a local workspace, None otherwise.
    """
    try:
        from ..sandbox import get_workspace

        ws = get_workspace(ctx)
        if getattr(ws, "remote", False):
            return None
    except Exception:  # noqa: BLE001 — no workspace (tests, odd contexts)
        pass
    if not _executes_on_this_host():
        return None
    root = _ACTIVE_PROJECT_ROOT.get()
    resolved = _skills_for_root(root) if root else None
    # Per-skill and per-PROJECT: shadowing is already settled by discovery, so
    # `greeter` resolves to the one directory this session was offered — the
    # project's copy, not the built-in it shadows.
    d = (resolved[1].get(name) if resolved else None) or _SKILL_DIRS.get(name)
    return d if d and os.path.isdir(d) else None


def _executes_on_this_host() -> bool:
    """True when run_bash runs on the machine holding the skill files."""
    backend = (os.environ.get("ADK_CC_SANDBOX_BACKEND") or "noop").strip().lower()
    return backend in ("", "noop", "host", "local")


# Filled by `_patch_skill_tools` so the load tool can name a skill's directory
# without threading the index through ADK's constructors.
_SKILL_DIRS: dict[str, str] = {}


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
        name = args.get("skill_name") or ""
        if name and _denied(name, tool_context):
            return skill_enablement.refusal(name)
        already = _already_active(name, tool_context)
        if not already:
            try:
                skill_usage.record_used(name)
            except Exception:  # noqa: BLE001 — never break a load for a counter
                _log.debug("skills: could not record use of '%s'", name)
        result = await super().run_async(args=args, tool_context=tool_context)
        if already and isinstance(result, dict) and result.get("instructions"):
            # The implementer guide's dedupe: these instructions are already in
            # this session's context, and re-injecting them pays their whole
            # token cost a second time to say nothing new.
            _log.info("load_skill: '%s' already active this session", name)
            return {
                "skill_name": name,
                "already_loaded": True,
                "instructions": (
                    f"{NOTE_PREFIX} '{name}' is already loaded in this conversation — "
                    "its instructions are above and still apply. Nothing was "
                    "re-sent. Use load_skill_resource for a specific file."),
            }
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
            # ${SKILL_DIR} lets a skill reference its own files, the way
            # Claude Code's ${CLAUDE_SKILL_DIR} does.
            base = _local_skill_dir(name, tool_context)
            if base:
                # A skill may reference its own files, like Claude Code's
                # ${CLAUDE_SKILL_DIR}.
                instr = instr.replace("${SKILL_DIR}", base)
            result["instructions"] = _wrap_untrusted(
                instr, f"{args.get('skill_name', '')}/SKILL.md")
            if base:
                # Carried as FIELDS rather than prepended to `instructions`:
                # that field is capped to stop a pathological SKILL.md dumping
                # unbounded text, and folding a header into it would inflate
                # the very thing being measured. The model reads tool results,
                # so a labelled field is at least as visible as inline prose.
                result["base_dir"] = base
                result["how_to_run_scripts"] = (
                    f"This skill's files are on this machine under {base} — "
                    f"`scripts/…` means `{base}/scripts/…`. Run one with "
                    f"run_skill_script(skill_name=\"{name}\", "
                    f"file_path=\"scripts/…\"), which also supplies the "
                    f"analysis interpreter and installs the skill's "
                    f"dependencies.")
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
        if skill_name and _denied(skill_name, tool_context):
            return skill_enablement.refusal(skill_name)
        query = args.get("query") or ""
        max_results = _coerce_int(args.get("max_results"), 30)
        skill_dir = _skill_dir_for(skill_name, self._skill_dirs)
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


def _state_of(tool_context: ToolContext) -> Any:
    """Session state behind a ToolContext, or an empty mapping.

    Enablement is resolved from state on EVERY skill call because the toolset
    itself is a process-wide singleton (agent.py builds it once at import), so
    there is nowhere else per-user to put it.
    """
    try:
        return tool_context.state
    except Exception:  # noqa: BLE001 — a missing state must not break a tool
        return {}


def _denied(name: str, tool_context: ToolContext) -> bool:
    try:
        return skill_enablement.is_disabled(name, _state_of(tool_context))
    except Exception:  # noqa: BLE001 — fail OPEN: a broken deny-list file
        _log.warning("skill enablement: check failed for %r", name, exc_info=True)
        return False


def _anchor_script_args(args: dict[str, Any], tool_context: ToolContext) -> dict[str, Any]:
    """Rewrite workspace-relative path arguments to absolute ones.

    ADK runs a skill script inside a fresh temp directory — its wrapper does
    `os.chdir(tempfile.TemporaryDirectory())` (skill_toolset.py) so the script
    cannot litter the workspace. The side effect is that EVERY relative path
    argument resolves against that temp dir: a live run passed
    `["index.html", "check.mjs"]` and got "page not found:
    /var/.../tmpXXXX/index.html", then spent a round trip on `realpath` to
    recover. It is not skill-specific — every data-analyst probe takes a data
    file path through the same door.

    Only rewritten when `<workspace>/<value>` EXISTS. That keeps the rule
    evidence-based rather than pattern-guessing: `data.csv` becomes absolute,
    while `defect_rate` (a column name), `10` and `--flag` are left exactly as
    given. The remaining ambiguity is a column named the same as a file in the
    workspace, which would be rewritten wrongly; a heuristic on "looks like a
    path" would have that problem far more often.

    Remote and containerized workspaces: the check uses the local filesystem, so
    a path that only exists on the remote is not rewritten and behaves as it does
    today. No regression, and absolute paths keep working everywhere.
    """
    try:
        from ..sandbox import get_workspace

        root = str(getattr(get_workspace(tool_context), "abs_path", "") or "")
    except Exception:  # noqa: BLE001 — no workspace: leave the args alone
        return args
    if not root or not os.path.isdir(root):
        return args

    def _fix(value: Any) -> Any:
        if not isinstance(value, str) or not value or value.startswith("-"):
            return value
        if os.path.isabs(value):
            return value
        candidate = os.path.join(root, value)
        return candidate if os.path.exists(candidate) else value

    out = dict(args)
    raw = out.get("args")
    if isinstance(raw, list):
        out["args"] = [_fix(v) for v in raw]
    elif isinstance(raw, dict):
        out["args"] = {k: _fix(v) for k, v in raw.items()}
    if isinstance(out.get("positional_args"), list):
        out["positional_args"] = [_fix(v) for v in out["positional_args"]]
    if isinstance(out.get("short_options"), dict):
        out["short_options"] = {k: _fix(v) for k, v in out["short_options"].items()}
    return out


# Extensions ADK itself can launch (`_build_wrapper_code` returns None for
# anything else, and the tool then reports "Supported types: .py, .sh, .bash").
_ADK_SCRIPT_EXTS = frozenset({"py", "sh", "bash"})

# What adk-cc adds. A skill is a folder of files, and there is no reason its
# author must write Python: the first skill to ship a Node runner could not be
# launched at all, and the workaround — a .py shim next to it — taxes every
# author for a limitation of the launcher.
#
# `argv` prefix per extension; the script path and args follow.
_EXTRA_INTERPRETERS: dict[str, tuple[str, ...]] = {
    "js": ("node",),
    "mjs": ("node",),
    "cjs": ("node",),
    # Type stripping rather than a toolchain: no tsc, no tsx to install.
    "ts": ("node", "--experimental-strip-types"),
    "ps1": ("pwsh", "-NoProfile", "-File"),
    "rb": ("ruby",),
    "pl": ("perl",),
    "php": ("php",),
    "lua": ("lua",),
    "R": ("Rscript",),
    "r": ("Rscript",),
}


def launchable_script_exts() -> frozenset[str]:
    """Extensions `run_skill_script` can launch (no leading dot).

    Exported so the "you ran a skill's script as a plain file" redirect in the
    bash tool keys off the same set — the two drifted the moment this one grew,
    and a skill shipping a `.ps1` would have failed with no hint at all.
    """
    return frozenset(_ADK_SCRIPT_EXTS | set(_EXTRA_INTERPRETERS))


def _script_ext(file_path: str) -> str:
    return file_path.rsplit(".", 1)[-1] if "." in (file_path or "") else ""


def _flatten_script_args(
    script_args: Any, short_options: Any, positional_args: Any
) -> list[str]:
    """argv tail, matching ADK's own conventions so the two launchers behave
    identically: a list is the complete argv; a dict becomes `--key value`;
    short options `-k value`; positionals after a `--` separator."""
    out: list[str] = []
    if isinstance(script_args, list):
        out.extend(str(v) for v in script_args)
        return out
    if isinstance(script_args, dict):
        for k, v in script_args.items():
            out.extend([f"--{k}", str(v)])
    if isinstance(short_options, dict):
        for k, v in short_options.items():
            out.extend([f"-{k}", str(v)])
    if isinstance(positional_args, list) and positional_args:
        out.append("--")
        out.extend(str(v) for v in positional_args)
    return out


class _EnablementCheckedRunSkillScriptTool(RunSkillScriptTool):
    """`run_skill_script` refuses a disabled skill, and anchors relative paths."""

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        # Bind the project root HERE, not only in process_llm_request: ADK's
        # confirmation-resume path (`request_confirmation.py`) re-invokes a
        # confirmed tool directly, with no model request in between — so the
        # contextvar was unset and a PROJECT skill's script came back
        # SKILL_NOT_FOUND the moment the user clicked Allow (measured live).
        _ACTIVE_PROJECT_ROOT.set(_root_of(tool_context))
        name = args.get("skill_name") or ""
        if name and _denied(name, tool_context):
            return skill_enablement.refusal(name)
        ext = _script_ext(str(args.get("file_path") or ""))
        if ext and ext not in _ADK_SCRIPT_EXTS and ext not in _EXTRA_INTERPRETERS:
            # Own the message: ADK's says "Supported types: .py, .sh, .bash",
            # which is no longer the truth for this tool.
            supported = sorted(_ADK_SCRIPT_EXTS | set(_EXTRA_INTERPRETERS))
            return {
                "error": (
                    f"Cannot run a '.{ext}' script. Supported: "
                    + ", ".join(f".{e}" for e in supported)
                    + ". Ship a launchable entrypoint beside it, or invoke it "
                    "from one."
                ),
                "error_code": "UNSUPPORTED_SCRIPT_TYPE",
            }
        args = _anchor_script_args(args, tool_context)
        return await super().run_async(args=args, tool_context=tool_context)


# Skills are materialised HERE, not in a temp dir: a script that creates files
# has to leave them somewhere real. Deliberately not under `.adk-cc/skills`,
# which is where a project's OWN skills live — a cache inside that tree would
# look like a skill.
_SKILL_RUNTIME_SUBDIR = ".adk-cc/skill-runtime"

_MODULE_MISSING_RE = re.compile(r"No module named '([^']+)'")


def _explain_missing_package(res: dict, skill: Skill) -> dict:
    """Name the dependency when a skill script dies on an import.

    Third-party skills declare their dependencies in prose (or a
    `requirements.txt` we do not install), so their scripts hit the analysis
    environment as it happens to be provisioned. Measured on Anthropic's
    published `pdf` skill: `extract_form_field_info.py` failed with a bare
    `ModuleNotFoundError: No module named 'pypdf'` at the end of a traceback —
    accurate, and easy to read as "this script is broken" rather than "this
    machine lacks one package".

    A SIBLING module missing is a different fault (materialisation), so it is
    left alone rather than mislabelled as a package.
    """
    err = res.get("stderr") or ""
    m = _MODULE_MISSING_RE.search(err)
    if not m:
        return res
    missing = m.group(1).split(".")[0]
    try:
        siblings = {n.rsplit("/", 1)[-1][:-3]
                    for n in skill.resources.list_scripts() if n.endswith(".py")}
    except Exception:  # noqa: BLE001
        siblings = set()
    if missing in siblings:
        return res
    # When the install ALREADY failed because the interpreter is read-only,
    # "run uv pip install and re-run" is advice that cannot work, and the model
    # dutifully retries it. Measured live: psycopg2 into
    # /usr/local/lib/python3.13/site-packages on a container rootfs. Say what
    # is actually true instead.
    read_only = "Read-only file system" in err
    if read_only:
        remedy = (
            f"the interpreter it runs under is on a READ-ONLY filesystem, so "
            f"`uv pip install {missing}` there cannot work — do not retry it. "
            f"Report the step as NOT RUN and say the environment needs "
            f"'{missing}': the operator must unset ADK_CC_ANALYSIS_ENV so a "
            f"writable virtualenv is provisioned in the workspace, or add the "
            f"package to the sandbox image.")
    else:
        remedy = (
            f"install it there (uv pip install {missing}) and re-run, or "
            f"report the step as NOT RUN.")
    return _with_compatibility({**res, "stderr": err.rstrip() + (
        f"\n\n[adk-cc] this script needs the '{missing}' package, which is not "
        f"installed in the environment skill scripts run in — {remedy} Do not "
        f"re-implement the script's job inline and present it as the script's "
        f"result.")}, skill)


def _with_compatibility(res: dict, skill: Skill) -> dict:
    """Append the skill's own `compatibility` note to an environment failure.

    The spec has a field for exactly this — "environment requirements: intended
    product, system packages, network access" — so when a script dies because
    something is not installed, the author has usually already said what it
    needs. Quoting them beats making the agent guess.
    """
    note = (getattr(skill.frontmatter, "compatibility", None) or "").strip()
    if not note or note in (res.get("stderr") or ""):
        return res
    return {**res, "stderr": (res.get("stderr") or "").rstrip() + (
        f"\n[adk-cc] the skill declares its requirements as: {note}")}


def _skill_files(skill: Skill) -> dict[str, Any]:
    """Every file of a skill, laid out the way the script expects to find it."""
    files: dict[str, Any] = {}
    try:
        res = skill.resources
        for n in res.list_scripts():
            scr = res.get_script(n)
            if scr is not None and scr.src is not None:
                files[f"scripts/{n}"] = scr.src
        for n in res.list_references():
            c = res.get_reference(n)
            if c is not None:
                files[f"references/{n}"] = c
        for n in res.list_assets():
            c = res.get_asset(n)
            if c is not None:
                files[f"assets/{n}"] = c
    except Exception:  # noqa: BLE001 — a skill with no resources is fine
        pass
    # ADK's resource API exposes exactly three categories (scripts,
    # references, assets), so ANY other directory a skill ships — `data/`
    # most commonly — never reached the sandbox, and a script doing
    # `open("data/x.csv")` failed with a missing file even though the file
    # was right there in the skill folder. Materialise the whole skill
    # directory: the layout is the skill author's choice, not ours.
    files.update(_extra_skill_files(skill, files))
    return files


# Never shipped into the workspace: VCS/venv/cache noise, and our own runtime
# dir if a skill folder happens to contain one.
# Headroom between the script's own timeout and the exec that hosts it. The
# wrapper materialises the whole skill (hundreds of KB) and starts an
# interpreter before the script's first line runs, so the outer budget has to
# cover more than the script itself.
_OUTER_TIMEOUT_MARGIN_S = 30

_MATERIALIZE_IGNORE = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".adk-cc", ".DS_Store",
}


def _extra_skill_files(skill: Skill, already: dict[str, Any]) -> dict[str, Any]:
    """Files in the skill folder that ADK's three categories do not cover.

    Bounded by the same per-file cap as resources so a skill shipping a large
    corpus cannot blow up the payload; oversized files are skipped with a
    warning rather than silently truncated (a half-written data file is worse
    than an absent one — the script would read it and be wrong)."""
    name = getattr(getattr(skill, "frontmatter", None), "name", "") or ""
    if not name:
        return {}
    base = _skill_dir_for(name, {})
    if not base:
        # No session index (out-of-band call, or a skill outside the active
        # project root): resolve from a fresh discovery, mirroring what the
        # resource lookup does rather than silently shipping nothing.
        try:
            base = _build_skill_dir_index(
                discover_skills_with_sources(_resolve_skills_dirs())).get(name)
        except Exception:  # noqa: BLE001
            base = None
    if not base:
        return {}
    out: dict[str, Any] = {}
    cap = _file_max_bytes()
    try:
        root = Path(base)
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            rel_parts = f.relative_to(root).parts
            if any(p in _MATERIALIZE_IGNORE for p in rel_parts):
                continue
            rel = "/".join(rel_parts)
            if rel in already or rel == "SKILL.md":
                continue          # already carried, or the manifest itself
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if cap and size > cap:
                _log.warning(
                    "skill %r: %s is %d bytes (over the %d cap) — not "
                    "materialised; the script will not find it",
                    name, rel, size, cap)
                continue
            try:
                out[rel] = f.read_bytes()
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — a odd skill dir must not break the run
        return out
    return out


def _files_digest(files: dict[str, Any]) -> str:
    """Content address for a materialised skill, so an edited skill re-writes
    itself and an unchanged one never does."""
    h = hashlib.sha256()
    h.update(b"v1\n")                       # bump when the layout changes
    for rel in sorted(files):
        content = files[rel]
        h.update(rel.encode("utf-8") + b"\0")
        h.update(content if isinstance(content, bytes) else content.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _skill_import_tiers(files: dict[str, Any]) -> list[str]:
    """Top-level modules the skill's Python scripts import.

    The sandbox executor sizes the analysis environment from the imports it can
    see in the code it is given. It used to read them out of ADK's wrapper,
    which embedded every script's source inline; now that a warm run ships no
    sources at all, the names travel as an explicit header instead. More
    accurate too — it no longer depends on `repr` escaping.
    """
    mods: set[str] = set()
    for rel, content in files.items():
        if not rel.endswith(".py") or not isinstance(content, str):
            continue
        for line in content.splitlines():
            line = line.strip()
            for prefix in ("import ", "from "):
                if line.startswith(prefix):
                    name = line[len(prefix):].split()[0].split(".")[0].strip(",")
                    if name.isidentifier():
                        mods.add(name)
    return sorted(mods)


class _WiderScriptCodeExecutor(_SkillScriptCodeExecutor):
    """Runs a skill's script from a durable directory in the WORKSPACE.

    This has to live on the EXECUTOR rather than on the tool: `run_skill_script`
    constructs a `_SkillScriptCodeExecutor` inside its own `run_async`, so an
    override on the tool is never reached (it silently wasn't — `.js` kept
    returning ADK's "Unsupported script type"). Installed by swapping the module
    attribute ADK reads.

    ADK materialises the WHOLE skill into a `TemporaryDirectory`, chdirs there,
    and runs the target with `runpy`. Three things follow from that, all
    measured against published skills:

      * anything the script CREATES is deleted when the call returns. The
        published `web-artifacts-builder` scaffolds a project — it cannot work
        at all under a temp cwd, and `cd "$PROJECT_NAME"` failed either way.
      * the payload is re-sent on every invocation. docx/pptx/xlsx carry
        ~1.1 MB of schemas each, which crosses the wire per call on a remote
        backend.
      * `runpy.run_path` does not put the script's own directory on `sys.path`,
        so sibling imports failed and needed a chdir hook to repair.

    So: materialise once into `.adk-cc/skill-runtime/<skill>/<digest>/` beside
    the workspace, run the script as a real SUBPROCESS with cwd = the
    workspace, and keep ADK's result shape. Running it as a subprocess is what
    retires the sibling-import hack — Python puts a script's own directory at
    `sys.path[0]` when it runs a file, which is exactly what was being
    simulated. A warm run ships a few hundred bytes.
    """

    async def execute_script_async(  # noqa: ANN201 — mirrors ADK's signature
        self, invocation_context, skill, file_path, script_args,
        short_options=None, positional_args=None,
    ):
        if not file_path.startswith("scripts/"):
            file_path = f"scripts/{file_path}"
        files = _skill_files(skill)
        digest = _files_digest(files)
        name = getattr(skill.frontmatter, "name", "skill") or "skill"
        cache = f"{_SKILL_RUNTIME_SUBDIR}/{re.sub(r'[^A-Za-z0-9._-]', '_', name)}/{digest}"
        argv = _flatten_script_args(script_args, short_options, positional_args)

        def _shaped(payload: dict) -> dict:
            stdout = payload.get("stdout", "")
            stderr = payload.get("stderr", "")
            rc = payload.get("returncode", 0)
            if rc != 0 and not stderr:
                stderr = f"Exit code {rc}"
            # ADK's own three-way status, kept identical so a caller cannot
            # tell the two launchers apart.
            if rc != 0 or (stderr and not stdout):
                status = "error"
            elif stderr:
                status = "warning"
            else:
                status = "success"
            shaped = _explain_missing_package({
                "skill_name": skill.name, "file_path": file_path,
                "stdout": stdout, "stderr": stderr, "status": status}, skill)
            # A missing INTERPRETER (returncode 127) is the other half of the
            # same question, and the wrapper cannot see the frontmatter.
            if rc == 127:
                shaped = _with_compatibility(shaped, skill)
            return shaped

        try:
            tiers = _skill_import_tiers(files)
            deps = skill_deps.collect_requirements(skill)
            # Ask first, carry nothing: if this workspace already holds the
            # skill at this digest, that one exchange also runs the script and
            # the payload never moves. An in-process "already materialised" set
            # would save only the cold round trip, and keying it is exactly
            # where the first attempt went wrong — `id(self._base_executor)`
            # changes per call, so every run looked cold and shipped 1.1 MB.
            res = await self._exec(invocation_context, self._wrapper(
                cache, file_path, argv, None, tiers, deps))
            if res.get("__needs_materialize__"):
                res = await self._exec(invocation_context, self._wrapper(
                    cache, file_path, argv, files, tiers, deps))
        except Exception as e:  # noqa: BLE001 — same shape as ADK's catch
            _log.exception("skill script '%s' of '%s' failed", file_path, name)
            return {"error": f"Failed to execute script '{file_path}':"
                             f" {type(e).__name__}: {str(e)[:200]}",
                    "error_code": "EXECUTION_ERROR"}
        if res.get("__error__"):
            return {"error": f"Failed to execute script '{file_path}':"
                             f" {res['__error__'][:400]}",
                    "error_code": "EXECUTION_ERROR"}
        return _shaped(res)

    async def _exec(self, invocation_context, code: str) -> dict:
        """Run wrapper code through the configured executor, return its payload."""
        result = await asyncio.to_thread(
            self._base_executor.execute_code,
            invocation_context,
            CodeExecutionInput(code=code),
        )
        out = (result.stdout or "").strip()
        for line in reversed(out.splitlines()):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and (
                    parsed.get("__shell_result__") or parsed.get("__needs_materialize__")):
                return parsed
        # No envelope at all means the wrapper itself did not run — a sandbox
        # or environment failure, which must not be reported as the script's
        # own empty output.
        return {"__error__": (result.stderr or out or "no output from the "
                              "skill-script launcher")}

    def _wrapper(self, cache: str, file_path: str, argv: list[str],
                 files: Optional[dict[str, Any]], tiers: list[str],
                 deps: Optional[list[str]] = None) -> str:
        """The code that runs in the sandbox.

        Sent without `files` first: if this workspace already has the skill at
        this digest, that run needs nothing else and the 1.1 MB never moves.
        Otherwise it reports back and the caller re-sends with the payload.
        """
        ext = _script_ext(file_path)
        interp = list(_EXTRA_INTERPRETERS.get(ext, ()))
        if ext in ("sh", "bash"):
            interp = ["bash"]
        timeout = getattr(self, "_script_timeout", 300)
        # Never let the inner timeout exceed the outer one: whichever fires
        # first decides what the user sees, and only the inner can report the
        # script's partial output. Applies when a caller supplies their own
        # (tighter) executor — the one built here is already sized above.
        outer = getattr(getattr(self, "_base_executor", None),
                        "timeout_seconds", None)
        if isinstance(outer, int) and outer > 0:
            timeout = max(5, min(timeout, outer - _OUTER_TIMEOUT_MARGIN_S))
        return "\n".join([
            # Read by the sandbox executor to size the analysis environment.
            f"# adk-cc-skill-tiers: {' '.join(tiers)}",
            "import os, sys, json as _json, shutil, subprocess",
            # This runs as its OWN process, so every name the generated code
            # uses has to exist in the generated code. The dep-install notes
            # below say f'{NOTE_PREFIX} ...' inside PLAIN strings — literal
            # text here, evaluated over there — so without this line they
            # raise NameError and the launcher dies with a traceback instead
            # of reporting why the install failed. Interpolated (f-string) on
            # purpose: the value has to be baked in, not looked up.
            f"NOTE_PREFIX = {NOTE_PREFIX!r}",
            f"_cache = {cache!r}",
            f"_rel = {file_path!r}",
            f"_argv = {argv!r}",
            f"_interp = {interp!r}",
            f"_files = {files!r}" if files is not None else "_files = None",
            "_ready = os.path.join(_cache, '.ready')",
            # Readiness must mean "the SCRIPT is there", not just "a marker
            # file is there". A tree can lose its contents while `.ready`
            # survives — a checkpoint restore reverting `.adk-cc/**`, a manual
            # clean, an interrupted write — and then the probe says warm and
            # the run dies with "No such file or directory" on the script
            # itself. Checking the actual target re-materialises instead.
            "_target = os.path.join(_cache, _rel)",
            f"_deps = {list(deps or ())!r}",
            "_depnote = ''",
            "def _emit(**kw):",
            "    if _depnote: kw['stderr'] = _depnote + (kw.get('stderr') or '')",
            "    print(_json.dumps(dict(__shell_result__=True, **kw)))",
            "if not (os.path.isfile(_ready) and os.path.isfile(_target)):",
            "    if _files is None:",
            "        print(_json.dumps({'__needs_materialize__': True}))",
            "        raise SystemExit(0)",
            "    if not _files:",
            # An empty payload cannot produce a working skill, and the old
            # code died on `open(_ready)` with a bare ENOENT that pointed at
            # the runtime dir rather than the real problem.
            "        _emit(returncode=1, stdout='', stderr="
            "'skill has no files to materialise (nothing readable in the "
            "skill directory) — reinstall or re-add the skill')",
            "        raise SystemExit(0)",
            # Create the digest dir EXPLICITLY: `makedirs(dirname(f))` below
            # only ever creates it as a side effect of a file that happens to
            # sit in a subdirectory, so a flat file set left it missing.
            "    os.makedirs(_cache, exist_ok=True)",
            "    _parent = os.path.dirname(_cache)",
            # Older versions of the same skill are dead the moment this one
            # exists; leaving them would grow without bound in the project.
            "    if os.path.isdir(_parent):",
            "        for _old in os.listdir(_parent):",
            "            if os.path.join(_parent, _old) != _cache:",
            "                shutil.rmtree(os.path.join(_parent, _old), ignore_errors=True)",
            "    for _r, _c in _files.items():",
            "        _f = os.path.join(_cache, _r)",
            "        os.makedirs(os.path.dirname(_f), exist_ok=True)",
            "        with open(_f, 'wb' if isinstance(_c, bytes) else 'w') as _fh:",
            "            _fh.write(_c)",
            "    for _r in _files:",
            "        if _r.startswith('scripts/'):",
            "            try: os.chmod(os.path.join(_cache, _r), 0o755)",
            "            except OSError: pass",
            "    open(_ready, 'w').write('1')",
            # Lazy dependency install (#94): Python only, into THIS session's
            # analysis env (sys.executable here IS that env), once per skill
            # version (the marker sits beside .ready in the digest dir). The
            # user has already seen this list on the confirmation card. A
            # failed install is reported and the script still runs — it may
            # not need every package on the list.
            "_depmark = os.path.join(_cache, '.deps-ok')",
            "if _deps and not os.path.isfile(_depmark):",
            "    _uv = shutil.which('uv')",
            "    if not _uv:",
            "        _depnote = (f'{NOTE_PREFIX} could not install ' + ', '.join(_deps) +",
            "                    ' (uv not available); the script may fail on "
            "imports.\\n')",
            "    else:",
            "        _ir = subprocess.run([_uv, 'pip', 'install', '--python',",
            "            sys.executable] + _deps, capture_output=True, text=True,",
            "            timeout=600, stdin=subprocess.DEVNULL)",
            # A container sandbox mounts its rootfs read-only, so installing
            # INTO the interpreter fails whenever that interpreter lives on it
            # — which is exactly the supported offline setup
            # (ADK_CC_ANALYSIS_ENV=/usr/local/bin/python). Measured live:
            # "failed to create directory /usr/local/lib/python3.13/
            # site-packages/psycopg2-...dist-info: Read-only file system".
            #
            # Retry into a writable directory beside the materialised skill and
            # put it on sys.path instead of giving up. Nothing needs to mutate
            # the interpreter — a per-skill target is enough, and it keeps one
            # skill's pins out of every other skill's environment.
            "        if _ir.returncode != 0 and 'Read-only file system' in "
            "(_ir.stderr or ''):",
            "            _dtgt = os.path.join(_cache, '.deps')",
            "            os.makedirs(_dtgt, exist_ok=True)",
            "            _ir = subprocess.run([_uv, 'pip', 'install', '--target',",
            "                _dtgt, '--python', sys.executable] + _deps,",
            "                capture_output=True, text=True, timeout=600,",
            "                stdin=subprocess.DEVNULL)",
            "            if _ir.returncode == 0:",
            "                _pp = os.environ.get('PYTHONPATH') or ''",
            "                os.environ['PYTHONPATH'] = (",
            "                    _dtgt + (os.pathsep + _pp if _pp else ''))",
            "        if _ir.returncode == 0:",
            "            open(_depmark, 'w').write('1')",
            "        else:",
            "            _ro = 'Read-only file system' in (_ir.stderr or '')",
            "            _depnote = (f'{NOTE_PREFIX} installing ' + ', '.join(_deps) +",
            "                        ' failed: ' + (_ir.stderr or '')[-400:] +",
            "                        ('\\nThe interpreter at ' + sys.executable +",
            "                         ' is on a read-only filesystem, so `uv pip "
            "install` into it CANNOT work — do not retry it. Ask the operator to "
            "unset ADK_CC_ANALYSIS_ENV so a writable virtualenv is provisioned in "
            "the workspace, or to add this package to the sandbox image.'",
            "                         if _ro else '') + '\\n')",
            "_abs = os.path.abspath(os.path.join(_cache, _rel))",
            "if not os.path.isfile(_abs):",
            "    _emit(stdout='', stderr='materialised skill is missing ' + _rel,",
            "          returncode=1)",
            "    raise SystemExit(0)",
            # sys.executable is the analysis-env interpreter this wrapper runs
            # under, so a .py script gets the same packages as the rest of the
            # session rather than whatever `python` means on PATH.
            "_cmd = ([sys.executable] if not _interp else None)",
            "if _interp:",
            "    _exe = shutil.which(_interp[0])",
            "    if not _exe:",
            "        _emit(stdout='', stderr=('this skill script needs ' + _interp[0] +",
            "              ', which is not installed here. Install it, or report the "
            "step as not run — do not substitute a different method silently.'),",
            "              returncode=127)",
            "        raise SystemExit(0)",
            "    _cmd = [_exe] + _interp[1:]",
            "try:",
            # stdin=DEVNULL is load-bearing, not hygiene. There is no
            # interactive user behind a skill script, so a script that reads
            # stdin (pandas.read_csv('-'), input(), a `getpass` in a helper)
            # blocks on an inherited descriptor that never closes. Measured:
            # data-analyst's premodel_audit hung for the FULL exec timeout and
            # returned nothing, surfacing as "no output from the skill-script
            # launcher"; the identical command with </dev/null finished in 5s.
            "    _r = subprocess.run(_cmd + [_abs] + _argv, capture_output=True,",
            f"        text=True, timeout={timeout!r}, cwd=os.getcwd(),",
            "        stdin=subprocess.DEVNULL)",
            "    _emit(stdout=_r.stdout, stderr=_r.stderr, returncode=_r.returncode)",
            "except subprocess.TimeoutExpired as _e:",
            # TimeoutExpired carries BYTES even under text=True (CPython
            # decodes only on the success path), so emitting it raw raised
            # "Object of type bytes is not JSON serializable" and the timeout
            # report — the message AND the partial output it exists to
            # preserve — was replaced by a traceback. Unreachable until the
            # inner timeout could fire at all, so it shipped broken.
            "    def _txt(_v):",
            "        if isinstance(_v, bytes): return _v.decode('utf-8', 'replace')",
            "        return _v or ''",
            "    _emit(stdout=_txt(_e.stdout),",
            f"          stderr=(_txt(_e.stderr) + 'Timed out after {timeout}s'),",
            "          returncode=-1)",
        ])


class _NoopGuardedRunSkillScriptTool(_EnablementCheckedRunSkillScriptTool):
    """`run_skill_script` that refuses under the noop backend (host exec).

    Phase-2 guard (only installed when ADK_CC_SKILL_GUARDS=1): under noop a
    skill's script runs on the HOST. Refuse unless explicitly acknowledged
    with ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC=1. Mirrors how the artifact tools
    gate on the noop backend.
    """

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        if not env_bool("ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC"):
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


class _FilteredListSkillsTool(ListSkillsTool):
    """`list_skills` minus the skills this session has turned off."""

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        from google.adk.skills import prompt as _skill_prompt

        skills = skill_enablement.filter_skills(
            self._toolset._list_skills(), _state_of(tool_context)
        )
        return _skill_prompt.format_skills_as_xml(skills)


# The project root of the session being served right now.
#
# ADK's skill lookups (`_list_skills`, `_get_skill`) take no context — they are
# plain accessors on the toolset, which is built ONCE for the agent and shared
# by every session. A contextvar carries the per-request answer into them,
# mirroring how the model selection already reaches SelectableLlm.
_ACTIVE_PROJECT_ROOT: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "adk_cc_active_project_root", default=None
)

# root → cached discovery. Built on first use per project and reused: the
# built-ins are the bulk of it and re-reading them per request would be
# wasteful, but a project layer is small.
#
# Two things this has to get right, both of which it originally got wrong and
# which together made "did my new skill show up?" non-deterministic:
#
#   1. The KEY is normalised. It used to be the raw root string, so one
#      directory could occupy four entries — `/tmp/p`, `/private/tmp/p`,
#      `/tmp/p/`, `/tmp/p//` — each freezing the skill set as of the first
#      time that particular spelling appeared. Whether a new skill was
#      visible depended on which spelling the turn happened to present.
#      Normalisation is LEXICAL, never realpath: a remote workspace's path
#      must not be resolved against this host (see WorkspaceRoot).
#   2. It is INVALIDATED by a directory signature, so adding, removing, or
#      editing a skill is picked up on the next turn without a restart.
_SKILLS_BY_ROOT: dict[str, "_RootSkills"] = {}

# How often a cached root re-stats its dirs. The signature costs one stat per
# skill, so it is cheap, but `_skill_dir_for` runs per resource access and
# there is no reason to pay it several times inside one turn.
_SIG_RECHECK_S = 1.0


@dataclass
class _RootSkills:
    """Discovery for one project root, plus what makes it go stale."""

    skills: list[Skill]
    index: dict[str, str]
    signature: tuple
    checked_at: float

    def as_tuple(self) -> tuple[list[Skill], dict[str, str]]:
        return (self.skills, self.index)


def _normalise_root(root: str) -> str:
    """Collapse the spellings of one directory into a single cache key.

    Lexical only — `normpath` folds `//`, `.` and trailing slashes without
    touching symlinks. Deliberately NOT `realpath`: for a remote workspace
    that would resolve the path against the wrong machine, which is exactly
    the hazard the grant code already refuses to take."""
    try:
        return os.path.normpath(root)
    except Exception:  # noqa: BLE001
        return root


def _skills_signature(dirs: list[Path]) -> tuple:
    """Cheap fingerprint of the skill dirs — enough to catch an added,
    removed, or edited skill without re-reading a single skill body.

    Each scanned dir contributes its own mtime (which moves when a skill
    folder is added or removed) and, per skill, its SKILL.md mtime+size
    (which moves when the frontmatter or body is edited). Bodies are read
    lazily at load time, so a body edit already shows up without this; the
    signature is what makes the CATALOGUE keep up."""
    out: list[tuple] = []
    for d in dirs:
        try:
            out.append((str(d), os.stat(d).st_mtime_ns))
            # scandir, not iterdir: this runs on a per-resource-access path,
            # and building a Path per entry costs several times the stat it
            # exists to perform.
            with os.scandir(d) as it:
                children = sorted(e.path for e in it if e.is_dir())
        except OSError:
            out.append((str(d), -1))
            continue
        for child in children:
            try:
                st = os.stat(os.path.join(child, "SKILL.md"))
            except OSError:
                continue
            out.append((os.path.basename(child), st.st_mtime_ns, st.st_size))
            # EVERY file, not just SKILL.md. The original signature trusted
            # its own docstring's claim that bodies are read lazily — false
            # for scripts/references/assets: ADK's loader read_text()s them
            # ONCE into SkillResources dicts, and get_script() serves that RAM
            # copy forever. So editing scripts/foo.py moved nothing in the
            # signature, the cached Skill objects kept their old content, the
            # materialisation digest never changed, and the OLD script ran —
            # reported live from web mode ("cached skills material has no way
            # to be renewed"), and the reason desktop needed its explicit
            # reload as a workaround. Cost: a few dozen stats per skill on a
            # 1s-TTL recheck path.
            for sub in ("scripts", "references", "assets"):
                subdir = os.path.join(child, sub)
                entries: list[tuple] = []
                # RECURSIVE — ADK's loader keys resources by relative path, so
                # nested files (scripts/lib/util.py) are loaded too and must
                # move the signature like any other.
                for root_, _dirs, files_ in os.walk(subdir):
                    for fn in files_:
                        fp = os.path.join(root_, fn)
                        try:
                            st_f = os.stat(fp)
                        except OSError:
                            continue
                        entries.append((os.path.relpath(fp, subdir),
                                        st_f.st_mtime_ns, st_f.st_size))
                if entries:
                    out.append((os.path.basename(child), sub,
                                tuple(sorted(entries))))
    return tuple(out)


def locate_skill_script(candidate: str) -> Optional[tuple[str, str]]:
    """`(skill_name, path_within_skill)` for a script that lives in a SKILL, or
    None. Used to explain a failed `run_bash python scripts/x.py`.

    A skill's files are not in the workspace — they are served through the skill
    tools — but the natural instinct (and, for vendored skills, their own docs:
    `data-analyst/scripts/README.md` says `python scripts/premodel_audit.py
    data.csv --target SalePrice`) is a bare interpreter call on a relative path.
    Live, that failed with "can't open file", and the agent quietly wrote its own
    analysis instead: six vetted probe scripts shipped, none run, and an answer
    that looked fine.

    Matching is by the tail of the path, so `scripts/premodel_audit.py` and a
    bare `premodel_audit.py` both resolve.
    """
    name = (candidate or "").strip().strip("'\"")
    if not name:
        return None
    tail = name.split("/")[-1]
    if not tail or "." not in tail:
        return None
    resolved = _skills_for_root(_ACTIVE_PROJECT_ROOT.get())
    index: dict[str, str] = dict(resolved[1]) if resolved else {}
    if not index:
        try:
            pairs = discover_skills_with_sources()
            index = _build_skill_dir_index(pairs)
        except Exception:  # noqa: BLE001
            return None
    for skill_name, base in index.items():
        try:
            for found in Path(base).rglob(tail):
                if found.is_file():
                    return skill_name, str(found.relative_to(base))
        except OSError:
            continue
    return None


def _skill_dir_for(skill_name: str, fallback: dict[str, str]) -> Optional[str]:
    """On-disk dir for a skill, preferring the active session's index.

    The resource/search tools were handed ONE index at construction. A project
    skill discovered per session is not in it, so its references/ and scripts/
    would resolve to nothing (or, worse, to a same-named built-in's copy)."""
    root = _ACTIVE_PROJECT_ROOT.get()
    resolved = _skills_for_root(root) if root else None
    if resolved and skill_name in resolved[1]:
        return resolved[1][skill_name]
    return fallback.get(skill_name)


def _root_of(ctx: Any) -> Optional[str]:
    """The workspace root for this session, or None when it cannot be told.

    Desktop works in the project directory in place, so the workspace root IS
    the project root. Returning None falls back to the process-wide skills,
    which is the old behaviour — never an error."""
    try:
        from ..sandbox import get_workspace

        ws = get_workspace(ctx)
        return str(getattr(ws, "abs_path", "") or "") or None
    except Exception:  # noqa: BLE001 — no workspace (tests, odd contexts)
        return None


def _skills_for_root(root: Optional[str]) -> Optional[tuple[list[Skill], dict[str, str]]]:
    """Skills visible to a session rooted at `root`, or None to use the
    process-wide set."""
    if not root or env_bool("ADK_CC_DISABLE_PROJECT_SKILLS"):
        return None
    key = _normalise_root(root)
    now = time.monotonic()
    cached = _SKILLS_BY_ROOT.get(key)
    # Hot path first: this runs per resource access, and resolving the dir
    # list walks from the project root up to $HOME — more work than the
    # signature it would feed. Inside the window, answer from the entry alone.
    if cached is not None and now - cached.checked_at < _SIG_RECHECK_S:
        return cached.as_tuple()
    try:
        dirs = _resolve_skills_dirs(Path(key))
    except Exception:  # noqa: BLE001 — a bad project dir must not break the turn
        _log.debug("project skill dirs failed for %r", root, exc_info=True)
        return cached.as_tuple() if cached else None

    if not dirs and cached is not None:
        # Nothing left to scan AT ALL — an unmounted share or a deleted
        # project folder, not a user emptying their skills. Keep serving the
        # last good set: a catalogue that silently goes blank mid-session is
        # worse than one that is briefly out of date. (Deleting individual
        # skills still takes effect: those dirs still exist to be scanned.)
        _log.info("skills: no readable skill dirs for %s — keeping the last set", key)
        return cached.as_tuple()

    # Computed ONCE and reused as the new entry's signature: taking it again
    # after the scan would be both wasted work and a window in which a write
    # landing mid-scan gets stamped as already-seen.
    signature = _skills_signature(dirs)
    if cached is not None:
        if signature == cached.signature:
            cached.checked_at = now
            return cached.as_tuple()
        _log.info("skills: %s changed on disk — rediscovering", key)
        # A skill that failed to load before may be fixed now; a diagnostic
        # about a file that no longer exists is worse than none.
        _UNLOADABLE.clear()
        _DIAGNOSTICS.clear()

    try:
        pairs = discover_skills_with_sources(dirs)
    except Exception:  # noqa: BLE001
        _log.debug("project skill discovery failed for %r", root, exc_info=True)
        return cached.as_tuple() if cached else None
    max_bytes = _file_max_bytes()
    for skill, _ in pairs:
        _prune_oversized_resources(skill, max_bytes)
    entry = _RootSkills(
        skills=[s for s, _ in pairs],
        index=_build_skill_dir_index(pairs),
        signature=signature,
        checked_at=now,
    )
    _SKILLS_BY_ROOT[key] = entry
    return entry.as_tuple()


def clear_project_skill_cache() -> None:
    """Drop the per-root cache (tests, and a skills-changed signal)."""
    _SKILLS_BY_ROOT.clear()
    # Whatever was broken last time may have been fixed; the next discovery
    # re-records it if not.
    _UNLOADABLE.clear()
    _DIAGNOSTICS.clear()
    _UNTRUSTED.clear()


# ADK's stock skill instruction says scripts "can be run via bash" — and then,
# four lines later, to use `run_skill_script`. The model resolves that in
# favour of bash, which cannot work: skill files live outside the workspace
# and are materialised under a content-addressed cache, so `python
# scripts/x.py` either fails or runs whatever stale copy a previous digest
# left. Observed live: the model retried from several different guessed
# directories, and because each guess produced a DIFFERENT command string,
# "allow always" never matched and it re-prompted every time.
#
# The post-failure hint in tools/bash already explains this once it has gone
# wrong (_skill_script_hint). This is the same knowledge, delivered before the
# first attempt instead of after it.
_SKILL_SCRIPT_INSTRUCTION = """
IMPORTANT — running a skill's scripts (this overrides any statement above that
skill scripts "can be run via bash"):

- A skill's files are NOT in your workspace. No path and no `cd` reaches them,
  so `run_bash python scripts/<name>.py` does not work — it fails, or worse,
  runs a stale copy.
- Run them with `run_skill_script(skill_name="<skill>", file_path="scripts/
  <name>.py", args=[...])`. Use `load_skill_resource` first if you need to
  read the script or its README.
- It executes with YOUR workspace as the working directory, so file paths you
  pass in `args` mean the same thing they do in `run_bash`.
"""

class _EnablementAwareSkillToolset(SkillToolset):
    """SkillToolset that also honours the deny-list in the SYSTEM INSTRUCTION.

    `list_skills` is not the only catalog: ADK's `process_llm_request` injects
    every skill's name + description into each request. Filtering only the tool
    would leave disabled skills costing tokens on every turn and still visible
    to the model — the two things the toggle exists to prevent.
    """

    # --- per-session resolution ------------------------------------------

    # Set by `make_skill_toolset`: False when the caller pinned ONE directory.
    # Per-session resolution must not override an explicit choice — a test (or
    # an operator) that says "use exactly this dir" means it, and silently
    # swapping in the built-ins plus a walk-up made the pinned skills vanish
    # from the catalogue.
    _project_scoped: bool = True

    def _project_skills(self) -> Optional[tuple[list[Skill], dict[str, str]]]:
        if not self._project_scoped:
            return None
        return _skills_for_root(_ACTIVE_PROJECT_ROOT.get())

    def _list_skills(self):  # noqa: ANN201 — matches ADK's signature
        resolved = self._project_skills()
        return resolved[0] if resolved else super()._list_skills()

    def _get_skill(self, name: str):  # noqa: ANN201
        resolved = self._project_skills()
        if resolved:
            for skill in resolved[0]:
                if getattr(getattr(skill, "frontmatter", None), "name", None) == name:
                    return skill
            # Fall through: a project root that does not define the name should
            # still see the built-ins, which the base set holds.
        return super()._get_skill(name)

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        # `get_tools` runs per request, before the model sees anything, so this
        # is the earliest point the session's root is knowable.
        if readonly_context is not None:
            _ACTIVE_PROJECT_ROOT.set(_root_of(readonly_context))
        return await super().get_tools(readonly_context)

    async def process_llm_request(
        self, *, tool_context: ToolContext, llm_request: Any
    ) -> None:
        from google.adk.tools.skill_toolset import (
            _DEFAULT_SKILL_SYSTEM_INSTRUCTION,
        )
        from google.adk.skills import prompt as _skill_prompt

        _ACTIVE_PROJECT_ROOT.set(_root_of(tool_context))
        skills = skill_enablement.filter_skills(
            self._list_skills(), _state_of(tool_context)
        )
        llm_request.append_instructions([
            _DEFAULT_SKILL_SYSTEM_INSTRUCTION,
            _SKILL_SCRIPT_INSTRUCTION,
            _skill_prompt.format_skills_as_xml(skills),
        ])
        # This is the only place that knows what was actually OFFERED on a
        # turn — after the enablement filter, before the model chooses. Paired
        # with the activation count in `load_skill`, it tells a skill's author
        # whether their description is doing its job.
        try:
            skill_usage.record_offered(
                [s.frontmatter.name for s in skills],
                _invocation_id_of(tool_context))
        except Exception:  # noqa: BLE001 — a counter must never break a turn
            _log.debug("skills: could not record offers", exc_info=True)


def _install_wider_script_launcher() -> None:
    """Point ADK's `run_skill_script` at `_WiderScriptCodeExecutor`.

    Idempotent, and a no-op for `.py`/`.sh`/`.bash` (those still go through
    ADK's own wrapper), so re-running it or hitting it from several toolsets is
    safe. Global because ADK instantiates the class by module-level name.
    """
    if _adk_skill_toolset._SkillScriptCodeExecutor is not _WiderScriptCodeExecutor:
        _adk_skill_toolset._SkillScriptCodeExecutor = _WiderScriptCodeExecutor


def _patch_skill_tools(
    toolset: SkillToolset, skill_dirs: dict[str, str]
) -> None:
    """Swap ADK's skill tools for adk-cc's bounded/guarded variants in-place.

    Also records the name->dir index globally so `load_skill` can tell the
    model where a skill actually lives (see `_base_dir_header`).

    `SkillToolset._tools` is a regular list built in `__init__`. We replace:
      - LoadSkillResourceTool → _LenientLoadSkillResourceTool (bounded + disk
        fallback)
      - LoadSkillTool         → _BoundedLoadSkillTool (caps instructions)
      - RunSkillScriptTool    → _NoopGuardedRunSkillScriptTool (only when
        ADK_CC_SKILL_GUARDS=1)
    Idempotent: already-swapped subclasses are skipped.
    """
    guards = _guards_on()
    _install_wider_script_launcher()
    # So load_skill can name a skill's directory without threading the index
    # through ADK's tool constructors.
    _SKILL_DIRS.update(skill_dirs or {})
    for i, tool in enumerate(toolset._tools):
        if isinstance(tool, LoadSkillResourceTool) and not isinstance(
            tool, _LenientLoadSkillResourceTool
        ):
            toolset._tools[i] = _LenientLoadSkillResourceTool(toolset, skill_dirs)
        elif isinstance(tool, LoadSkillTool) and not isinstance(
            tool, _BoundedLoadSkillTool
        ):
            toolset._tools[i] = _BoundedLoadSkillTool(toolset)
        elif isinstance(tool, ListSkillsTool) and not isinstance(
            tool, _FilteredListSkillsTool
        ):
            toolset._tools[i] = _FilteredListSkillsTool(toolset)
        elif isinstance(tool, RunSkillScriptTool) and not isinstance(
            tool, _EnablementCheckedRunSkillScriptTool
        ):
            # The enablement check is unconditional; the host-exec guard is the
            # opt-in layer on top of it.
            toolset._tools[i] = (
                _NoopGuardedRunSkillScriptTool(toolset)
                if guards
                else _EnablementCheckedRunSkillScriptTool(toolset)
            )


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
        # The wrapper runs the script under its OWN timeout (script_timeout)
        # and must outlive it, or the outer kill always wins and the inner's
        # report — which names the script and carries its partial stdout —
        # can never be sent. The executor's 60s dataclass default was an
        # accident here: `script_timeout=300` is this toolset's declared
        # limit, so a 90s analysis script was being killed at 60s by
        # machinery that never meant to have an opinion. This executor is
        # built for skills alone (run_code has its own), so raising it
        # changes nothing else.
        code_executor.timeout_seconds = script_timeout + _OUTER_TIMEOUT_MARGIN_S
    toolset = _EnablementAwareSkillToolset(
        skills=[s for s, _ in pairs],
        code_executor=code_executor,
        script_timeout=script_timeout,
    )
    toolset._project_scoped = skills_dir is None
    # Phase 1.5: always-on grep-within-resource retrieval tool. Appended to
    # `_tools` directly — `additional_tools=` would gate it behind a skill's
    # adk_additional_tools metadata (it lands in _provided_tools_by_name).
    toolset._tools.append(_SkillResourceSearchTool(skill_dirs))
    _patch_skill_tools(toolset, skill_dirs)
    return toolset
