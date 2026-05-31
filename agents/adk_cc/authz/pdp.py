"""Policy Decision Point: the pure `authorize(...)` decision function.

`PolicyDecisionPoint` is the abstract interface — swap in an external
engine (OPA/Cerbos) later without touching the PEPs. `AbacPolicyDecisionPoint`
is the default: attribute-based rules + an ownership/tenant baseline,
closed-world (unmatched ⇒ deny).

The PDP is pure and side-effect-free (mirrors `permissions/engine.py::
decide`). Audit emission happens at the PEP, not here, so the PDP stays
trivially testable.
"""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from .model import Action, AuthzContext, Decision, Effect, Resource, Subject


class PolicyDecisionPoint(ABC):
    """Decide whether `subject` may perform `action` on `resource`."""

    @abstractmethod
    def authorize(
        self,
        subject: Subject,
        action: Action,
        resource: Resource,
        context: AuthzContext,
    ) -> Decision:
        ...


@dataclass(frozen=True)
class AbacPolicy:
    """One ABAC rule. All set predicates must match (AND). Unset = wildcard.

    - effect:        "permit" | "deny"
    - roles:         subject must hold ANY of these roles
    - scopes:        subject must hold ANY of these scopes
    - subject_tenant:subject.tenant_id must equal this
    - action:        glob on action.name (e.g. "mcp__*", "read_*")
    - resource_type: resource.type must equal this
    - resource:      glob on resource.id (e.g. "/etc/*", "*.csv")
    - owner:         if True, subject.user_id must == resource.owner_user_id
    - same_tenant:   if True, subject.tenant_id must == resource.tenant_id
    """

    effect: Effect
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    subject_tenant: Optional[str] = None
    action: Optional[str] = None
    resource_type: Optional[str] = None
    resource: Optional[str] = None
    owner: Optional[bool] = None
    same_tenant: Optional[bool] = None
    name: str = "policy"

    def matches(
        self, subject: Subject, action: Action, resource: Resource
    ) -> bool:
        if self.roles and not (self.roles & subject.roles):
            return False
        if self.scopes and not (self.scopes & subject.scopes):
            return False
        if self.subject_tenant is not None and subject.tenant_id != self.subject_tenant:
            return False
        if self.action is not None and not fnmatch.fnmatch(action.name, self.action):
            return False
        if self.resource_type is not None and resource.type != self.resource_type:
            return False
        if self.resource is not None and not fnmatch.fnmatch(
            resource.id, self.resource
        ):
            return False
        if self.owner is not None:
            is_owner = (
                resource.owner_user_id is not None
                and resource.owner_user_id == subject.user_id
            )
            if is_owner != self.owner:
                return False
        if self.same_tenant is not None:
            same = (
                resource.tenant_id is not None
                and resource.tenant_id == subject.tenant_id
            )
            if same != self.same_tenant:
                return False
        return True


class AbacPolicyDecisionPoint(PolicyDecisionPoint):
    """Default PDP. Evaluation order (first decisive wins):

      1. Any matching DENY policy   → deny (deny always wins).
      2. Any matching PERMIT policy → permit.
      3. Ownership/tenant baseline  → permit if the subject owns the
         resource or it's in the subject's own tenant (and the resource
         carries an owner/tenant to compare).
      4. Closed-world default       → deny.

    The baseline (step 3) means the common "users act on their own stuff"
    case needs no explicit policy; operators add policies only to GRANT
    cross-tenant/role access or to DENY specific things.
    """

    def __init__(self, policies: Optional[list[AbacPolicy]] = None) -> None:
        self._policies = list(policies or [])

    def authorize(
        self,
        subject: Subject,
        action: Action,
        resource: Resource,
        context: AuthzContext,
    ) -> Decision:
        # 1. explicit DENY wins.
        for p in self._policies:
            if p.effect == "deny" and p.matches(subject, action, resource):
                return Decision("deny", f"denied by policy {p.name!r}", p.name)
        # 2. explicit PERMIT.
        for p in self._policies:
            if p.effect == "permit" and p.matches(subject, action, resource):
                return Decision("permit", f"permitted by policy {p.name!r}", p.name)
        # 3. ownership / same-tenant baseline.
        if (
            resource.owner_user_id is not None
            and resource.owner_user_id == subject.user_id
        ):
            return Decision("permit", "subject owns the resource", "baseline:owner")
        if (
            resource.tenant_id is not None
            and resource.tenant_id == subject.tenant_id
        ):
            return Decision(
                "permit", "resource in subject's tenant", "baseline:tenant"
            )
        # 4. closed-world default-deny.
        return Decision("deny", "no matching policy (default deny)", None)
