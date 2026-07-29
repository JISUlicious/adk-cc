"""Skill-declared verification contracts (W9 S1).

A skill states what "done" means for its own output:

    metadata:
      x-adk-cc/verify: |
        {"mode": "self",
         "checks": ["every finding cites a command output or file:line"],
         "commands": ["python -m pytest -q"]}

Why the skill declares it: the verifier's weakest point is deciding what
"working" means for an arbitrary task. The skill author knows the criteria
better than a general-purpose verifier ever could, so the skill supplies the
target and the loop enforces checking it.

Parsing mirrors `credentials/required_inputs`: same `x-adk-cc/*` namespace,
same rule that malformed metadata is skipped with a debug log — a bad manifest
must never break skill loading.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

_log = logging.getLogger(__name__)

VERIFY_METADATA_KEY = "x-adk-cc/verify"
# Upstream skill authors adopted the idea under a plain key — pd-skills'
# data-analyst declares `metadata.verify` as a YAML list of checks. Requiring
# an adk-cc-specific namespace of a first-party skill we merely vendor would
# mean rewriting its frontmatter on every update, so read both.
FALLBACK_METADATA_KEY = "verify"

MODES = ("none", "self", "verifier")


@dataclass(frozen=True)
class VerifyContract:
    mode: str = "none"
    checks: tuple[str, ...] = field(default_factory=tuple)
    commands: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""

    @property
    def wants_verifier(self) -> bool:
        return self.mode == "verifier"

    @property
    def is_active(self) -> bool:
        return self.mode in ("self", "verifier")


def _as_list(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,) if v.strip() else ()
    if isinstance(v, (list, tuple)):
        return tuple(str(x).strip() for x in v if str(x).strip())
    return ()


def parse(raw: Any, *, source: str = "") -> VerifyContract:
    """Parse an `x-adk-cc/verify` value. Never raises."""
    if raw is None:
        return VerifyContract(source=source)
    data: Any = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return VerifyContract(source=source)
        if s[0] in "[{":
            try:
                data = json.loads(s)
            except json.JSONDecodeError as e:
                _log.debug("%s: invalid %s JSON: %s", source, VERIFY_METADATA_KEY, e)
                return VerifyContract(source=source)
        else:
            # bare mode, e.g. `x-adk-cc/verify: self`
            data = {"mode": s}
    if isinstance(data, (list, tuple)):
        # A bare LIST of checks — the shape upstream pd-skills uses. Mode
        # defaults to "self": the author stated criteria, not who enforces them.
        return VerifyContract(mode="self", checks=_as_list(data), source=source)
    if not isinstance(data, dict):
        return VerifyContract(source=source)

    mode = str(data.get("mode") or "self").strip().lower()
    if mode not in MODES:
        _log.debug("%s: unknown verify mode %r; treating as 'self'", source, mode)
        mode = "self"
    return VerifyContract(
        mode=mode,
        checks=_as_list(data.get("checks")),
        commands=_as_list(data.get("commands")),
        source=source,
    )


def contract_for_skill(skill: Any) -> VerifyContract:
    """Read the contract off a loaded ADK Skill (frontmatter.metadata)."""
    fm = getattr(skill, "frontmatter", None)
    meta = getattr(fm, "metadata", None) or {}
    name = getattr(fm, "name", "") or getattr(skill, "name", "") or "?"
    if not isinstance(meta, dict):
        return VerifyContract(source=f"skill:{name}")
    raw = meta.get(VERIFY_METADATA_KEY)
    if raw is None:
        raw = meta.get(FALLBACK_METADATA_KEY)
    return parse(raw, source=f"skill:{name}")


def criteria_from_skills(skill_names: Iterable[str], skills: Iterable[Any]) -> list[str]:
    """Checks declared by the named skills — the acceptance criteria to show
    the model (or hand the verifier) for this turn."""
    wanted = {n for n in skill_names if n}
    out: list[str] = []
    for s in skills or []:
        fm = getattr(s, "frontmatter", None)
        nm = getattr(fm, "name", None) or getattr(s, "name", None)
        if nm in wanted:
            out.extend(contract_for_skill(s).checks)
    return list(dict.fromkeys(out))
