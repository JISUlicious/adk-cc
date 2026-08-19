"""What a skill's scripts need installed, computed BEFORE the first run.

Measured across the published example-skills: `pdf` dies on
`No module named 'pypdf'`; docx/pptx/xlsx need `defusedxml`;
`slack-gif-creator` needs pillow/imageio; `mcp-builder` ships the only
`scripts/requirements.txt` in the corpus. The spec's `compatibility` field —
the designated place for this — is set by 0 of 41 skills surveyed, so the
information lives in manifests and code, not metadata.

Scope, fixed by decision (task #94):
  * Python packages only, into the SESSION's analysis environment only. Never
    system Python, never a system package manager, never binaries.
  * Collected from an explicit manifest first (`scripts/requirements.txt`),
    then from the scripts' own top-level imports — minus stdlib, minus sibling
    modules, minus what the tier system already provisions.
  * NEVER inferred from a runtime ModuleNotFoundError: reading a typo'd
    `import reqeusts` out of a traceback and installing it is how a typo
    becomes a supply-chain incident. Everything collected here is shown to the
    user on the same card that gates the script itself.

Root-level requirements.txt (slack-gif-creator's layout) is invisible to ADK's
loader, which only reads scripts/references/assets — those skills are covered
by the import scan instead.
"""

from __future__ import annotations

import re
import sys
from typing import Any

# Import name → distribution name, for the measured cases. An unmapped name
# passes through as itself — visible on the card before anything installs, so
# a wrong guess is inspectable rather than silent.
_IMPORT_TO_DIST: dict[str, str] = {
    "PIL": "pillow",
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "dateutil": "python-dateutil",
    "bs4": "beautifulsoup4",
    "fitz": "PyMuPDF",
    "defusedxml": "defusedxml",
    "pypdf": "pypdf",
    # Reported live (client's mlcc skill): bare `psycopg2` is a SOURCE build
    # needing pg_config, which no sandbox has — the wheel-only dist is the
    # one that actually installs.
    "psycopg2": "psycopg2-binary",
}

_TOP_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+([A-Za-z_][\w]*)|from\s+([A-Za-z_][\w]*)(?:[\w.]*)\s+import)",
    re.M,
)

# Spec lines a requirements.txt can carry that are not installable dists.
_REQ_SKIP_RE = re.compile(r"^\s*(?:#|-r\b|-c\b|--|\s*$)")


def _tier_import_names() -> frozenset[str]:
    """Import names the analysis-env tier system already provisions — those
    ride the existing `adk-cc-skill-tiers` header and must not be installed a
    second way."""
    try:
        from ..sandbox.analysis_env import _IMPORT_TIER

        return frozenset(_IMPORT_TIER)
    except Exception:  # noqa: BLE001 — collector must work without the sandbox
        return frozenset()


def _declared_requirements(skill: Any) -> list[str]:
    """`metadata["x-adk-cc/requirements"]` from SKILL.md — the explicit
    declaration channel (mirrors x-adk-cc/secrets / x-adk-cc/verify). A JSON
    array of specs, or a comma/newline-separated string. Never raises."""
    try:
        raw = (getattr(skill.frontmatter, "metadata", None)
               or {}).get("x-adk-cc/requirements")
    except Exception:  # noqa: BLE001
        return []
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                import json

                data = json.loads(s)
                return [str(x).strip() for x in data if str(x).strip()] \
                    if isinstance(data, list) else []
            except ValueError:
                return []
        return [p.strip() for p in re.split(r"[,\n]", s) if p.strip()]
    return []


def _disk_modules(skill_dir: str) -> dict[str, str]:
    """{module_name: source} for `.py` files the skill SHIPS but ADK's
    resource loader does not expose — the skill's top level. Reported live
    (client's mlcc skill): a root-level `common.py` importing psycopg2 was
    invisible to the scan, AND `import common` itself was then mis-read as
    a PyPI dist named 'common'. Materialisation ships the whole folder, so
    the runtime sees these files — the scan must too. Bounded: top level
    only (scripts/ already arrives via resources), small files only."""
    import os

    out: dict[str, str] = {}
    try:
        for entry in sorted(os.listdir(skill_dir)):
            if not entry.endswith(".py"):
                continue
            p = os.path.join(skill_dir, entry)
            try:
                if os.path.getsize(p) > 512_000:
                    continue
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    out[entry[:-3]] = fh.read()
            except OSError:
                continue
    except OSError:
        pass
    return out


def collect_requirements(skill: Any, *, skill_dir: str = "") -> list[str]:
    """Distributions this skill's scripts need and nothing else provides.

    Order and provenance:
      1. `metadata["x-adk-cc/requirements"]` in SKILL.md, verbatim — the
         author's explicit word beats any scan.
      2. `scripts/requirements.txt`, verbatim (specifiers kept — the author
         pinned them for a reason).
      3. Top-level imports of every shipped `.py` — scripts/ via ADK's
         resources, plus the skill's top level from disk when `skill_dir`
         is given — mapped to distributions.

    Deduplicated case-insensitively by distribution name; earlier sources
    win over a scan hit for the same dist.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(spec: str) -> None:
        dist = re.split(r"[<>=!~\[;\s]", spec.strip(), 1)[0].lower()
        if dist and dist not in seen:
            seen.add(dist)
            out.append(spec.strip())

    for spec in _declared_requirements(skill):
        _add(spec)

    try:
        res = skill.resources
        script_names = list(res.list_scripts())
    except Exception:  # noqa: BLE001 — a skill with no resources needs nothing
        return out

    for name in script_names:
        if name.rsplit("/", 1)[-1].lower() == "requirements.txt":
            src = getattr(res.get_script(name), "src", "") or ""
            if isinstance(src, bytes):
                continue
            for line in src.splitlines():
                if not _REQ_SKIP_RE.match(line):
                    _add(line.split("#", 1)[0])

    disk = _disk_modules(skill_dir) if skill_dir else {}
    sources: list[str] = []
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    tiered = _tier_import_names()
    siblings = {n.rsplit("/", 1)[-1][:-3] for n in script_names
                if n.endswith(".py")} | set(disk)
    for name in script_names:
        if not name.endswith(".py"):
            continue
        src = getattr(res.get_script(name), "src", "") or ""
        if not isinstance(src, bytes):
            sources.append(src)
    sources.extend(disk.values())
    for src in sources:
        for m in _TOP_IMPORT_RE.finditer(src):
            mod = (m.group(1) or m.group(2) or "").split(".")[0]
            if (not mod or mod in stdlib or mod in tiered
                    or mod in siblings or mod == "scripts"):
                continue
            _add(_IMPORT_TO_DIST.get(mod, mod))
    return out
