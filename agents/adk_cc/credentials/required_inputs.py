"""Declared secret/env requirements for skills (Phase 3).

The Agent Skills open spec has no field for declaring required env/secrets, but
sanctions `metadata` for client-specific data. So a skill declares the secrets
it needs under the namespaced key `metadata["x-adk-cc/secrets"]` (a JSON list,
shaped like VS Code MCP `inputs`):

    metadata:
      x-adk-cc/secrets: |
        [{"id": "GITHUB_TOKEN", "description": "GitHub PAT for pushes", "secret": true}]

This registry unions those declarations across the installed skills. Two uses:
  1. SCOPE injection — the sandbox injects only the user's secrets whose key is
     declared-required (least privilege), instead of the user's whole wallet.
     (When NOTHING is declared anywhere, injection falls back to all user
     secrets so the feature still works pre-declaration.)
  2. PROMPT in the UI — `/auth/secrets` lists declared inputs + whether the user
     has set them, so the Settings page can ask for the missing ones.

MCP server tokens are NOT here: they're connection auth (resolved per-server via
`credential_key`), not sandbox env — a different injection target.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

_log = logging.getLogger(__name__)

SECRETS_METADATA_KEY = "x-adk-cc/secrets"


@dataclass(frozen=True)
class RequiredInput:
    id: str
    description: str = ""
    secret: bool = True
    source: str = ""  # e.g. "skill:pdf-processing"


def _parse_declaration(raw: Any, *, source: str) -> list[RequiredInput]:
    """Parse a `metadata["x-adk-cc/secrets"]` value into RequiredInputs.

    Accepts a JSON array/object of `{id, description?, secret?}`, a bare JSON
    string id, or a plain comma list of ids. Malformed → skipped with a debug
    log (never fatal; a bad manifest must not break skill loading)."""
    if raw is None:
        return []
    data: Any = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s[0] in "[{":
            try:
                data = json.loads(s)
            except json.JSONDecodeError as e:
                _log.debug("%s: invalid x-adk-cc/secrets JSON: %s", source, e)
                return []
        else:
            # plain comma list of ids
            return [
                RequiredInput(id=x.strip(), source=source)
                for x in s.split(",")
                if x.strip()
            ]
    items = data if isinstance(data, list) else [data]
    out: list[RequiredInput] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            out.append(RequiredInput(id=it.strip(), source=source))
        elif isinstance(it, dict) and str(it.get("id", "")).strip():
            out.append(
                RequiredInput(
                    id=str(it["id"]).strip(),
                    description=str(it.get("description", "")),
                    secret=bool(it.get("secret", True)),
                    source=source,
                )
            )
    return out


def discover_skill_required_inputs() -> list[RequiredInput]:
    """Union of declared required inputs across all installed skills (first id
    wins on dups). Best-effort: any discovery error → empty list."""
    try:
        from ..tools.skills import discover_skills

        skills = discover_skills()
    except Exception as e:  # noqa: BLE001
        _log.debug("required-inputs skill discovery failed (%s: %s)", type(e).__name__, e)
        return []
    seen: dict[str, RequiredInput] = {}
    for sk in skills:
        fm = getattr(sk, "frontmatter", None)
        md = getattr(fm, "metadata", None) or {}
        raw = md.get(SECRETS_METADATA_KEY) if isinstance(md, dict) else None
        if not raw:
            continue
        for ri in _parse_declaration(raw, source=f"skill:{getattr(fm, 'name', '?')}"):
            seen.setdefault(ri.id, ri)
    return list(seen.values())


_CACHE: Optional[list[RequiredInput]] = None


def required_inputs(*, refresh: bool = False) -> list[RequiredInput]:
    """Cached union of declared inputs (skills don't change at runtime)."""
    global _CACHE
    if _CACHE is None or refresh:
        _CACHE = discover_skill_required_inputs()
    return _CACHE


def declared_secret_keys(*, refresh: bool = False) -> set[str]:
    """Just the declared ids — the allowlist used to scope sandbox injection.
    Empty set when nothing is declared (caller then falls back to all secrets)."""
    return {ri.id for ri in required_inputs(refresh=refresh)}
