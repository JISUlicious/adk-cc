"""PAP: load AbacPolicy rules + capability Requirements from a YAML file.

Shares the same file as the PermissionPlugin's `rules:` block
(`ADK_CC_PERMISSIONS_YAML`) — backward compatible: an old file with only
`rules:` yields empty policy/requirement lists (authZ stays inert), and a
new file can carry `rules:`, `policies:`, and `requirements:` together.

Example:

    policies:
      - effect: deny
        action: run_bash
        resource: "rm *"
      - effect: permit
        roles: [admin]
        action: "read_*"
      - effect: permit
        scopes: ["write:artifacts"]
        action: save_as_artifact
      - effect: deny
        owner: false
        resource_type: artifact

    requirements:
      # To run the deploy tool, the subject must hold `tool:deploy`.
      - match: deploy
        target: tool
        permissions: [tool:deploy]
      # To hand off to the Explore sub-agent, hold `agent:explore`.
      - match: Explore
        target: agent
        permissions: [agent:explore]
      # Gate every MCP tool behind one capability (augments code defaults).
      - match: "mcp__*"
        target: tool
        permissions: [tool:mcp]
        mode: augment   # or `replace` to override code-declared perms

`policies:` fields map to AbacPolicy; `requirements:` fields to Requirement.
Lists become frozensets. Unset = wildcard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .pdp import AbacPolicy
from .requirements import Requirement


def _load_yaml(path: str | Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required to load YAML policies. "
            "Install with `pip install pyyaml`."
        ) from e
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def load_policies_from_yaml(path: str | Path) -> list[AbacPolicy]:
    """Parse the `policies:` list from a YAML file into AbacPolicy rules.

    Returns [] when the file has no `policies:` block. Raises RuntimeError
    if PyYAML isn't installed (lazy import, same as the perm loader).
    """
    raw = _load_yaml(path)
    return [_parse_policy(p, i) for i, p in enumerate(raw.get("policies", []) or [])]


def load_requirements_from_yaml(path: str | Path) -> list[Requirement]:
    """Parse the `requirements:` list from a YAML file into Requirements.

    Returns [] when the file has no `requirements:` block.
    """
    raw = _load_yaml(path)
    return [
        _parse_requirement(r, i)
        for i, r in enumerate(raw.get("requirements", []) or [])
    ]


def _parse_requirement(d: dict[str, Any], idx: int) -> Requirement:
    match = d.get("match")
    if not match or not isinstance(match, str):
        raise ValueError(
            f"requirement[{idx}]: 'match' (a name glob string) is required"
        )
    target = d.get("target", "any")
    if target not in ("tool", "agent", "any"):
        raise ValueError(
            f"requirement[{idx}]: target must be 'tool', 'agent', or 'any', "
            f"got {target!r}"
        )
    mode = d.get("mode", "augment")
    if mode not in ("augment", "replace"):
        raise ValueError(
            f"requirement[{idx}]: mode must be 'augment' or 'replace', "
            f"got {mode!r}"
        )
    return Requirement(
        match=match,
        permissions=_as_set(d.get("permissions")),
        target=target,
        mode=mode,
    )


def _as_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(value.split())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(v) for v in value)
    return frozenset()


def _parse_policy(d: dict[str, Any], idx: int) -> AbacPolicy:
    effect = d.get("effect")
    if effect not in ("permit", "deny"):
        raise ValueError(
            f"policy[{idx}]: effect must be 'permit' or 'deny', got {effect!r}"
        )
    return AbacPolicy(
        effect=effect,
        roles=_as_set(d.get("roles")),
        scopes=_as_set(d.get("scopes")),
        subject_tenant=d.get("subject_tenant"),
        action=d.get("action"),
        resource_type=d.get("resource_type"),
        resource=d.get("resource"),
        owner=d.get("owner"),
        same_tenant=d.get("same_tenant"),
        name=str(d.get("name", f"policy[{idx}]")),
    )
