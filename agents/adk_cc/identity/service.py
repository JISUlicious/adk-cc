"""IdentityService — wires store + provider + token issuer, builds from env.

This is what the server holds: it knows how to authenticate (provider), mint a
token for the result (issuer), and produce the `JwtAuthExtractor` that validates
those tokens in-process. Selecting a different `IdentityProvider` here (OIDC,
Keycloak) is the single swap point for a future login variant.
"""

from __future__ import annotations

import os
import secrets
import time

from .models import InviteRecord
from .provider import EmailPasswordProvider, Identity
from .store import JsonFileInviteStore, JsonFileUserStore, normalize_email
from .tokens import TokenIssuer

MEMBER_ROLE = "member"
OWNER_ROLE = "owner"
_INVITE_TTL_S = 7 * 24 * 3600


class IdentityService:
    def __init__(self, *, provider, issuer: TokenIssuer, mode: str, invites=None) -> None:
        self.provider = provider
        self.issuer = issuer
        self.mode = mode
        self.invites = invites

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
            store, mode=mode, global_tenant_id=global_tenant, admin_role=admin_role
        )
        invites = JsonFileInviteStore(os.path.join(base, "invites.json"))
        svc = cls(provider=provider, issuer=issuer, mode=mode, invites=invites)
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
        )

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
        return [self._member_dict(u) for u in self.store.list_by_tenant(tenant_id)]

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
