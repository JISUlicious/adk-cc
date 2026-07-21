"""REST Policy Enforcement Point — closes the trust-the-path hole.

ADK's session/artifact routes take the user from the URL path
(`/apps/{app}/users/{user_id}/sessions/{sid}/...`), NOT from the
authenticated principal — so without this, an authenticated caller can
read/modify ANOTHER user's sessions and artifacts just by changing the
path. This is invisible to the tool-call PEP (it's not a tool call), so
it needs its own request-layer gate.

This middleware matches `/apps/{app}/users/{path_user}/...` requests and,
via the PDP, requires the authenticated principal to be authorized for
that path-user's resources — by default: same user, or same tenant + a
role that grants cross-user access. Default-OFF: inert unless
`ADK_CC_AUTHZ=1`, so existing path-trust deployments are unchanged.

Implemented as middleware (not per-route Depends) because the routes live
inside ADK's `get_fast_api_app` and we don't define them.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from ..config.schema import env_bool
from ..authz import (
    Action,
    AuthzContext,
    PolicyDecisionPoint,
    Resource,
    Subject,
)

# /apps/{app}/users/{user_id}/...  → capture app + user_id.
_USER_PATH = re.compile(r"^/apps/([^/]+)/users/([^/]+)(?:/|$)")


def make_authz_middleware(pdp: PolicyDecisionPoint):
    """Build the REST authZ middleware bound to a PDP.

    Enforces, on `/apps/{app}/users/{path_user}/...` requests:
      - 401 if unauthenticated,
      - the PDP verdict on (subject=caller, action=read/write per method,
        resource={type:'user_data', owner=path_user, tenant=caller_tenant}).
    The ownership baseline means caller==path_user is permitted with no
    policy; a cross-user grant needs a `policies:` rule (e.g. role:admin).
    Non-matching paths pass through.
    """
    from fastapi import Request  # noqa: F401 — type only
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response

    class _RestAuthzMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Default-OFF, checked per-request so tests/env toggles apply.
            if not env_bool("ADK_CC_AUTHZ"):
                return await call_next(request)

            m = _USER_PATH.match(request.url.path)
            if m is None:
                return await call_next(request)  # not a user-scoped route

            app_name, path_user = m.group(1), m.group(2)
            principal = getattr(request.state, "adk_cc_auth", None)
            if principal is None:
                return Response("not authenticated", status_code=401, media_type="text/plain")

            subject = Subject(
                user_id=principal[0],
                tenant_id=principal[1],
                roles=getattr(principal, "roles", frozenset()),
                scopes=getattr(principal, "scopes", frozenset()),
            )
            # Resource = the path-user's data, owned by path_user. We do
            # NOT set resource.tenant_id to the caller's tenant — that
            # would make the same-tenant baseline trivially true and
            # defeat the gate. With tenant_id=None, only the OWNER baseline
            # (caller == path_user) or an explicit policy permits; every
            # other-user access is denied by default. A cross-user grant
            # (e.g. role:admin) is an explicit `policies:` rule.
            resource = Resource(
                type="user_data",
                id=f"{app_name}/{path_user}",
                owner_user_id=path_user,
                tenant_id=None,
                attrs={"app": app_name},
            )
            action = Action("write_user_data" if request.method in ("POST", "PUT", "PATCH", "DELETE") else "read_user_data")
            decision = pdp.authorize(subject, action, resource, AuthzContext())
            if decision.effect == "deny":
                return Response(
                    f"forbidden: {decision.reason}",
                    status_code=403,
                    media_type="text/plain",
                )
            return await call_next(request)

    return _RestAuthzMiddleware
