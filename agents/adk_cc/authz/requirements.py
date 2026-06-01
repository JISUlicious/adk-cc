"""Capability requirements: what permission(s) a tool/agent demands.

A *requirement* says "to invoke this tool/agent, the subject must hold these
capability permissions." Requirements come from two sources, combined by
`RequirementResolver`:

  - CODE: a tool's `ToolMeta.required_permissions`, or an agent's entry in
    the `AGENT_REQUIRED_PERMISSIONS` registry (agents have no metadata slot,
    so a name→perms map is the equivalent). Travels with the code.
  - YAML: a `requirements:` block in `ADK_CC_PERMISSIONS_YAML`, so operators
    can gate tools/agents without a code change. Each entry can `augment`
    (default — union onto the code requirement) or `replace` it.

The resolved set is handed to the PDP via `AuthzContext.required_permissions`;
the PDP enforces AND semantics (subject must hold ALL). Empty = ungated.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Requirement:
    """One YAML `requirements:` entry.

    - match:       glob on the tool/agent name (e.g. "deploy", "mcp__*").
    - permissions: capability permissions this entry contributes.
    - target:      "tool" | "agent" | "any" — which kind of name `match`
                   applies to (so a tool and an agent can share a name
                   without cross-gating).
    - mode:        "augment" (union onto the code requirement, default) or
                   "replace" (discard the code requirement + any prior
                   matches, start from these).
    """

    match: str
    permissions: frozenset[str]
    target: str = "any"
    mode: str = "augment"

    def applies_to(self, name: str, target: str) -> bool:
        if self.target not in ("any", target):
            return False
        return fnmatch.fnmatch(name, self.match)


class RequirementResolver:
    """Resolves the effective capability requirement for a tool/agent.

    Combines the code-declared `base` requirement with YAML requirements,
    in YAML order: an `augment` entry unions its permissions on; a `replace`
    entry discards everything accumulated so far (base + prior matches) and
    restarts from its own permissions. Order is the YAML file order, so a
    `replace` near the end wins over earlier augments — documented and
    deterministic.
    """

    def __init__(self, requirements: list[Requirement] | None = None) -> None:
        self._requirements = list(requirements or [])

    def resolve(
        self,
        name: str,
        *,
        target: str,
        base: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        effective = set(base)
        for req in self._requirements:
            if not req.applies_to(name, target):
                continue
            if req.mode == "replace":
                effective = set(req.permissions)
            else:  # augment
                effective |= req.permissions
        return frozenset(effective)
