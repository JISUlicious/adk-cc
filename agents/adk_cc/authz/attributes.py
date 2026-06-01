"""PIP: build authZ Subject + Resource from runtime context.

- `subject_from_state` reads the principal (roles/scopes) that
  TenancyPlugin seeded into session state from the authenticated request.
- `resource_from_tool` turns a tool call into a Resource, reusing the
  PermissionPlugin's `_RULE_KEY_EXTRACTORS` so the resource id (path /
  command / etc.) matches what permission rules already key on. The
  resource is tagged with the caller's tenant + user as owner, since a
  tool acting in a session operates on that user's workspace.
"""

from __future__ import annotations

from typing import Any, Optional

from .model import Resource, Subject

_PRINCIPAL_KEY = "temp:auth_principal"
_TENANT_KEY = "temp:tenant_context"


def subject_from_state(state: Any) -> Subject:
    """Build a Subject from seeded session state.

    Prefers the full principal (roles/scopes) seeded by TenancyPlugin;
    falls back to the tenant_context (user/tenant, no roles) and finally
    to a bare local subject so the PDP always has something to evaluate.
    """
    principal = _safe_get(state, _PRINCIPAL_KEY)
    if isinstance(principal, dict) and principal.get("user_id"):
        return Subject(
            user_id=str(principal.get("user_id") or "local"),
            tenant_id=str(principal.get("tenant_id") or "local"),
            roles=frozenset(principal.get("roles") or ()),
            scopes=frozenset(principal.get("scopes") or ()),
            permissions=frozenset(principal.get("permissions") or ()),
        )
    tenant = _safe_get(state, _TENANT_KEY)
    user_id = getattr(tenant, "user_id", None) or "local"
    tenant_id = getattr(tenant, "tenant_id", None) or "local"
    return Subject(user_id=str(user_id), tenant_id=str(tenant_id))


def resource_from_tool(
    tool_name: str, args: dict, subject: Subject
) -> Resource:
    """Map a tool call to a Resource for authZ.

    The resource id is the tool's "rule key" (path/command/root) via the
    shared extractors; tools without an extractor get an empty id and
    authorize on action+subject only (or a policy that names the tool).
    Owner/tenant = the acting subject (a tool operates on the caller's own
    workspace), so the ownership baseline permits self-directed tool use.
    """
    # Reuse the permission layer's extractors so authZ resource ids line
    # up with what permission rules already match on.
    from ..permissions.rules import _RULE_KEY_EXTRACTORS

    extractor = _RULE_KEY_EXTRACTORS.get(tool_name)
    rid = extractor(args) if extractor is not None else ""
    rtype = _RESOURCE_TYPE.get(tool_name, "tool")
    return Resource(
        type=rtype,
        id=rid,
        owner_user_id=subject.user_id,
        tenant_id=subject.tenant_id,
        attrs={"tool": tool_name},
    )


# Coarse resource-type tagging so policies can target a class of tools
# (e.g. resource_type: file). Tools not listed default to "tool".
_RESOURCE_TYPE: dict[str, str] = {
    "read_file": "file",
    "write_file": "file",
    "edit_file": "file",
    "glob_files": "file",
    "grep": "file",
    "run_bash": "command",
    "save_as_artifact": "artifact",
    "load_artifact_to_sandbox": "artifact",
}


def _safe_get(state: Any, key: str) -> Optional[Any]:
    try:
        return state.get(key)
    except Exception:  # noqa: BLE001
        return None
