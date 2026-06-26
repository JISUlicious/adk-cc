"""Tenant-scoped opaque secret storage.

Holds per-tenant secrets (MCP server tokens, model API keys, anything
that needs to be substituted into a tool's config at session-creation
time). Tenants register a credential under a key; resolvers read it
back when building the per-session tool surface.

Read at session boot, not in flight: the `BaseToolset.get_tools()`
resolver fetches the credential at the start of each invocation and
substitutes it into the underlying tool's connection params. Rotating
a credential takes effect on the next session, not in-flight ones.

Why not ADK's `BaseCredentialService`? Different shape:
- ADK's service is `@experimental` (tied to AuthConfig/AuthCredential
  and ADK's OAuth-exchange machinery).
- It's bucketed by `(app_name, user_id)` for ADK's per-tool auth flow.
- It returns `AuthCredential` objects with scheme metadata.

For static API tokens we just need `tenant_id × key → str`, with the
credential consumed at toolset construction (not via ADK's auth
runtime). Operators whose MCP servers use OAuth flows should plug
ADK's `BaseCredentialService` into those tools directly; the two
abstractions live alongside each other.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class CredentialProvider(ABC):
    """Per-tenant (and optionally per-user) opaque secret storage.

    Two scopes share one store, selected by the optional `user_id`:
      - `user_id=None`  → the TENANT-SHARED scope (org secrets; admin-managed).
      - `user_id=<id>`  → that user's PERSONAL scope (self-service).

    Resolution is LAYERED on read: `get(user_id=X)` returns the user's personal
    value if present, otherwise FALLS BACK to the tenant-shared value. Writes
    and listing are scope-EXACT (no fallback) — a personal `put` never touches
    the shared scope, and `list_keys(user_id=X)` lists only X's personal keys.
    `user_id=None` everywhere preserves the original tenant-only behavior.

    Implementations: see `adk_cc.credentials.impls` for in-memory and
    encrypted-file defaults; operators wrap external systems (Vault, AWS
    Secrets Manager, K8s projected secrets) by implementing this ABC.
    """

    @abstractmethod
    async def get(
        self, *, tenant_id: str, key: str, user_id: str | None = None
    ) -> str | None:
        """Return the stored secret or None if absent.

        With `user_id`, returns the user's personal value, falling back to the
        tenant-shared value (`user_id=None`) when the user has none.
        """

    @abstractmethod
    async def put(
        self, *, tenant_id: str, key: str, value: str, user_id: str | None = None
    ) -> None:
        """Store / overwrite a secret at the EXACT scope given. Atomic per
        `(tenant_id, user_id, key)`."""

    @abstractmethod
    async def delete(
        self, *, tenant_id: str, key: str, user_id: str | None = None
    ) -> None:
        """Remove a secret at the EXACT scope given. No-op if absent."""

    async def list_keys(
        self, *, tenant_id: str, user_id: str | None = None
    ) -> list[str]:
        """Return the credential KEY NAMES at the EXACT scope (never values).

        `user_id=None` → tenant-shared keys; `user_id=<id>` → that user's
        personal keys. Powers the admin panel (shared) and the per-user Settings
        page (personal). NOT abstract — defaults to an empty list so existing
        external implementations (Vault, etc.) keep working without change;
        override to surface key names. The stock providers override this.
        """
        return []
