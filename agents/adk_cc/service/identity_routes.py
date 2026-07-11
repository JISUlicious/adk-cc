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
import io
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from ..identity.provider import AccountPendingError
from ..identity.ratelimit import FailureLockout, SlidingWindowLimiter

_MAX_SKILL_ZIP = 10 * 1024 * 1024  # 10 MiB per personal skill upload
_MAX_USER_RESOURCES = 50  # cap personal skills / MCP servers per user


def _safe_id(value: str, label: str = "id") -> str:
    safe = "".join(c for c in (value or "") if c.isalnum() or c in "-_")
    if safe != value or not safe:
        raise HTTPException(status_code=400, detail=f"invalid {label}")
    return safe

# Public paths the auth middleware must let through unauthenticated (you can't
# present a token before you have one). Kept here so server.py imports one list.
PUBLIC_PATHS: tuple[str, ...] = (
    "/auth/login",
    "/auth/signup",
    "/auth/request-access",
    # Public: possession of the refresh token IS the credential, and both must
    # work with an expired access token (that's their whole point).
    "/auth/refresh",
    "/auth/logout",
    "/auth/config",
    "/.well-known/jwks.json",
)

# Public (auth-exempt) reset endpoints live under this prefix — the one-time
# token in the path is the credential, and the holder has no account access yet.
PUBLIC_PREFIXES: tuple[str, ...] = ("/auth/reset/",)


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

    # --- brute-force protection on the public auth endpoints ---------------
    # Per-IP burst budget + per-(ip|email) lockout after repeated failures.
    # In-memory/per-process — right for the self-hosted deployment.
    # ADK_CC_AUTH_RATELIMIT=0 disables (dev/tests).
    rl_enabled = os.environ.get("ADK_CC_AUTH_RATELIMIT", "1").lower() not in ("0", "false")
    ip_limiter = SlidingWindowLimiter(
        limit=int(os.environ.get("ADK_CC_AUTH_RATELIMIT_MAX", "30")),
        window_s=float(os.environ.get("ADK_CC_AUTH_RATELIMIT_WINDOW_S", "60")))
    lockout = FailureLockout(
        threshold=int(os.environ.get("ADK_CC_AUTH_LOCKOUT_THRESHOLD", "5")),
        lockout_s=float(os.environ.get("ADK_CC_AUTH_LOCKOUT_S", "300")))

    # Only trust X-Forwarded-For when explicitly deployed behind a proxy that
    # sets it — otherwise a client could spoof the header to dodge the lockout.
    # Without this, every request behind a reverse proxy shares the proxy's IP,
    # collapsing the per-(ip,email) lockout to email-only (a victim-lockout DoS).
    _trust_proxy = os.environ.get("ADK_CC_TRUST_PROXY", "").lower() in ("1", "true")

    def _client_ip(request: Request) -> str:
        if _trust_proxy:
            xff = request.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()  # left-most = original client
        return request.client.host if request.client else "?"

    def _guard_rate(request: Request) -> None:
        if rl_enabled and not ip_limiter.allow(_client_ip(request)):
            raise HTTPException(
                status_code=429, detail="too many requests — slow down",
                headers={"Retry-After": str(int(ip_limiter.window_s) or 1)})

    def _guard_lockout(request: Request, email: str) -> str:
        key = f"{_client_ip(request)}|{(email or '').strip().lower()}"
        if rl_enabled:
            wait = lockout.locked_for(key)
            if wait > 0:
                raise HTTPException(
                    status_code=429,
                    detail="too many failed attempts — try again later",
                    headers={"Retry-After": str(int(wait) + 1)})
        return key

    async def _token_response(ident) -> dict:
        # access token + (when a refresh store is configured) a refresh token.
        out = {
            "access_token": identity.token_for(ident),
            "token_type": "Bearer",
            "user": identity.user_dict(ident),
        }
        rt = await asyncio.to_thread(identity.issue_refresh_token, ident.user_id)
        if rt:
            out["refresh_token"] = rt
        return out

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
        _guard_rate(request)
        body = await _json(request)
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not email or not password:
            raise HTTPException(status_code=400, detail="email and password required")
        key = _guard_lockout(request, email)
        try:
            ident = await identity.provider.login_password(email, password)
        except AccountPendingError:
            # Password verified but the access request hasn't been approved yet.
            lockout.clear(key)
            raise HTTPException(status_code=403,
                                detail="Your access request is awaiting admin approval.")
        if ident is None:
            lockout.record_failure(key)
            raise HTTPException(status_code=401, detail="invalid email or password")
        lockout.clear(key)
        await identity.record(ident.tenant_id, ident.user_id, "login", actor_email=ident.email)
        return await _token_response(ident)

    @router.post("/auth/signup")
    async def signup(request: Request):
        _guard_rate(request)
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
        return await _token_response(ident)

    @router.post("/auth/request-access")
    async def request_access(request: Request):
        # User-initiated join (the mirror of an admin invite): creates a PENDING
        # account an org admin approves/rejects. No token is returned — the
        # account can't log in until approved.
        _guard_rate(request)
        if not identity.provider.supports_access_requests:
            raise HTTPException(status_code=403, detail="access requests are disabled")
        body = await _json(request)
        try:
            ident = await identity.provider.request_access(
                email=(body.get("email") or "").strip(),
                password=body.get("password") or "",
                name=(body.get("name") or "").strip(),
                note=(body.get("note") or "").strip()[:500],
            )
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        # ident is None when the email was already taken — respond identically
        # ("pending") either way so the endpoint can't be used to enumerate accounts.
        if ident is not None:
            await identity.record(ident.tenant_id, ident.user_id, "access.requested",
                                  actor_email=ident.email)
        return {"status": "pending"}

    # --- public: complete a password reset (one-time link from an admin) ---
    @router.get("/auth/reset/{token}")
    async def get_reset(token: str, request: Request):
        _guard_rate(request)  # unmetered lookups let a bot probe tokens + read emails
        info = await asyncio.to_thread(identity.reset_public, token)
        if info is None:
            raise HTTPException(status_code=404, detail="reset link invalid or expired")
        return info

    @router.post("/auth/reset/{token}/complete")
    async def complete_reset(token: str, request: Request):
        _guard_rate(request)
        body = await _json(request)
        try:
            ident = await asyncio.to_thread(
                identity.complete_password_reset, token, body.get("password") or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(ident.tenant_id, ident.user_id, "password.reset",
                              actor_email=ident.email)
        # Completing the reset proves possession — sign them straight in.
        return await _token_response(ident)

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
            ident = await asyncio.to_thread(identity.change_password,
                auth.user_id,
                current=body.get("current_password") or "",
                new=body.get("new_password") or "",
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "password.changed")
        # change_password revoked ALL of the user's refresh tokens (including
        # this session's); hand back a fresh pair so the caller stays signed in
        # while every OTHER session is logged out.
        return await _token_response(ident)

    @router.post("/auth/email")
    async def change_email(request: Request):
        # Immediate swap gated on the current password (no mailer → nothing to
        # verify against). The audit log keeps old → new.
        auth = _require_auth(request)
        body = await _json(request)
        try:
            # Old email read inside the try so a deleted-user race → 404, not 500.
            old = (await asyncio.to_thread(identity.profile, auth.user_id)).get("email", "")
            prof = await asyncio.to_thread(identity.change_email,
                auth.user_id,
                new_email=body.get("new_email") or "",
                password=body.get("password") or "",
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "email.changed",
                              target=prof.get("email", ""), detail=f"was {old}")
        return prof

    async def _purge_user_resources(auth) -> None:
        """Best-effort removal of everything personal to a deleted account:
        secrets, MCP servers, skills, and the workspace directory. Each leg is
        independent — a failure in one never blocks the others."""
        log = logging.getLogger(__name__)
        if credentials is not None:
            try:
                for key in await credentials.list_keys(
                        tenant_id=auth.tenant_id, user_id=auth.user_id):
                    await credentials.delete(
                        tenant_id=auth.tenant_id, key=key, user_id=auth.user_id)
            except Exception as e:  # noqa: BLE001
                log.warning("account purge: secrets failed (%s)", e)
        reg_dir = os.environ.get("ADK_CC_TENANT_REGISTRY_DIR")
        if reg_dir:
            try:
                from ..service.registry import JsonFileTenantResourceRegistry
                from ..tools.mcp_tenant import McpServerConfig

                reg = JsonFileTenantResourceRegistry(
                    root=reg_dir, kind="mcp", model=McpServerConfig, id_attr="server_name")
                for cfg in await reg.list_for_tenant(auth.tenant_id, auth.user_id):
                    await reg.remove(tenant_id=auth.tenant_id,
                                     resource_id=cfg.server_name, user_id=auth.user_id)
            except Exception as e:  # noqa: BLE001
                log.warning("account purge: mcp servers failed (%s)", e)
        skill_root = os.environ.get("ADK_CC_TENANT_SKILLS_DIR")
        if skill_root:
            try:
                d = (Path(skill_root) / _safe_id(auth.tenant_id, "tenant_id")
                     / "_users" / _safe_id(auth.user_id, "user_id"))
                if d.is_dir():
                    await asyncio.to_thread(shutil.rmtree, d, True)
            except Exception as e:  # noqa: BLE001
                log.warning("account purge: skills failed (%s)", e)
        ws_root = os.environ.get("ADK_CC_WORKSPACE_ROOT")
        if ws_root:
            try:
                root = Path(ws_root).resolve()
                d = (root / _safe_id(auth.tenant_id, "tenant_id")
                     / _safe_id(auth.user_id, "user_id")).resolve()
                # containment: only ever delete <root>/<tenant>/<user>
                if d.is_dir() and root in d.parents and d != root:
                    await asyncio.to_thread(shutil.rmtree, d, True)
            except Exception as e:  # noqa: BLE001
                log.warning("account purge: workspace failed (%s)", e)

    @router.post("/auth/account/deactivate")
    async def deactivate_account(request: Request):
        # Self-service, reversible: blocks login + ends sessions; an admin
        # re-enables from the members list.
        auth = _require_auth(request)
        body = await _json(request)
        try:
            await asyncio.to_thread(identity.deactivate_account,
                auth.user_id, password=body.get("password") or "")
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await identity.record(auth.tenant_id, auth.user_id, "account.deactivated")
        return {"status": "disabled"}

    @router.delete("/auth/account")
    async def delete_account(request: Request):
        # Self-service, permanent: identity record + credentials go first,
        # then a best-effort purge of personal resources. Sessions rows in the
        # session DB are retained but inert (nothing can read them once the
        # tenant/user scope is gone).
        auth = _require_auth(request)
        body = await _json(request)
        try:
            m = await asyncio.to_thread(identity.delete_account,
                auth.user_id, password=body.get("password") or "")
        except KeyError:
            raise HTTPException(status_code=404, detail="user not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # actor_email passed explicitly — the record is already gone.
        await identity.record(auth.tenant_id, auth.user_id, "account.deleted",
                              target=m["email"], actor_email=m["email"])
        await _purge_user_resources(auth)
        return {"status": "deleted"}

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

    @router.post("/auth/refresh")
    async def refresh(request: Request):
        # Rotate-on-use: the presented refresh token is revoked and a fresh
        # (access, refresh) pair comes back. One opaque 401 for every failure
        # mode — no oracle for probing token state.
        _guard_rate(request)
        body = await _json(request)
        try:
            ident, new_refresh = await asyncio.to_thread(
                identity.rotate_refresh_token, body.get("refresh_token") or "")
        except ValueError:
            raise HTTPException(status_code=401, detail="invalid or expired refresh token")
        return {
            "access_token": identity.token_for(ident),
            "refresh_token": new_refresh,
            "token_type": "Bearer",
            "user": identity.user_dict(ident),
        }

    @router.post("/auth/logout")
    async def logout(request: Request):
        # Real logout: revoke the presented refresh token (the access token
        # simply ages out — it's short-lived). Public + best-effort so an
        # expired access token can't block sign-out.
        if await request.body():
            body = await _json(request)
            rt = body.get("refresh_token") or ""
            if rt:
                await asyncio.to_thread(identity.revoke_refresh_token, rt)
        return Response(status_code=204)

    # ---- per-user secrets (Settings → Secrets) ---------------------------
    # Self-service personal credentials for skills/MCP, resolved user-over-
    # tenant at use time. Mounted only when a CredentialProvider is configured.
    # Returns names + scope; a value is returned ONLY for inputs a manifest
    # declares non-secret. Writes go to the caller's PERSONAL scope; tenant-
    # shared secrets are admin-managed elsewhere.
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

            async def item(ri) -> dict:
                st = status_of(ri.id)
                out = {
                    "key": ri.id,
                    "status": st,
                    "description": ri.description,
                    "required": True,
                    "secret": ri.secret,
                }
                # A manifest can declare an input as non-secret (e.g. a region or
                # endpoint). Those values are safe to surface so the UI shows /
                # edits them as plain text. Secret values are NEVER returned.
                if not ri.secret and st != "unset":
                    val = await credentials.get(
                        tenant_id=auth.tenant_id, key=ri.id, user_id=auth.user_id
                    )
                    if val is not None:
                        out["value"] = val
                return out

            # Declared inputs grouped by owning skill / MCP server, so the UI
            # can render one section each and badge the ones not yet set. Secret
            # values are never returned; non-secret values are (see item()).
            try:
                from ..credentials.required_inputs import discover_groups

                raw_groups = await discover_groups(auth.tenant_id, auth.user_id)
            except Exception:  # noqa: BLE001
                raw_groups = []
            declared_ids: set[str] = set()
            groups = []
            missing_required = 0
            for g in raw_groups:
                inputs = [await item(ri) for ri in g.inputs]
                miss = sum(1 for it in inputs if it["status"] == "unset")
                missing_required += miss
                declared_ids.update(it["key"] for it in inputs)
                groups.append({"kind": g.kind, "name": g.name, "inputs": inputs, "missing": miss})

            # keys the user set that no skill/MCP declares (custom) — undeclared,
            # so treat as secret (write-only) by default.
            other = [
                {"key": k, "status": status_of(k), "description": "", "required": False, "secret": True}
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

    # ---- per-user MCP servers (Settings → Your MCP servers) --------------
    # Self-service personal MCP servers, unioned with the org's at session time
    # (user shadows tenant by server_name). Same registry/root the agent reads.
    reg_dir = os.environ.get("ADK_CC_TENANT_REGISTRY_DIR")
    if reg_dir:
        from ..service.registry import JsonFileTenantResourceRegistry
        from ..tools.mcp_tenant import McpServerConfig

        _mcp_reg = JsonFileTenantResourceRegistry(
            root=reg_dir, kind="mcp", model=McpServerConfig, id_attr="server_name"
        )

        @router.get("/auth/mcp-servers")
        async def list_user_mcp(request: Request):
            auth = _require_auth(request)
            personal = await _mcp_reg.list_for_tenant(auth.tenant_id, auth.user_id)
            tenant = await _mcp_reg.list_for_tenant(auth.tenant_id)
            pn = {c.server_name for c in personal}
            out = [{**c.model_dump(mode="json"), "scope": "user"} for c in personal]
            out += [
                {**c.model_dump(mode="json"), "scope": "tenant"}
                for c in tenant
                if c.server_name not in pn  # personal shadows org
            ]
            return {"servers": out}

        @router.put("/auth/mcp-servers/{server_name}")
        async def put_user_mcp(server_name: str, request: Request):
            auth = _require_auth(request)
            server_name = _safe_id(server_name, "server_name")
            body = await _json(request)
            body["server_name"] = server_name  # path wins
            try:
                cfg = McpServerConfig.model_validate(body)
            except Exception as e:  # noqa: BLE001 — Pydantic validation
                raise HTTPException(status_code=400, detail=f"invalid config: {e}")
            existing = await _mcp_reg.list_for_tenant(auth.tenant_id, auth.user_id)
            if server_name not in {c.server_name for c in existing} and len(existing) >= _MAX_USER_RESOURCES:
                raise HTTPException(status_code=409, detail="personal MCP server limit reached")
            await _mcp_reg.add(tenant_id=auth.tenant_id, resource=cfg, user_id=auth.user_id)
            await identity.record(auth.tenant_id, auth.user_id, "mcp.set", target=server_name)
            return {"status": "ok", "scope": "user"}

        @router.delete("/auth/mcp-servers/{server_name}")
        async def delete_user_mcp(server_name: str, request: Request):
            auth = _require_auth(request)
            server_name = _safe_id(server_name, "server_name")
            await _mcp_reg.remove(
                tenant_id=auth.tenant_id, resource_id=server_name, user_id=auth.user_id
            )
            await identity.record(auth.tenant_id, auth.user_id, "mcp.deleted", target=server_name)
            return {"status": "deleted", "server_name": server_name}

    # ---- per-user skills (Settings → Your skills) ------------------------
    # Upload a skill .zip into <root>/<tenant>/_users/<user>/<name>/; the agent's
    # TenantSkillToolset unions it with the org's (user shadows by name).
    skill_root = os.environ.get("ADK_CC_TENANT_SKILLS_DIR")
    if skill_root:
        _sroot = Path(skill_root)

        def _user_skill_dir(auth) -> Path:
            return _sroot / _safe_id(auth.tenant_id, "tenant_id") / "_users" / _safe_id(auth.user_id, "user_id")

        @router.get("/auth/skills")
        async def list_user_skills(request: Request):
            auth = _require_auth(request)
            base = _user_skill_dir(auth)
            if not base.is_dir():
                return {"skills": []}
            return {"skills": sorted(p.name for p in base.iterdir() if p.is_dir())}

        @router.put("/auth/skills/{skill_name}")
        async def put_user_skill(skill_name: str, request: Request):
            auth = _require_auth(request)
            s = _safe_id(skill_name, "skill_name")
            base = _user_skill_dir(auth)
            target = base / s
            body = await request.body()
            if not body:
                raise HTTPException(status_code=400, detail="empty body")
            if len(body) > _MAX_SKILL_ZIP:
                raise HTTPException(status_code=413, detail="skill zip too large")
            if not target.exists() and base.is_dir() and sum(1 for p in base.iterdir() if p.is_dir()) >= _MAX_USER_RESOURCES:
                raise HTTPException(status_code=409, detail="personal skill limit reached")
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as zf:
                    for member in zf.namelist():
                        if member.startswith("/") or ".." in Path(member).parts:
                            raise HTTPException(status_code=400, detail=f"unsafe path in zip: {member!r}")
                    with tempfile.TemporaryDirectory(dir=str(base) if base.is_dir() else None) as tmp:
                        zf.extractall(tmp)
                        if not any(
                            f.suffix.lower() in (".md", ".yaml", ".yml")
                            for f in Path(tmp).rglob("*") if f.is_file()
                        ):
                            raise HTTPException(status_code=400, detail="no skill manifest found in zip")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.move(tmp, target)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="body is not a valid zip")
            from ..credentials.required_inputs import invalidate_cache

            invalidate_cache()  # new declarations surface immediately
            await identity.record(auth.tenant_id, auth.user_id, "skill.uploaded", target=s)
            return {"status": "ok"}

        @router.delete("/auth/skills/{skill_name}")
        async def delete_user_skill(skill_name: str, request: Request):
            auth = _require_auth(request)
            s = _safe_id(skill_name, "skill_name")
            target = _user_skill_dir(auth) / s
            if target.exists():
                shutil.rmtree(target)
            from ..credentials.required_inputs import invalidate_cache

            invalidate_cache()
            await identity.record(auth.tenant_id, auth.user_id, "skill.deleted", target=s)
            return {"status": "deleted", "skill_name": s}

    app.include_router(router)
