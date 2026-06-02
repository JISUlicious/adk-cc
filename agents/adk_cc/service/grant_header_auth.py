"""Edge adapter for a gateway-resolved authorization grant header.

This wires a specific upstream grant format into the generic authZ layer
WITHOUT touching the PDP or the PEPs. It supplies two of the layer's
swappable seams:

  - an `AuthExtractor` (`GrantHeaderExtractor`) that reads the per-user
    grant from a request header and FLATTENS it into capability permission
    strings on `Subject.permissions`, and
  - a `RequirementProvider` (`ScopedLevelRequirementProvider`) that, for a
    given (invoking agent, tool, level), produces the matching required
    string — so the PDP's `required ⊆ held` check enforces it.

The header shape (per the gateway):

    {"auth": [
        {"resolvedAt": "...", "authList": [
            {"authYn": true, "authType": "MANAGER", "authSource": "PERSONAL",
             "authSourceDeptCode": null, "authSourceDeptCodes": null,
             "serviceName": "<AGENT NAME>", "authLevel": [1],
             "detailedAuth": [
                 {"funcId": "<TOOL NAME>", "authLevel": 1, ...}
             ]}
        ]}
    ]}

`serviceName` is the ADK AGENT name and `detailedAuth[].funcId` is the ADK
TOOL name (used verbatim — no mapping table). Levels are DISCRETE (each is a
distinct grant; access is exact match, not ≥). Tool grants are PER-AGENT:
`read_file` under agent A is a different grant than under agent B.

Flattened permission vocabulary (a held grant and a required permission use
the SAME strings, so subset-match enforces them):

    svc:{agent}:level:{n}              # service-level access level
    svc:{agent}:role:{authType}        # e.g. svc:Explore:role:MANAGER
    svc:{agent}:source:{authSource}    # e.g. svc:Explore:source:PERSONAL
    svc:{agent}:dept:{code}            # per dept code (service-scoped)
    svc:{agent}:func:{tool}:level:{n}  # PER-AGENT tool access level

`authYn:false` entries are skipped (not granted). `resolvedAt` is not
enforced (freshness is the gateway/transport's job). `objectId` is ignored.

SECURITY: a request header is caller-controlled unless something upstream
guarantees otherwise. This extractor is only safe when adk-cc sits behind a
trusted gateway that is the SOLE ingress and STRIPS this header from inbound
client requests before injecting its own. See `make_auth_middleware`'s
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

def perm_service_level(agent: str, level: Any) -> str:
    return f"svc:{agent}:level:{level}"


def perm_service_role(agent: str, role: str) -> str:
    return f"svc:{agent}:role:{role}"


def perm_service_source(agent: str, source: str) -> str:
    return f"svc:{agent}:source:{source}"


def perm_service_dept(agent: str, code: str) -> str:
    return f"svc:{agent}:dept:{code}"


def perm_tool_level(agent: str, tool: str, level: Any) -> str:
    return f"svc:{agent}:func:{tool}:level:{level}"


def _dept_codes(item: dict) -> list[str]:
    """Tolerate both the singular and plural dept fields; either may be null."""
    out: list[str] = []
    one = item.get("authSourceDeptCode")
    if one:
        out.append(str(one))
    many = item.get("authSourceDeptCodes")
    if isinstance(many, (list, tuple)):
        out.extend(str(c) for c in many if c)
    return out


def flatten_grant(auth: list) -> frozenset[str]:
    """Flatten the gateway grant (`header.auth`) into capability strings.

    Skips `authYn:false` entries and entries without a serviceName. Unions
    across all `auth[]` batches and all `authList[]` items.
    """
    perms: set[str] = set()
    for batch in auth or []:
        for item in batch.get("authList", []) or []:
            if not item.get("authYn"):
                continue
            svc = item.get("serviceName")
            if not svc:
                continue
            for lvl in item.get("authLevel") or []:
                perms.add(perm_service_level(svc, lvl))
            if item.get("authType"):
                perms.add(perm_service_role(svc, item["authType"]))
            if item.get("authSource"):
                perms.add(perm_service_source(svc, item["authSource"]))
            for code in _dept_codes(item):
                perms.add(perm_service_dept(svc, code))
            for d in item.get("detailedAuth") or []:
                fid, flvl = d.get("funcId"), d.get("authLevel")
                if fid is not None and flvl is not None:
                    perms.add(perm_tool_level(svc, fid, flvl))
    return frozenset(perms)


# -- AuthExtractor: header → AuthPrincipal ---------------------------------

class GrantHeaderExtractor:
    """AuthExtractor that builds the principal from the gateway grant header.

    The grant (a JSON object with an `auth` array) is read from
    `header_name` (default `X-Auth-Grant`); the user/tenant come from their
    own headers (the grant carries authorization, not identity). All three
    header names are configurable. Flattened grant → `Subject.permissions`.
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


# -- RequirementProvider: per-agent, exact-level ---------------------------

class ScopedLevelRequirementProvider(RequirementProvider):
    """Per-agent, exact-level requirement provider for the grant scheme.

    Maps the action (invoking agent + tool/agent name) to the SAME string
    format `flatten_grant` emits, so the held grant and the required
    permission line up under the PDP's subset check:

      - tool  → `svc:{invoking_agent}:func:{tool}:level:{N}`
      - agent → `svc:{agent}:level:{N}`

    The level `N` is declared per (agent, tool) / per agent via the config
    maps (the gateway header is the user's GRANT; these maps are the
    deployment's REQUIREMENT spec). Names absent from the maps are ungated
    (empty requirement) unless `closed_world=True`, in which case an
    unmapped tool/agent is given an unsatisfiable requirement (deny).
    """

    def __init__(
        self,
        *,
        tool_levels: Optional[dict[tuple[str, str], Any]] = None,
        agent_levels: Optional[dict[str, Any]] = None,
        agent_roles: Optional[dict[str, str]] = None,
        closed_world: bool = False,
    ) -> None:
        # tool_levels keyed by (agent_name, tool_name) → required level.
        self._tool_levels = dict(tool_levels or {})
        self._agent_levels = dict(agent_levels or {})
        self._agent_roles = dict(agent_roles or {})
        self._closed_world = closed_world

    def for_tool(
        self,
        tool_name: str,
        *,
        tool_meta: Any = None,
        invoking_agent: Optional[str] = None,
    ) -> frozenset[str]:
        agent = invoking_agent or ""
        key = (agent, tool_name)
        if key in self._tool_levels:
            return frozenset(
                {perm_tool_level(agent, tool_name, self._tool_levels[key])}
            )
        if self._closed_world:
            # Unmapped tool under closed-world → unsatisfiable (no grant can
            # match this sentinel), so the PDP denies.
            return frozenset({f"svc:{agent}:func:{tool_name}:DENY"})
        return frozenset()

    def for_agent(self, agent_name: str) -> frozenset[str]:
        required: set[str] = set()
        if agent_name in self._agent_levels:
            required.add(perm_service_level(agent_name, self._agent_levels[agent_name]))
        if agent_name in self._agent_roles:
            required.add(perm_service_role(agent_name, self._agent_roles[agent_name]))
        if required:
            return frozenset(required)
        if self._closed_world:
            return frozenset({f"svc:{agent_name}:DENY"})
        return frozenset()
