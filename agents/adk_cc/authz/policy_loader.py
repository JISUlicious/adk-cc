"""PAP: load AbacPolicy rules from the `policies:` block of a YAML file.

Shares the same file as the PermissionPlugin's `rules:` block
(`ADK_CC_PERMISSIONS_YAML`) — backward compatible: an old file with only
`rules:` yields an empty policy list (authZ stays inert), and a new file
can carry both.

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

Each field maps to AbacPolicy; lists become frozensets. Unset = wildcard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .pdp import AbacPolicy


def load_policies_from_yaml(path: str | Path) -> list[AbacPolicy]:
    """Parse the `policies:` list from a YAML file into AbacPolicy rules.

    Returns [] when the file has no `policies:` block. Raises RuntimeError
    if PyYAML isn't installed (lazy import, same as the perm loader).
    """
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required to load YAML policies. "
            "Install with `pip install pyyaml`."
        ) from e

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [_parse_policy(p, i) for i, p in enumerate(raw.get("policies", []) or [])]


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
