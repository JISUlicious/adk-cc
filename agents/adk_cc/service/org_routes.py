"""FastAPI routes for org / team management (Phase 3).

Mounted by `build_fastapi_app` alongside the identity routes when an
IdentityService is configured. Two audiences:

  - Admin-gated, tenant-scoped (`/orgs/*`): an admin manages the members of
    THEIR OWN org — list, invite, change role, disable/enable. The caller's
    tenant comes from their authenticated principal, never the URL, so there's
    no cross-tenant surface to abuse.
  - Public (`/auth/invite/*`): how an invitee joins before they have an
    account — look up an invite by token, then accept it (set a password).

Roles are kept to {admin, member}. The service refuses to demote/disable the
last admin so an org can't lock itself out.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

# Public (auth-exempt) invite endpoints live under this prefix.
PUBLIC_PREFIXES: tuple[str, ...] = ("/auth/invite/",)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


def mount_org_routes(app, identity) -> None:
    router = APIRouter()
    admin_role = identity.admin_role

    def _require_admin(request: Request):
        """Authenticated + holds the admin role. Returns the principal so the
        caller's tenant scopes the operation."""
        auth = getattr(request.state, "adk_cc_auth", None)
        if auth is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        if admin_role not in auth.roles:
            raise HTTPException(status_code=403, detail="admin role required")
        return auth

    # --- admin, scoped to the caller's own tenant -----------------------
    @router.get("/orgs/members")
    async def list_members(request: Request):
        auth = _require_admin(request)
        return {"members": await asyncio.to_thread(identity.list_members, auth.tenant_id)}

    @router.get("/orgs/audit")
    async def audit_log(request: Request):
        auth = _require_admin(request)
        return {"events": await asyncio.to_thread(identity.recent_audit, auth.tenant_id)}

    @router.get("/orgs/usage")
    async def usage(request: Request):
        auth = _require_admin(request)
        return {"users": await asyncio.to_thread(identity.usage_summary, auth.tenant_id)}

    @router.post("/orgs/members")
    async def create_member(request: Request):
        # Admin provisions a user directly (email + initial password + role) in
        # their own org. Complements invites — useful for single-org deployments.
        auth = _require_admin(request)
        body = await _json(request)
        try:
            m = await asyncio.to_thread(identity.provision_member, 
                auth.tenant_id,
                email=body.get("email") or "",
                password=body.get("password") or "",
                name=(body.get("name") or "").strip(),
                role=body.get("role") or "member",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "user.created",
                        target=m["email"], detail=", ".join(m["roles"]))
        return m

    @router.post("/orgs/invites")
    async def create_invite(request: Request):
        auth = _require_admin(request)
        body = await _json(request)
        try:
            inv = await asyncio.to_thread(identity.create_invite, 
                auth.tenant_id, body.get("email") or "", body.get("role") or "member")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "invite.created",
                        target=inv.email, detail=inv.role)
        # Build the shareable accept link from the request's own origin.
        base = str(request.base_url).rstrip("/")
        return {"token": inv.token, "url": f"{base}/invite/{inv.token}",
                "email": inv.email, "role": inv.role, "expires": inv.expires}

    @router.get("/orgs/invites")
    async def list_invites(request: Request):
        auth = _require_admin(request)
        return {"invites": await asyncio.to_thread(identity.list_invites, auth.tenant_id)}

    @router.delete("/orgs/invites/{token}")
    async def revoke_invite(token: str, request: Request):
        auth = _require_admin(request)
        try:
            await asyncio.to_thread(identity.revoke_invite, auth.tenant_id, token)
        except KeyError:
            raise HTTPException(status_code=404, detail="invite not found")
        await identity.record(auth.tenant_id, auth.user_id, "invite.revoked")
        return {"status": "revoked"}

    # --- access requests: user-initiated joins awaiting approval ---------
    @router.get("/orgs/requests")
    async def list_requests(request: Request):
        auth = _require_admin(request)
        return {"requests": await asyncio.to_thread(identity.list_access_requests, auth.tenant_id)}

    @router.post("/orgs/requests/{user_id}/approve")
    async def approve_request(user_id: str, request: Request):
        auth = _require_admin(request)
        body = (await _json(request)) if await request.body() else {}
        try:
            m = await asyncio.to_thread(identity.approve_access_request,
                auth.tenant_id, user_id, body.get("role") or "member")
        except KeyError:
            raise HTTPException(status_code=404, detail="request not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "request.approved",
                        target=m["email"], detail=", ".join(m["roles"]))
        return m

    @router.post("/orgs/requests/{user_id}/reject")
    async def reject_request(user_id: str, request: Request):
        auth = _require_admin(request)
        try:
            m = await asyncio.to_thread(identity.reject_access_request, auth.tenant_id, user_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="request not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "request.rejected", target=m["email"])
        return {"status": "rejected"}

    @router.post("/orgs/members/{user_id}/reset-password")
    async def reset_password(user_id: str, request: Request):
        # Mint a one-time reset link (the invite pattern) the admin delivers
        # out-of-band. The raw token appears only in this response.
        auth = _require_admin(request)
        try:
            raw = await asyncio.to_thread(identity.create_password_reset,
                                          auth.tenant_id, auth.user_id, user_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="member not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        m = await asyncio.to_thread(identity._member_in_tenant, auth.tenant_id, user_id)
        await identity.record(auth.tenant_id, auth.user_id, "password.reset_link",
                        target=m.email)
        base = str(request.base_url).rstrip("/")
        return {"url": f"{base}/reset-password/{raw}", "email": m.email}

    @router.post("/orgs/members/{user_id}/role")
    async def set_role(user_id: str, request: Request):
        auth = _require_admin(request)
        body = await _json(request)
        try:
            m = await asyncio.to_thread(identity.set_member_role, auth.tenant_id, user_id, body.get("role") or "")
        except KeyError:
            raise HTTPException(status_code=404, detail="member not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "member.role",
                        target=m["email"], detail=", ".join(m["roles"]))
        return m

    @router.post("/orgs/members/{user_id}/disable")
    async def disable_member(user_id: str, request: Request):
        auth = _require_admin(request)
        try:
            m = await asyncio.to_thread(identity.set_member_status, auth.tenant_id, user_id, "disabled")
        except KeyError:
            raise HTTPException(status_code=404, detail="member not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "member.disabled", target=m["email"])
        return m

    @router.post("/orgs/members/{user_id}/enable")
    async def enable_member(user_id: str, request: Request):
        auth = _require_admin(request)
        try:
            m = await asyncio.to_thread(identity.set_member_status, auth.tenant_id, user_id, "active")
        except KeyError:
            raise HTTPException(status_code=404, detail="member not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "member.enabled", target=m["email"])
        return m

    # --- public: accept an invite (how you join before having an account) ---
    @router.get("/auth/invite/{token}")
    async def get_invite(token: str):
        info = await asyncio.to_thread(identity.invite_public, token)
        if info is None:
            raise HTTPException(status_code=404, detail="invite invalid or expired")
        return info

    @router.post("/auth/invite/{token}/accept")
    async def accept_invite(token: str, request: Request):
        body = await _json(request)
        password = body.get("password") or ""
        name = (body.get("name") or "").strip()
        try:
            ident = await asyncio.to_thread(identity.accept_invite, token, password=password, name=name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(ident.tenant_id, ident.user_id, "invite.accepted", actor_email=ident.email)
        out = {
            "access_token": identity.token_for(ident),
            "token_type": "Bearer",
            "user": identity.user_dict(ident),
        }
        rt = await asyncio.to_thread(identity.issue_refresh_token, ident.user_id)
        if rt:
            out["refresh_token"] = rt
        return out

    app.include_router(router)
