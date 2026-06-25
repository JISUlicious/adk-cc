"""Identity providers — the pluggable login VARIANT.

`IdentityProvider` is the abstraction the user asked us to keep open: the
email+password variant is implemented here; future OIDC / SAML / Keycloak
variants implement the SAME surface. The redirect-based SSO methods are present
as `NotImplementedError` seams so a new provider only overrides what it needs,
and the server / routes can stay variant-agnostic (they call `describe()` to
learn which methods are live).
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from abc import ABC
from dataclasses import dataclass
from typing import ClassVar

from .models import UserRecord
from .passwords import hash_password, verify_password
from .store import UserStore, normalize_email

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 8


@dataclass
class Identity:
    """The authenticated result a provider returns — maps 1:1 onto the JWT
    claims the issuer mints and `AuthPrincipal` downstream consumes."""

    user_id: str
    tenant_id: str
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    email: str = ""
    name: str = ""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


class IdentityProvider(ABC):
    id: ClassVar[str] = "base"
    supports_password_login: ClassVar[bool] = False
    supports_registration: ClassVar[bool] = False
    supports_sso: ClassVar[bool] = False

    async def login_password(self, email: str, password: str) -> "Identity | None":
        raise NotImplementedError(f"{self.id}: no password login")

    async def register(self, *, email: str, password: str, name: str = "", org: str = "") -> "Identity":
        raise NotImplementedError(f"{self.id}: no self-registration")

    # --- SSO redirect seams (future OIDC / SAML / Keycloak providers) -------
    def sso_authorization_url(self, *, redirect_uri: str, state: str) -> str:
        raise NotImplementedError(f"{self.id}: no SSO")

    async def sso_exchange(self, *, params: dict) -> "Identity":
        raise NotImplementedError(f"{self.id}: no SSO")

    def describe(self) -> dict:
        return {
            "id": self.id,
            "password": self.supports_password_login,
            "registration": self.supports_registration,
            "sso": self.supports_sso,
        }


class EmailPasswordProvider(IdentityProvider):
    """Local accounts with scrypt-hashed passwords.

    Tenancy:
      - ``single`` mode: self-registration is OFF; an admin provisions users
        (``provision()``), all on the global tenant.
      - ``multi`` mode: ``register()`` is ON; each signup creates its own
        tenant (org) and the registrant is its admin/owner.
    """

    id = "password"
    supports_password_login = True

    def __init__(
        self,
        store: UserStore,
        *,
        mode: str = "single",
        global_tenant_id: str = "local",
        admin_role: str = "admin",
        owner_role: str = "owner",
    ) -> None:
        self.store = store
        self.mode = mode
        self.global_tenant_id = global_tenant_id
        self.admin_role = admin_role
        self.owner_role = owner_role

    @property
    def supports_registration(self) -> bool:  # type: ignore[override]
        return self.mode == "multi"

    async def login_password(self, email: str, password: str) -> "Identity | None":
        rec = await asyncio.to_thread(self.store.get_by_email, email)
        if rec is None or rec.status != "active":
            return None
        # scrypt is CPU-bound (~16 MiB, tens of ms) — offload it so a login
        # (unauthenticated, hot) never blocks the event loop / health checks.
        if not await asyncio.to_thread(verify_password, password, rec.password_hash):
            return None
        return Identity(rec.user_id, rec.tenant_id, tuple(rec.roles), (), rec.email, rec.name)

    async def register(self, *, email: str, password: str, name: str = "", org: str = "") -> "Identity":
        if self.mode != "multi":
            raise PermissionError("self-registration is disabled (single-org mode)")
        return await asyncio.to_thread(self._create, email, password, name, org, None, None)

    def provision(
        self,
        *,
        email: str,
        password: str,
        name: str = "",
        tenant_id: str | None = None,
        roles: list[str] | None = None,
    ) -> "Identity":
        """Admin / bootstrap path: create a user directly (synchronous — runs
        at startup or from an admin route, not on the login hot path)."""
        return self._create(email, password, name, "", tenant_id, roles)

    def _create(self, email, password, name, org, tenant_id, roles) -> "Identity":
        email = normalize_email(email)
        if not _EMAIL_RE.match(email):
            raise ValueError("invalid email address")
        if len(password or "") < _MIN_PASSWORD:
            raise ValueError(f"password must be at least {_MIN_PASSWORD} characters")
        uid = uuid.uuid4().hex
        if tenant_id is None:
            if self.mode == "multi":
                tenant_id = _slug(org) or uid  # each signup owns a fresh tenant
                # The org creator OWNS it (owner ⊇ admin powers).
                roles = roles if roles is not None else [self.owner_role, self.admin_role]
            else:
                tenant_id = self.global_tenant_id
                roles = roles if roles is not None else []
        rec = UserRecord(
            user_id=uid,
            email=email,
            password_hash=hash_password(password),
            name=name or "",
            tenant_id=tenant_id,
            roles=list(roles or []),
            status="active",
            created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.store.create(rec)
        return Identity(uid, tenant_id, tuple(rec.roles), (), email, rec.name)
