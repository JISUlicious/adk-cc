"""Project-scoped skills load only from folders the user has trusted.

A skill is instructions the agent follows and code it runs. Project-scoped
skills come from the repository being worked on, which may be a clone the user
has never read. Both the Agent Skills implementer guide and Anthropic's own
documentation name this exact risk:

    "Consider gating project-level skill loading on a trust check — only load
     them if the user has marked the project folder as trusted. This prevents
     untrusted repositories from silently injecting instructions into the
     agent's context."   — agentskills.io client-implementation guide

    "Use Skills only from trusted sources … a malicious Skill can direct Claude
     to invoke tools or execute code in ways that don't match the Skill's
     stated purpose."   — Anthropic platform docs

So `<project>/.adk-cc/skills` and `<project>/.agents/skills` are skipped until
the folder is trusted, and what was skipped is REPORTED rather than silently
dropped — a skill that vanishes with no explanation is the failure mode this
whole area keeps producing.

Scope of the gate: PROJECT scope only. Built-ins ship with adk-cc, global
skills belong to the install, and `ADK_CC_SKILLS_DIR` is an operator's explicit
choice — none of those arrive with a cloned repository.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from ..config.schema import env_bool

_log = logging.getLogger(__name__)
_lock = threading.Lock()

# Set for a deployment that cannot ask (headless, CI, a server with no UI):
# project skills load as before. Not a per-project setting — that is what the
# trust list is.
_TRUST_ALL_ENV = "ADK_CC_TRUST_PROJECT_SKILLS"


def _store() -> Path:
    from .. import deployment

    return Path(deployment.data_dir()) / "skill-trust.json"


def _read() -> dict:
    try:
        with open(_store(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _key(root: Path | str) -> str:
    # One canonicalisation (P3): the old OSError fallback keyed the RAW
    # string, which can never match a resolved key — trust for such a root
    # silently never persisted.
    from ..paths import canonical

    return canonical(root)


def is_trusted(root: Optional[Path | str]) -> bool:
    """Has the user agreed to run skills from this folder?"""
    if root is None:
        return False
    if env_bool(_TRUST_ALL_ENV):
        return True
    return _read().get("trusted", {}).get(_key(root)) is True


def set_trusted(root: Path | str, trusted: bool) -> None:
    """Record (or withdraw) trust for one folder."""
    with _lock:
        data = _read()
        entries = dict(data.get("trusted") or {})
        if trusted:
            entries[_key(root)] = True
        else:
            entries.pop(_key(root), None)
        data["trusted"] = entries
        path = _store()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as e:
            _log.warning("skills: could not persist skill trust: %s", e)
    _log.info("skills: %s project skills for %s",
              "trusting" if trusted else "no longer trusting", _key(root))


def trusted_roots() -> list[str]:
    return sorted(_read().get("trusted", {}))
