"""Edge adapter for a gateway-resolved authorization grant header.

The gateway is FULLY AUTHORITATIVE: it has already decided what each user
may use, and forwards that decision as a per-request grant header. adk-cc's
job is only to ENFORCE it — if a (agent, tool) pair is present in the grant,
the call is allowed; if not, it's denied. There is no level/role/dept
comparison and no per-tool requirement map to maintain: the requirement is
*presence in the grant*, derived from the call itself.

This wires the gateway's format into the generic authZ layer WITHOUT
touching the PDP or the PEPs, via two swappable seams:

  - `GrantHeaderExtractor` (an `AuthExtractor`): reads the per-user grant
    from a request header and FLATTENS it into presence strings on
    `Subject.permissions`.
  - `PresenceRequirementProvider` (a `RequirementProvider`): for a tool call
    under agent A, requires `svc:A:func:{tool}`; for an agent handoff,
    requires `svc:{agent}`. The PDP's `required ⊆ held` check then enforces
    presence.

The header shape (per the gateway):

    {"auth": [
        {"resolvedAt": "...", "authList": [
            {"authYn": true, "serviceName": "<AGENT NAME>",
             "detailedAuth": [{"funcId": "<TOOL NAME>", ...}],
             ...other fields ignored...}
        ]}
    ]}

`serviceName` is the ADK AGENT name and `detailedAuth[].funcId` is the ADK
TOOL name (used verbatim — no mapping table). Tool grants are PER-AGENT:
`read_file` under agent A is a different grant than under agent B.

Flattened presence vocabulary (held grant and required permission use the
SAME strings, so subset-match enforces them):

    svc:{agent}                 # may invoke this agent
    svc:{agent}:func:{tool}     # may use this tool UNDER this agent

`authYn:false` entries are skipped (not granted). `authLevel`, `authType`,
`authSource`, dept codes, `resolvedAt`, and `objectId` are all IGNORED — the
gateway's grant/deny decision is the whole signal.

Entry-agent exemption: ADK fires the agent gate for the ROOT agent too, so
the entry agent (the coordinator) must be exempt or every session would be
denied. `PresenceRequirementProvider(exempt_agents=...)` returns no
requirement for those names.

SECURITY: a request header is caller-controlled unless something upstream
guarantees otherwise. This is only safe when adk-cc sits behind a trusted
gateway that is the SOLE ingress and STRIPS this header from inbound client
requests before injecting its own. See `make_auth_middleware`'s
gateway-header note for the same boundary.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from ..authz import RequirementProvider
from .auth import AuthPrincipal

try:
    from fastapi import HTTPException
    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False


# -- shared string format (held grant == required permission) --------------

def perm_agent(agent: str) -> str:
    """Presence string: may invoke this agent."""
    return f"svc:{agent}"


def perm_tool(agent: str, tool: str) -> str:
    """Presence string: may use this tool UNDER this agent (per-agent)."""
    return f"svc:{agent}:func:{tool}"


def flatten_grant(auth: list) -> frozenset[str]:
    """Flatten the gateway grant (`header.auth`) into presence strings.

    Emits `svc:{serviceName}` for each granted service and
    `svc:{serviceName}:func:{funcId}` for each granted function. Skips
    `authYn:false` entries and entries without a serviceName. Unions across
    all `auth[]` batches and `authList[]` items. All other fields (levels,
    role, source, dept, resolvedAt, objectId) are ignored — presence is the
    whole signal.
    """
    perms: set[str] = set()
    for batch in auth or []:
        for item in batch.get("authList", []) or []:
            if not item.get("authYn"):
                continue
            svc = item.get("serviceName")
            if not svc:
                continue
            perms.add(perm_agent(svc))
            for d in item.get("detailedAuth") or []:
                fid = d.get("funcId")
                if fid:
                    perms.add(perm_tool(svc, fid))
    return frozenset(perms)


# -- AuthExtractor: header → AuthPrincipal ---------------------------------

class GrantHeaderExtractor:
    """AuthExtractor that builds the principal from the gateway grant header.

    The grant (a JSON object with an `auth` array) is read from
    `header_name` (default `X-Auth-Grant`); user/tenant come from their own
    headers (the grant carries authorization, not identity). All header
    names are configurable. Flattened grant → `Subject.permissions`.
    """

    def __init__(
        self,
        *,
        header_name: str = "X-Auth-Grant",
        user_header: str = "X-Auth-User",
        tenant_header: str = "X-Auth-Tenant",
        default_tenant: str = "default",
    ) -> None:
        self._header = header_name
        self._user_header = user_header
        self._tenant_header = tenant_header
        self._default_tenant = default_tenant

    async def __call__(self, request: Any) -> AuthPrincipal:
        if not _FASTAPI_AVAILABLE:
            raise RuntimeError("fastapi is not installed")
        raw = request.headers.get(self._header)
        if not raw:
            raise HTTPException(status_code=401, detail="missing grant header")
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="malformed grant header")
        auth = payload.get("auth") if isinstance(payload, dict) else None
        if not isinstance(auth, list):
            raise HTTPException(status_code=401, detail="grant header has no auth[]")

        user = request.headers.get(self._user_header)
        if not user:
            raise HTTPException(status_code=401, detail="missing user header")
        tenant = request.headers.get(self._tenant_header) or self._default_tenant

        permissions = flatten_grant(auth)
        return AuthPrincipal(user, tenant, frozenset(), frozenset(), permissions)


# -- RequirementProvider: presence, per-agent ------------------------------

class PresenceRequirementProvider(RequirementProvider):
    """Presence-based requirement provider (gateway fully authoritative).

    Derives the required permission from the call itself — no per-tool/agent
    config map. The PDP then checks it against the flattened grant:

      - tool  → `svc:{invoking_agent}:func:{tool}` must be present
      - agent → `svc:{agent}` must be present

    Tool requirements are PER-AGENT: the same tool under a different agent
    requires a different presence string, so a grant for `read_file` under
    `Explore` does not authorize `read_file` under another agent.

    `exempt_agents` are returned ungated (empty requirement) — required for
    the ENTRY agent, since ADK fires the agent gate for the root too and it
    is normally not part of any grant. Defaults to the configured entry
    agent name.
    """

    def __init__(self, *, exempt_agents: Optional[set[str]] = None) -> None:
        self._exempt = set(exempt_agents or ())

    def for_tool(
        self,
        tool_name: str,
        *,
        tool_meta: Any = None,
        invoking_agent: Optional[str] = None,
    ) -> frozenset[str]:
        # No invoking agent known (shouldn't happen in normal flow) → cannot
        # form a per-agent presence string; fail closed with an unsatisfiable
        # requirement so the PDP denies rather than silently allowing.
        if not invoking_agent:
            return frozenset({f"svc:?:func:{tool_name}"})
        if invoking_agent in self._exempt:
            return frozenset()
        return frozenset({perm_tool(invoking_agent, tool_name)})

    def for_agent(self, agent_name: str) -> frozenset[str]:
        if agent_name in self._exempt:
            return frozenset()
        return frozenset({perm_agent(agent_name)})


# -- env-driven construction (configurable, no code edits) -----------------

def grant_provider_from_env() -> Optional[PresenceRequirementProvider]:
    """Build a PresenceRequirementProvider when the grant scheme is enabled.

    Enabled by `ADK_CC_GRANT_HEADER=1`. `ADK_CC_GRANT_EXEMPT_AGENTS` is a
    comma-separated list of agent names to leave ungated (the entry agent);
    defaults to "coordinator". Returns None when the scheme is off, so the
    caller falls back to the default declared-requirement provider.
    """
    if os.environ.get("ADK_CC_GRANT_HEADER") != "1":
        return None
    raw = os.environ.get("ADK_CC_GRANT_EXEMPT_AGENTS", "coordinator")
    exempt = {a.strip() for a in raw.split(",") if a.strip()}
    return PresenceRequirementProvider(exempt_agents=exempt)


def grant_extractor_from_env() -> Optional[GrantHeaderExtractor]:
    """Build a GrantHeaderExtractor when the grant scheme is enabled.

    Enabled by `ADK_CC_GRANT_HEADER=1`. Header names are overridable via
    `ADK_CC_GRANT_HEADER_NAME`, `ADK_CC_GRANT_USER_HEADER`,
    `ADK_CC_GRANT_TENANT_HEADER`. Returns None when the scheme is off.
    """
    if os.environ.get("ADK_CC_GRANT_HEADER") != "1":
        return None
    return GrantHeaderExtractor(
        header_name=os.environ.get("ADK_CC_GRANT_HEADER_NAME", "X-Auth-Grant"),
        user_header=os.environ.get("ADK_CC_GRANT_USER_HEADER", "X-Auth-User"),
        tenant_header=os.environ.get("ADK_CC_GRANT_TENANT_HEADER", "X-Auth-Tenant"),
    )
