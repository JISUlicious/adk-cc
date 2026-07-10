"""IdentityService — wires store + provider + token issuer, builds from env.

This is what the server holds: it knows how to authenticate (provider), mint a
token for the result (issuer), and produce the `JwtAuthExtractor` that validates
those tokens in-process. Selecting a different `IdentityProvider` here (OIDC,
Keycloak) is the single swap point for a future login variant.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
import uuid

from .models import ApiKeyRecord, AuditEvent, InviteRecord
from .passwords import hash_password, verify_password
from .provider import EmailPasswordProvider, Identity
from .store import (
    JsonFileApiKeyStore,
    JsonFileAuditStore,
    JsonFileInviteStore,
    JsonFileUserStore,
    normalize_email,
)
from .tokens import TokenIssuer

MEMBER_ROLE = "member"
OWNER_ROLE = "owner"
_INVITE_TTL_S = 7 * 24 * 3600
_PAT_TTL_S = 365 * 24 * 3600  # personal access tokens: 1 year

_log = logging.getLogger(__name__)


def _fire_and_forget(fn, *args) -> None:
    """Run a blocking side-effect (a best-effort store write) OFF the event
    loop without awaiting it, so the request never waits on the I/O. Falls back
    to a direct synchronous call when there's no running loop (tests/cron)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001 — best-effort
            _log.warning("identity: background write failed (%s: %s)", type(e).__name__, e)
        return
    fut = loop.run_in_executor(None, fn, *args)

    def _log_err(f) -> None:
        exc = f.exception()
        if exc is not None:
            _log.warning("identity: background write failed (%s: %s)", type(exc).__name__, exc)

    fut.add_done_callback(_log_err)


class IdentityService:
    def __init__(self, *, provider, issuer: TokenIssuer, mode: str, invites=None,
                 api_keys=None, audit=None) -> None:
        self.provider = provider
        self.issuer = issuer
        self.mode = mode
        self.invites = invites
        self.api_keys = api_keys
        self.audit = audit

    @property
    def store(self):
        return self.provider.store

    @property
    def admin_role(self) -> str:
        return getattr(self.provider, "admin_role", "admin")

    def allowed_roles(self) -> tuple[str, ...]:
        return (self.admin_role, MEMBER_ROLE)

    @classmethod
    def build_from_env(cls) -> "IdentityService":
        base = os.environ.get("ADK_CC_IDENTITY_DIR") or os.path.join(".adk-cc", "identity")
        mode = (os.environ.get("ADK_CC_TENANCY_MODE") or "single").strip().lower()
        if mode not in ("single", "multi"):
            mode = "single"
        global_tenant = os.environ.get("ADK_CC_GLOBAL_TENANT_ID", "local")
        admin_role = os.environ.get("ADK_CC_ADMIN_ROLE", "admin")

        store = JsonFileUserStore(os.path.join(base, "users.json"))
        issuer = TokenIssuer(
            key_path=os.path.join(base, "jwt_key.json"),
            issuer=os.environ.get("ADK_CC_AUTH_ISSUER", "adk-cc"),
            audience=os.environ.get("ADK_CC_AUTH_AUDIENCE") or None,
            ttl_s=int(os.environ.get("ADK_CC_AUTH_TOKEN_TTL_S", "43200")),
            user_claim=os.environ.get("ADK_CC_JWT_USER_CLAIM", "sub"),
            tenant_claim=os.environ.get("ADK_CC_JWT_TENANT_CLAIM", "tenant"),
            roles_claim=os.environ.get("ADK_CC_JWT_ROLES_CLAIM", "roles"),
            scopes_claim=os.environ.get("ADK_CC_JWT_SCOPES_CLAIM", "scope"),
        )
        provider = EmailPasswordProvider(
            store, mode=mode, global_tenant_id=global_tenant, admin_role=admin_role,
            access_requests=os.environ.get("ADK_CC_ACCESS_REQUESTS", "1").lower() not in ("0", "false"),
        )
        invites = JsonFileInviteStore(os.path.join(base, "invites.json"))
        api_keys = JsonFileApiKeyStore(os.path.join(base, "api_keys.json"))
        audit = JsonFileAuditStore(os.path.join(base, "audit.json"))
        svc = cls(provider=provider, issuer=issuer, mode=mode, invites=invites,
                  api_keys=api_keys, audit=audit)
        svc._maybe_bootstrap_admin(global_tenant, admin_role)
        return svc

    def _maybe_bootstrap_admin(self, global_tenant: str, admin_role: str) -> None:
        """Seed a first admin from env so a fresh single-mode deployment has
        someone who can log in. No-op if the email already exists or env unset."""
        email = os.environ.get("ADK_CC_BOOTSTRAP_ADMIN_EMAIL")
        password = os.environ.get("ADK_CC_BOOTSTRAP_ADMIN_PASSWORD")
        if not email or not password:
            return
        if self.provider.store.get_by_email(email) is not None:
            return
        self.provider.provision(
            email=email, password=password, name="admin",
            tenant_id=global_tenant, roles=[admin_role],
        )

    def make_extractor(self):
        """A JwtAuthExtractor that validates OUR tokens in-process (jwks held
        in memory — no network). Identical validation path Keycloak would use."""
        from ..service.auth import JwtAuthExtractor

        i = self.issuer
        return JwtAuthExtractor(
            jwks=i.public_jwks(),
            issuer=i.issuer,
            audience=i.audience,
            user_claim=i.user_claim,
            tenant_claim=i.tenant_claim,
            roles_claim=i.roles_claim,
            scopes_claim=i.scopes_claim,
            extra_validate=self._validate_pat,
        )

    def _validate_pat(self, claims) -> None:
        """Post-validation hook: reject a personal access token whose handle has
        been revoked (or vanished). Non-PAT tokens pass through untouched."""
        if not claims.get("pat"):
            return
        from fastapi import HTTPException

        jti = claims.get("jti")
        rec = self.api_keys.get(jti) if (self.api_keys and jti) else None
        if rec is None or rec.revoked:
            raise HTTPException(status_code=401, detail="api key revoked")
        # The last_used stamp is a whole-file rewrite — do it off-loop and don't
        # await it, so a PAT-bearing request isn't gated on the write. (The
        # revocation read above stays inline: it's a small read and it gates auth.)
        rec.last_used = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _fire_and_forget(self.api_keys.update, rec)

    def token_for(self, ident: Identity) -> str:
        return self.issuer.issue(
            user_id=ident.user_id, tenant_id=ident.tenant_id,
            roles=ident.roles, scopes=ident.scopes,
            email=ident.email, name=ident.name,
        )

    @staticmethod
    def user_dict(ident: Identity) -> dict:
        return {
            "id": ident.user_id,
            "email": ident.email,
            "name": ident.name,
            "tenant": ident.tenant_id,
            "roles": list(ident.roles),
        }

    # ------------------------------------------------------------------
    # Org / team management. All tenant-scoped: callers (the routes) pass the
    # ADMIN caller's own tenant_id, so an admin only ever sees/changes members
    # of their own org. Admin-role gating happens in org_routes.
    # ------------------------------------------------------------------
    @staticmethod
    def _member_dict(u) -> dict:
        return {"id": u.user_id, "email": u.email, "name": u.name,
                "roles": list(u.roles), "status": u.status, "created": u.created}

    @staticmethod
    def _invite_dict(i: InviteRecord) -> dict:
        return {"token": i.token, "email": i.email, "role": i.role,
                "created": i.created, "expires": i.expires, "status": i.status}

    @staticmethod
    def _expired(inv: InviteRecord) -> bool:
        return bool(inv.expires) and time.time() > inv.expires

    def list_members(self, tenant_id: str) -> list[dict]:
        # Pending access requests aren't members yet — they live in list_access_requests().
        return [self._member_dict(u) for u in self.store.list_by_tenant(tenant_id)
                if u.status != "pending"]

    def provision_member(self, tenant_id: str, *, email: str, password: str,
                        name: str = "", role: str = MEMBER_ROLE) -> dict:
        """Admin path: create a user directly in the org with a password (no
        invite). Role is restricted to {admin, member} — ownership isn't grantable."""
        role = role or MEMBER_ROLE
        if role not in self.allowed_roles():
            raise ValueError(f"invalid role: {role}")
        ident = self.provider.provision(
            email=email, password=password, name=name, tenant_id=tenant_id, roles=[role])
        u = self.store.get(ident.user_id)
        return self._member_dict(u)

    def create_invite(self, tenant_id: str, email: str, role: str = MEMBER_ROLE,
                      ttl_s: int = _INVITE_TTL_S) -> InviteRecord:
        role = role or MEMBER_ROLE
        if role not in self.allowed_roles():
            raise ValueError(f"invalid role: {role}")
        email = normalize_email(email)
        if not email:
            raise ValueError("email is required")
        existing = self.store.get_by_email(email)
        if existing and existing.tenant_id == tenant_id:
            raise ValueError("that email is already a member")
        inv = InviteRecord(
            token=secrets.token_urlsafe(24), email=email, tenant_id=tenant_id, role=role,
            created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            expires=(time.time() + ttl_s) if ttl_s else 0.0, status="pending",
        )
        self.invites.create(inv)
        return inv

    def list_invites(self, tenant_id: str) -> list[dict]:
        return [self._invite_dict(i) for i in self.invites.list_by_tenant(tenant_id)
                if i.status == "pending" and not self._expired(i)]

    def revoke_invite(self, tenant_id: str, token: str) -> None:
        inv = self.invites.get(token)
        if inv is None or inv.tenant_id != tenant_id:
            raise KeyError("invite not found")
        inv.status = "revoked"
        self.invites.update(inv)

    def invite_public(self, token: str) -> dict | None:
        """Non-secret info for the accept page. None if the token is unusable."""
        inv = self.invites.get(token)
        if inv is None or inv.status != "pending" or self._expired(inv):
            return None
        return {"email": inv.email, "org": inv.tenant_id, "role": inv.role}

    def accept_invite(self, token: str, *, password: str, name: str = "") -> Identity:
        inv = self.invites.get(token)
        if inv is None or inv.status != "pending" or self._expired(inv):
            raise ValueError("this invite is invalid or has expired")
        if self.store.get_by_email(inv.email) is not None:
            raise ValueError("an account already exists for this email")
        ident = self.provider.provision(
            email=inv.email, password=password, name=name,
            tenant_id=inv.tenant_id, roles=[inv.role],
        )
        inv.status, inv.accepted_by = "accepted", ident.user_id
        self.invites.update(inv)
        return ident

    # ------------------------------------------------------------------
    # Access requests — user-initiated joins, the mirror of invites: the user
    # files a pending account; an org admin approves (→ active member) or
    # rejects (→ record deleted). Tenant-scoped like everything above.
    # ------------------------------------------------------------------
    def list_access_requests(self, tenant_id: str) -> list[dict]:
        return [self._member_dict(u) | {"note": u.note}
                for u in self.store.list_by_tenant(tenant_id) if u.status == "pending"]

    def approve_access_request(self, tenant_id: str, user_id: str, role: str = MEMBER_ROLE) -> dict:
        role = role or MEMBER_ROLE
        if role not in self.allowed_roles():
            raise ValueError(f"invalid role: {role}")
        u = self._member_in_tenant(tenant_id, user_id)
        if u.status != "pending":
            raise ValueError("not a pending access request")
        u.status, u.roles = "active", [role]
        self.store.update(u)
        return self._member_dict(u)

    def reject_access_request(self, tenant_id: str, user_id: str) -> dict:
        u = self._member_in_tenant(tenant_id, user_id)
        if u.status != "pending":
            raise ValueError("not a pending access request")
        self.store.delete(u.user_id)
        return self._member_dict(u)

    def set_member_role(self, tenant_id: str, user_id: str, role: str) -> dict:
        if role not in self.allowed_roles():
            raise ValueError(f"invalid role: {role}")
        u = self._member_in_tenant(tenant_id, user_id)
        if OWNER_ROLE in u.roles:
            raise ValueError("the team owner's role can't be changed")
        if self.admin_role in u.roles and role != self.admin_role:
            self._guard_last_admin(tenant_id, user_id)
        u.roles = [role]
        self.store.update(u)
        return self._member_dict(u)

    def set_member_status(self, tenant_id: str, user_id: str, status: str) -> dict:
        if status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        u = self._member_in_tenant(tenant_id, user_id)
        if status == "disabled" and OWNER_ROLE in u.roles:
            raise ValueError("the team owner can't be disabled")
        if status == "disabled" and self.admin_role in u.roles:
            self._guard_last_admin(tenant_id, user_id)
        u.status = status
        self.store.update(u)
        return self._member_dict(u)

    def _member_in_tenant(self, tenant_id: str, user_id: str):
        u = self.store.get(user_id)
        if u is None or u.tenant_id != tenant_id:
            raise KeyError("member not found in this org")
        return u

    def _guard_last_admin(self, tenant_id: str, user_id: str) -> None:
        """Refuse to demote/disable the org's only remaining active admin."""
        admins = [m for m in self.store.list_by_tenant(tenant_id)
                  if self.admin_role in m.roles and m.status == "active"]
        if len(admins) <= 1 and any(m.user_id == user_id for m in admins):
            raise ValueError("cannot remove the last admin of the org")

    # ------------------------------------------------------------------
    # Account self-service (the signed-in user acts on their OWN account).
    # ------------------------------------------------------------------
    def profile(self, user_id: str) -> dict:
        u = self.store.get(user_id)
        if u is None:
            raise KeyError("user not found")
        return {"id": u.user_id, "email": u.email, "name": u.name,
                "tenant": u.tenant_id, "roles": list(u.roles)}

    def update_profile(self, user_id: str, *, name: str) -> dict:
        u = self.store.get(user_id)
        if u is None:
            raise KeyError("user not found")
        u.name = (name or "").strip()
        self.store.update(u)
        return self.profile(user_id)

    def change_password(self, user_id: str, *, current: str, new: str) -> None:
        u = self.store.get(user_id)
        if u is None:
            raise KeyError("user not found")
        if not verify_password(current, u.password_hash):
            raise ValueError("current password is incorrect")
        if len(new or "") < 8:
            raise ValueError("new password must be at least 8 characters")
        u.password_hash = hash_password(new)
        self.store.update(u)

    def create_api_key(self, user_id: str, *, name: str) -> tuple[ApiKeyRecord, str]:
        """Mint a long-lived PAT (JWT). Returns (record, token); the token is
        shown ONCE and never stored — only its revocable handle is."""
        u = self.store.get(user_id)
        if u is None:
            raise KeyError("user not found")
        if u.status != "active":
            raise ValueError("account is not active")
        jti = secrets.token_urlsafe(12)
        token = self.issuer.issue(
            user_id=u.user_id, tenant_id=u.tenant_id, roles=tuple(u.roles),
            email=u.email, name=u.name, ttl_s=_PAT_TTL_S, jti=jti, extra={"pat": True},
        )
        rec = ApiKeyRecord(id=jti, user_id=user_id, name=(name or "").strip() or "api key",
                           created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.api_keys.create(rec)
        return rec, token

    def list_api_keys(self, user_id: str) -> list[dict]:
        return [{"id": k.id, "name": k.name, "created": k.created,
                 "last_used": k.last_used, "revoked": k.revoked}
                for k in self.api_keys.list_by_user(user_id) if not k.revoked]

    def revoke_api_key(self, user_id: str, key_id: str) -> None:
        rec = self.api_keys.get(key_id)
        if rec is None or rec.user_id != user_id:
            raise KeyError("api key not found")
        rec.revoked = True
        self.api_keys.update(rec)

    # ------------------------------------------------------------------
    # Audit + usage (Phase 6). record() is called from the routes after each
    # action; the audit log and usage summary are read by admins for their org.
    # ------------------------------------------------------------------
    async def record(self, tenant_id: str, actor_id: str, action: str,
                     *, target: str = "", detail: str = "", actor_email: str = "") -> None:
        # The audit write (actor-email lookup + filelock + whole-array rewrite)
        # runs OFF the event loop via to_thread — so it never blocks the loop /
        # health checks — but is AWAITED so the log is immediately consistent (an
        # admin reading the audit right after an action sees it).
        if self.audit is None:
            return
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await asyncio.to_thread(
            self._record_sync, tenant_id, actor_id, action, target, detail, actor_email, ts)

    def _record_sync(self, tenant_id, actor_id, action, target, detail, actor_email, ts) -> None:
        if not actor_email:
            u = self.store.get(actor_id)
            actor_email = u.email if u else ""
        self.audit.append(AuditEvent(
            id=uuid.uuid4().hex, ts=ts,
            tenant_id=tenant_id, actor_id=actor_id, actor_email=actor_email,
            action=action, target=target, detail=detail,
        ))

    def recent_audit(self, tenant_id: str, limit: int = 200) -> list[dict]:
        if self.audit is None:
            return []
        return [
            {"ts": e.ts, "actor": e.actor_email or e.actor_id, "action": e.action,
             "target": e.target, "detail": e.detail}
            for e in self.audit.list_by_tenant(tenant_id, limit)
        ]

    def usage_summary(self, tenant_id: str) -> list[dict]:
        """Per-actor activity derived from the audit log: event count + last
        active, joined with the current member list (so inactive members show 0)."""
        events = self.audit.list_by_tenant(tenant_id, limit=10000) if self.audit else []
        agg: dict[str, dict] = {}
        for e in events:
            a = agg.setdefault(e.actor_id, {"events": 0, "last_active": "", "email": e.actor_email})
            a["events"] += 1
            if e.ts > a["last_active"]:
                a["last_active"] = e.ts
            if e.actor_email:
                a["email"] = e.actor_email
        out = []
        for m in self.store.list_by_tenant(tenant_id):
            if m.status == "pending":  # not a member yet
                continue
            a = agg.get(m.user_id, {"events": 0, "last_active": "", "email": m.email})
            out.append({"id": m.user_id, "email": m.email or a["email"],
                        "roles": list(m.roles), "status": m.status,
                        "events": a["events"], "last_active": a["last_active"]})
        out.sort(key=lambda r: r["events"], reverse=True)
        return out
