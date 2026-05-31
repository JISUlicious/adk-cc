"""Authorization (authZ) layer for adk-cc.

A proper `subject × action × resource` authorization model, distinct from
the confirmation-focused `PermissionPlugin`. Modeled on the standard
PDP/PEP/PIP/PAP decomposition:

  - PDP (decide)   : `pdp.PolicyDecisionPoint` (abstract) +
                     `pdp.AbacPolicyDecisionPoint` (default, ABAC).
  - PEP (enforce)  : `plugins/authz.py` (tool calls) +
                     `service/authz_routes.py` (REST data access).
  - PIP (attrs)    : `attributes.py` (subject from session state, resource
                     from tool args).
  - PAP (policy)   : `policy_loader.py` (the `policies:` YAML block).

Default-OFF: inert unless `ADK_CC_AUTHZ=1`. Dev / single-tenant behavior
is unchanged when disabled.
"""

from .model import (
    Action,
    AuthzContext,
    Decision,
    Effect,
    Resource,
    Subject,
)
from .pdp import AbacPolicy, AbacPolicyDecisionPoint, PolicyDecisionPoint

__all__ = [
    "Action",
    "AuthzContext",
    "Decision",
    "Effect",
    "Resource",
    "Subject",
    "AbacPolicy",
    "AbacPolicyDecisionPoint",
    "PolicyDecisionPoint",
]
