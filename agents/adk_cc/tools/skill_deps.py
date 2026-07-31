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


def collect_requirements(skill: Any) -> list[str]:
    """Distributions this skill's scripts need and nothing else provides.

    Order and provenance:
      1. `scripts/requirements.txt`, verbatim (specifiers kept — the author
         pinned them for a reason).
      2. Top-level imports of every shipped `.py`, mapped to distributions.

    Deduplicated case-insensitively by distribution name; requirements.txt
    wins over a scan hit for the same dist.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(spec: str) -> None:
        dist = re.split(r"[<>=!~\[;\s]", spec.strip(), 1)[0].lower()
        if dist and dist not in seen:
            seen.add(dist)
            out.append(spec.strip())

    try:
        res = skill.resources
        script_names = list(res.list_scripts())
    except Exception:  # noqa: BLE001 — a skill with no resources needs nothing
        return []

    for name in script_names:
        if name.rsplit("/", 1)[-1].lower() == "requirements.txt":
            src = getattr(res.get_script(name), "src", "") or ""
            if isinstance(src, bytes):
                continue
            for line in src.splitlines():
                if not _REQ_SKIP_RE.match(line):
                    _add(line.split("#", 1)[0])

    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    tiered = _tier_import_names()
    siblings = {n.rsplit("/", 1)[-1][:-3] for n in script_names
                if n.endswith(".py")}
    for name in script_names:
        if not name.endswith(".py"):
            continue
        src = getattr(res.get_script(name), "src", "") or ""
        if isinstance(src, bytes):
            continue
        for m in _TOP_IMPORT_RE.finditer(src):
            mod = (m.group(1) or m.group(2) or "").split(".")[0]
            if (not mod or mod in stdlib or mod in tiered
                    or mod in siblings or mod == "scripts"):
                continue
            _add(_IMPORT_TO_DIST.get(mod, mod))
    return out
