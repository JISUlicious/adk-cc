"""FastAPI routes for the in-house email+password identity provider.

Mounted by `build_fastapi_app` only when an IdentityService is configured
(`ADK_CC_AUTH_PASSWORD=1`). Public (auth-exempt) routes: /auth/login,
/auth/signup, /auth/config, /.well-known/jwks.json. /auth/me and /auth/logout
run behind the auth middleware.

The routes are deliberately variant-agnostic: they read `provider.describe()`
and call the provider's methods, so an OIDC/Keycloak provider reuses this module
(adding /auth/sso/* handlers) without rewriting login/config/jwks.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response

# Public paths the auth middleware must let through unauthenticated (you can't
# present a token before you have one). Kept here so server.py imports one list.
PUBLIC_PATHS: tuple[str, ...] = (
    "/auth/login",
    "/auth/signup",
    "/auth/config",
    "/.well-known/jwks.json",
)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return body


def _safe_secret_key(key: str) -> str:
    safe = "".join(c for c in key if c.isalnum() or c in "-_")
    if safe != key or not safe:
        raise HTTPException(status_code=400, detail="invalid secret key name")
    return safe


def mount_identity_routes(app, identity, credentials=None) -> None:
    router = APIRouter()

    @router.get("/auth/config")
    async def auth_config():
        # Tells the SPA which login methods are live so it renders the right
        # form (email+password vs SSO buttons vs token paste).
        d = identity.provider.describe()
        d["mode"] = identity.mode
        return d

    @router.get("/.well-known/jwks.json")
    async def jwks():
        return identity.issuer.public_jwks()

    @router.post("/auth/login")
    async def login(request: Request):
        body = await _json(request)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            raise HTTPException(status_code=400, detail="email and password required")
        ident = await identity.provider.login_password(email, password)
        if ident is None:
            raise HTTPException(status_code=401, detail="invalid email or password")
        await identity.record(ident.tenant_id, ident.user_id, "login", actor_email=ident.email)
        return {
            "access_token": identity.token_for(ident),
            "token_type": "Bearer",
            "user": identity.user_dict(ident),
        }

    @router.post("/auth/signup")
    async def signup(request: Request):
        if not identity.provider.supports_registration:
            raise HTTPException(status_code=403, detail="self-registration is disabled")
        body = await _json(request)
        try:
            ident = await identity.provider.register(
                email=(body.get("email") or "").strip(),
                password=body.get("password") or "",
                name=(body.get("name") or "").strip(),
                org=(body.get("org") or "").strip(),
            )
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(ident.tenant_id, ident.user_id, "signup", actor_email=ident.email)
        return {
            "access_token": identity.token_for(ident),
            "token_type": "Bearer",
            "user": identity.user_dict(ident),
        }

    def _require_auth(request: Request):
        auth = getattr(request.state, "adk_cc_auth", None)
        if auth is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return auth

    @router.get("/auth/me")
    async def me(request: Request):
        auth = _require_auth(request)
        try:
            prof = await asyncio.to_thread(identity.profile, auth.user_id)
        except KeyError:
            prof = {"id": auth.user_id, "email": "", "name": "", "tenant": auth.tenant_id}
        return {**prof, "roles": sorted(auth.roles), "scopes": sorted(auth.scopes)}

    @router.patch("/auth/profile")
    async def update_profile(request: Request):
        auth = _require_auth(request)
        body = await _json(request)
        try:
            prof = await asyncio.to_thread(identity.update_profile, auth.user_id, name=body.get("name") or "")
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found")
        await identity.record(auth.tenant_id, auth.user_id, "profile.updated")
        return prof

    @router.post("/auth/password")
    async def change_password(request: Request):
        auth = _require_auth(request)
        body = await _json(request)
        try:
            await asyncio.to_thread(identity.change_password, 
                auth.user_id,
                current=body.get("current_password") or "",
                new=body.get("new_password") or "",
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "password.changed")
        return {"status": "ok"}

    @router.get("/auth/api-keys")
    async def list_api_keys(request: Request):
        auth = _require_auth(request)
        return {"keys": await asyncio.to_thread(identity.list_api_keys, auth.user_id)}

    @router.post("/auth/api-keys")
    async def create_api_key(request: Request):
        auth = _require_auth(request)
        body = await _json(request)
        try:
            rec, token = await asyncio.to_thread(identity.create_api_key, auth.user_id, name=body.get("name") or "")
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "apikey.created", target=rec.name)
        # The token is returned ONCE here and never again.
        return {"id": rec.id, "name": rec.name, "created": rec.created, "token": token}

    @router.delete("/auth/api-keys/{key_id}")
    async def revoke_api_key(key_id: str, request: Request):
        auth = _require_auth(request)
        try:
            await asyncio.to_thread(identity.revoke_api_key, auth.user_id, key_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="api key not found")
        await identity.record(auth.tenant_id, auth.user_id, "apikey.revoked", target=key_id)
        return {"status": "revoked"}

    @router.post("/auth/logout")
    async def logout():
        # Stateless JWT: the client drops the token. Endpoint exists for a
        # consistent client API and as the future home for session revocation.
        return Response(status_code=204)

    # ---- per-user secrets (Settings → Secrets) ---------------------------
    # Self-service personal credentials for skills/MCP, resolved user-over-
    # tenant at use time. Mounted only when a CredentialProvider is configured.
    # NEVER returns values — names + scope only. Writes go to the caller's
    # PERSONAL scope; tenant-shared secrets are admin-managed elsewhere.
    if credentials is not None:

        @router.get("/auth/secrets")
        async def list_secrets(request: Request):
            auth = _require_auth(request)
            personal = set(
                await credentials.list_keys(tenant_id=auth.tenant_id, user_id=auth.user_id)
            )
            shared = set(await credentials.list_keys(tenant_id=auth.tenant_id))

            def status_of(key: str) -> str:
                if key in personal:
                    return "user"      # set personally
                if key in shared:
                    return "tenant"    # provided by the org (read-only here)
                return "unset"

            def item(ri) -> dict:
                return {
                    "key": ri.id,
                    "status": status_of(ri.id),
                    "description": ri.description,
                    "required": True,
                }

            # Declared inputs grouped by owning skill / MCP server, so the UI
            # can render one section each and badge the ones not yet set. Names
            # + status ONLY, never values.
            try:
                from ..credentials.required_inputs import discover_groups

                raw_groups = await discover_groups(auth.tenant_id, auth.user_id)
            except Exception:  # noqa: BLE001
                raw_groups = []
            declared_ids: set[str] = set()
            groups = []
            missing_required = 0
            for g in raw_groups:
                inputs = [item(ri) for ri in g.inputs]
                miss = sum(1 for it in inputs if it["status"] == "unset")
                missing_required += miss
                declared_ids.update(it["key"] for it in inputs)
                groups.append({"kind": g.kind, "name": g.name, "inputs": inputs, "missing": miss})

            # keys the user set that no skill/MCP declares (custom)
            other = [
                {"key": k, "status": status_of(k), "description": "", "required": False}
                for k in sorted((personal | shared) - declared_ids)
            ]
            return {"groups": groups, "other": other, "missing_required": missing_required}

        @router.put("/auth/secrets/{key}")
        async def put_secret(key: str, request: Request):
            auth = _require_auth(request)
            key = _safe_secret_key(key)
            body = await _json(request)
            value = body.get("value")
            if not isinstance(value, str) or value == "":
                raise HTTPException(status_code=400, detail="non-empty 'value' required")
            await credentials.put(
                tenant_id=auth.tenant_id, key=key, value=value, user_id=auth.user_id
            )
            await identity.record(auth.tenant_id, auth.user_id, "secret.set", target=key)
            return {"status": "ok", "key": key, "scope": "user"}

        @router.delete("/auth/secrets/{key}")
        async def delete_secret(key: str, request: Request):
            auth = _require_auth(request)
            key = _safe_secret_key(key)
            await credentials.delete(
                tenant_id=auth.tenant_id, key=key, user_id=auth.user_id
            )
            await identity.record(auth.tenant_id, auth.user_id, "secret.deleted", target=key)
            return {"status": "deleted", "key": key}

    app.include_router(router)
