"""IdentityService — wires store + provider + token issuer, builds from env.

This is what the server holds: it knows how to authenticate (provider), mint a
token for the result (issuer), and produce the `JwtAuthExtractor` that validates
those tokens in-process. Selecting a different `IdentityProvider` here (OIDC,
Keycloak) is the single swap point for a future login variant.
"""

from __future__ import annotations

import os

from .provider import EmailPasswordProvider, Identity
from .store import JsonFileUserStore
from .tokens import TokenIssuer


class IdentityService:
    def __init__(self, *, provider, issuer: TokenIssuer, mode: str) -> None:
        self.provider = provider
        self.issuer = issuer
        self.mode = mode

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
        svc = cls(provider=provider, issuer=issuer, mode=mode)
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
