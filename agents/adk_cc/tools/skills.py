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
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from google.adk.code_executors.base_code_executor import BaseCodeExecutor
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
from ..config.schema import env_bool
from . import skill_enablement

_log = logging.getLogger(__name__)


# The one project-scoped location. `.claude/skills` used to be accepted here
# too (first-existing-wins per walked directory); scopes are now explicit and
# `.adk-cc/skills` is what "project skill" means, so a Claude Code skills folder
# is no longer picked up as one.
_PROJECT_SKILLS_SUBDIR = ".adk-cc/skills"

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

    # 2. PROJECT — walk up from the bound project only.
    if project_root is not None and not env_bool("ADK_CC_DISABLE_PROJECT_SKILLS"):
        try:
            cursor = Path(project_root).resolve()
        except OSError:
            cursor = None
        if cursor is not None:
            home = Path.home()
            while True:
                _add(cursor / _PROJECT_SKILLS_SUBDIR)
                if cursor == home or cursor == cursor.parent:
                    break
                cursor = cursor.parent

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

    # 3. Built-in skills — a base layer, always added (never a "fallback"):
    # higher-precedence sources override BY NAME via the first-found rule,
    # so a project skill shadows one built-in without hiding the others.
    if env_bool("ADK_CC_BUILTIN_SKILLS", True):
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


# A binary re-attached to a skill is embedded into the launcher payload on
# every run of that skill's scripts, so a huge one is a per-invocation cost
# (worse over SSH/Daytona than locally). Real ones are small — the tarball that
# exposed this is 20 KB — so cap and say what was left out.
_MAX_BINARY_RESOURCE_BYTES = 2 * 1024 * 1024


# ADK's own cap (`Frontmatter._validate_description`), restated because the
# repair below has to know the target length.
_MAX_DESCRIPTION_CHARS = 1024

_UNLOADABLE: dict[str, dict[str, str]] = {}


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


def _repair_skill(skill_dir: Path) -> Optional[Skill]:
    """Load a skill ADK rejected, when the defect is only its description.

    ADK caps `description` at 1024 characters and refuses the whole skill past
    that. The description is catalogue text — it is injected into every request
    so the model can choose the skill — so an over-long one is a reason to
    shorten the text, not to lose a working skill. Anthropic's own published
    `claude-api` skill trips it.

    Anything else stays rejected: this repairs presentation, not substance.
    """
    md = skill_dir / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        data = yaml.safe_load(parts[1]) or {}
        desc = str(data.get("description") or "")
        if not isinstance(data, dict) or len(desc) <= _MAX_DESCRIPTION_CHARS:
            return None
        # Budget the marker BEFORE cutting: an earlier version reserved a
        # round 40 characters for a 52-character marker, so the "repaired"
        # description came out at 1036 and failed the very validator this
        # exists to satisfy — silently, since a failed repair just means no
        # repair.
        marker = " …(truncated to fit the catalogue limit)"
        data["description"] = (
            desc[: _MAX_DESCRIPTION_CHARS - len(marker)].rstrip() + marker)
        skill = _SkillModel(
            frontmatter=_FrontmatterModel.model_validate(data),
            instructions=parts[2],
            resources=_ResourcesModel(),
        )
        _attach_missing_resources(skill, skill_dir)
    except Exception:  # noqa: BLE001 — a repair that fails is just no repair
        return None
    _log.warning(
        "skills: '%s' has a %d-character description (limit %d); loaded it "
        "with the description truncated — shorten it in SKILL.md",
        skill.frontmatter.name, len(desc), _MAX_DESCRIPTION_CHARS)
    return skill


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
        try:
            skill = load_skill_from_dir(skill_dir)
        except Exception as exc:  # noqa: BLE001
            skill = _repair_skill(skill_dir)
            if skill is None:
                # Skip a malformed skill rather than refusing to start — but
                # say so: a skill that is simply absent is the hardest kind of
                # bug to notice from the outside.
                _note_unloadable(name, skill_dir, str(exc))
                continue
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
    return {**res, "stderr": err.rstrip() + (
        f"\n\n[adk-cc] this script needs the '{missing}' package, which is not "
        f"installed in the environment skill scripts run in. Install it there "
        f"(uv pip install {missing}) and re-run, or report the step as NOT RUN "
        f"— do not re-implement the script's job inline and present it as the "
        f"script's result.")}


class _WiderScriptCodeExecutor(_SkillScriptCodeExecutor):
    """ADK's script launcher, taught the interpreters in `_EXTRA_INTERPRETERS`.

    This has to live on the EXECUTOR rather than on the tool: `run_skill_script`
    constructs a `_SkillScriptCodeExecutor` inside its own `run_async` and calls
    `_build_wrapper_code` on that, so an override on the tool is never reached
    (it silently wasn't — `.js` kept returning ADK's "Unsupported script type").
    Installed by swapping the module attribute ADK reads, which is also what
    makes it apply to the tool ADK builds rather than only to ours.
    """

    async def execute_script_async(  # noqa: ANN201 — mirrors ADK's signature
        self, invocation_context, skill, file_path, script_args,
        short_options=None, positional_args=None,
    ):
        """Unwrap the `__shell_result__` envelope for our interpreters too.

        ADK parses that envelope only when the extension is `sh`/`bash`, so a
        `.mjs` came back with the raw JSON as its stdout and — worse — a FAILING
        script reported `status: success`, because the real returncode was still
        inside the envelope. Post-processing super()'s result keeps ADK's
        materialisation, timeout and error handling intact.
        """
        res = await super().execute_script_async(
            invocation_context, skill, file_path, script_args, short_options,
            positional_args)
        if isinstance(res, dict):
            res = _explain_missing_package(res, skill)
        ext = _script_ext(file_path)
        if ext not in _EXTRA_INTERPRETERS or not isinstance(res, dict):
            return res
        out = res.get("stdout")
        if not out:
            return res
        try:
            parsed = json.loads(out)
        except (TypeError, ValueError):
            return res
        if not isinstance(parsed, dict) or not parsed.get("__shell_result__"):
            return res
        stdout = parsed.get("stdout", "")
        stderr = parsed.get("stderr", "")
        rc = parsed.get("returncode", 0)
        if rc != 0 and not stderr:
            stderr = f"Exit code {rc}"
        # ADK's own three-way status, kept identical so callers cannot tell the
        # two launchers apart.
        if rc != 0:
            status = "error"
        elif stderr and not stdout:
            status = "error"
        elif stderr:
            status = "warning"
        else:
            status = "success"
        return {**res, "stdout": stdout, "stderr": stderr, "status": status}

    def _build_wrapper_code(  # noqa: ANN202 — mirrors ADK's signature
        self, skill, file_path, script_args, short_options=None,
        positional_args=None,
    ):
        """Extend ADK's launcher to the interpreters in `_EXTRA_INTERPRETERS`.

        ADK builds a wrapper that materialises ALL of a skill's files into a
        temp dir, chdirs there, and either runpy's a `.py` or subprocess-runs a
        `.sh`, printing a `__shell_result__` envelope it then parses. Anything
        else returns None → "Unsupported script type".

        Emitting that same envelope for other interpreters reuses every
        surrounding piece — materialisation, argv conventions, timeout, result
        parsing — instead of forking the tool. Siblings work because the whole
        skill is materialised together and each runtime resolves relative
        imports from the script's own directory.
        """
        ext = _script_ext(file_path)
        if ext in _ADK_SCRIPT_EXTS or ext not in _EXTRA_INTERPRETERS:
            return super()._build_wrapper_code(
                skill, file_path, script_args, short_options, positional_args)

        interp = list(_EXTRA_INTERPRETERS[ext])
        argv = interp + [file_path] + _flatten_script_args(
            script_args, short_options, positional_args)
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

        timeout = getattr(self, "_script_timeout", 300)
        return "\n".join([
            "import os, sys, tempfile, shutil, subprocess, json as _json",
            f"_files = {files!r}",
            f"_argv = {argv!r}",
            "def _run():",
            "  _orig = os.getcwd()",
            "  with tempfile.TemporaryDirectory() as td:",
            "    for rel, content in _files.items():",
            "      full = os.path.join(td, rel)",
            "      os.makedirs(os.path.dirname(full), exist_ok=True)",
            "      mode = 'wb' if isinstance(content, bytes) else 'w'",
            "      with open(full, mode) as f:",
            "        f.write(content)",
            "    os.chdir(td)",
            "    try:",
            "      _exe = shutil.which(_argv[0])",
            "      if not _exe:",
            # Actionable, in the same shape as a real result, so the model reads
            # a reason rather than a bare non-zero exit.
            "        print(_json.dumps({'__shell_result__': True, 'stdout': '',",
            "          'stderr': ('this skill script needs ' + _argv[0] +",
            "            ', which is not installed here. Install it, or report "
            "the step as not run — do not substitute a different method "
            "silently.'), 'returncode': 127}))",
            "        return",
            "      _r = subprocess.run([_exe] + _argv[1:], capture_output=True,",
            f"        text=True, timeout={timeout!r}, cwd=td)",
            "      print(_json.dumps({'__shell_result__': True,",
            "        'stdout': _r.stdout, 'stderr': _r.stderr,",
            "        'returncode': _r.returncode}))",
            "    except subprocess.TimeoutExpired as _e:",
            "      print(_json.dumps({'__shell_result__': True,",
            "        'stdout': (_e.stdout or ''),",
            f"        'stderr': 'Timed out after {timeout}s',",
            "        'returncode': -1}))",
            "    finally:",
            "      os.chdir(_orig)",
            "_run()",
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

# root → (skills, name→dir index). Built on first use per project and reused:
# the built-ins are the bulk of it and re-reading them per request would be
# wasteful, but a project layer is small.
_SKILLS_BY_ROOT: dict[str, tuple[list[Skill], dict[str, str]]] = {}


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
    cached = _SKILLS_BY_ROOT.get(root)
    if cached is not None:
        return cached
    try:
        pairs = discover_skills_with_sources(_resolve_skills_dirs(Path(root)))
    except Exception:  # noqa: BLE001 — a bad project dir must not break the turn
        _log.debug("project skill discovery failed for %r", root, exc_info=True)
        return None
    max_bytes = _file_max_bytes()
    for skill, _ in pairs:
        _prune_oversized_resources(skill, max_bytes)
    resolved = ([s for s, _ in pairs], _build_skill_dir_index(pairs))
    _SKILLS_BY_ROOT[root] = resolved
    return resolved


def clear_project_skill_cache() -> None:
    """Drop the per-root cache (tests, and a skills-changed signal)."""
    _SKILLS_BY_ROOT.clear()
    # Whatever was broken last time may have been fixed; the next discovery
    # re-records it if not.
    _UNLOADABLE.clear()


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
            _skill_prompt.format_skills_as_xml(skills),
        ])


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
