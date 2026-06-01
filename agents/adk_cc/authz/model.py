"""Core authZ value types: Subject, Action, Resource, Context, Decision.

These are the inputs/outputs of a Policy Decision Point. Kept as plain
frozen dataclasses (no pydantic) — they're internal, hot-path, and never
serialized over the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Effect = Literal["permit", "deny"]


@dataclass(frozen=True)
class Subject:
    """WHO is acting. Built from the authenticated principal (PIP).

    `permissions` is the subject's capability list — a flat set of grants
    resolved upstream (an IdP minting a `permissions` claim, or a gateway
    that did the role→capability expansion). It's the dimension the
    capability gate checks against a tool/agent's declared requirement.
    Kept distinct from `scopes` (raw OAuth `scope`, which a token may carry
    for unrelated reasons) and `roles` (coarse identity groupings).
    """

    user_id: str
    tenant_id: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Action:
    """WHAT is being attempted — a tool name (`run_bash`, `mcp__x__y`) or
    a REST verb (`read_session`, `read_artifact`)."""

    name: str


@dataclass(frozen=True)
class Resource:
    """The target of the action.

    `owner_user_id` / `tenant_id` enable ownership + tenant-isolation
    relations in the PDP. `attrs` carries type-specific extras (e.g. the
    file path, the artifact filename) for glob matching.
    """

    type: str  # e.g. "tool", "file", "artifact", "session", "mcp_server"
    id: str = ""
    owner_user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthzContext:
    """Ambient context for the decision (permission mode, environment).

    `required_permissions` is the capability requirement the PEP resolved
    for THIS action (from the tool/agent's declared attribute ∪ matching
    YAML `requirements:`), injected here so the PDP stays pure — it
    evaluates the requirement it's handed rather than reaching for tool
    metadata itself. Empty = no capability gate for this action.
    """

    mode: Optional[str] = None
    env: Optional[str] = None
    required_permissions: frozenset[str] = frozenset()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """The PDP verdict. `matched` names the rule/relation that decided it
    (for audit + debugging 'why was this denied?')."""

    effect: Effect
    reason: str
    matched: Optional[str] = None

    @property
    def permitted(self) -> bool:
        return self.effect == "permit"
