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
    """Per-tenant opaque secret storage.

    Implementations: see `adk_cc.credentials.impls` for in-memory and
    encrypted-file defaults; operators wrap external systems (Vault, AWS
    Secrets Manager, K8s projected secrets) by implementing this ABC.
    """

    @abstractmethod
    async def get(self, *, tenant_id: str, key: str) -> str | None:
        """Return the stored secret or None if absent."""

    @abstractmethod
    async def put(self, *, tenant_id: str, key: str, value: str) -> None:
        """Store / overwrite a secret. Atomic per `(tenant_id, key)`."""

    @abstractmethod
    async def delete(self, *, tenant_id: str, key: str) -> None:
        """Remove a secret. No-op if absent."""
