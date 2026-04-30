"""Auth extraction for the FastAPI server layer.

Bring-your-own-auth design: operators pass an `AuthExtractor` callable to
the server factory. The default `BearerTokenExtractor` validates an
opaque token against a static map (env-configured). For real deployments,
implement an extractor that validates JWTs / hits an IdP / queries a
session DB.

The extractor returns a (user_id, tenant_id) pair or raises HTTPException.
The middleware attaches the pair to `request.state` where the
TenancyPlugin's resolver picks them up.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable, Protocol

# fastapi is an optional dep — only imported when the server module is used.
try:
    from fastapi import HTTPException, Request
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response
    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False


class AuthExtractor(Protocol):
    """Inspect a request, return (user_id, tenant_id), or raise."""

    async def __call__(self, request: "Request") -> tuple[str, str]: ...


class BearerTokenExtractor:
    """Trivial token → (user_id, tenant_id) lookup.

    Tokens load from `ADK_CC_AUTH_TOKENS` as `token1=user1:tenant1,token2=user2:tenant2`.
    Suitable for local testing; replace with a real JWT validator for prod.
    """

    def __init__(self, tokens: dict[str, tuple[str, str]] | None = None) -> None:
        if tokens is None:
            tokens = self._parse_env(os.environ.get("ADK_CC_AUTH_TOKENS", ""))
        self._tokens = tokens

    @staticmethod
    def _parse_env(raw: str) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            token, who = entry.split("=", 1)
            user, _, tenant = who.partition(":")
            out[token.strip()] = (user.strip() or "user", tenant.strip() or "default")
        return out

    async def __call__(self, request: "Request") -> tuple[str, str]:
        if not _FASTAPI_AVAILABLE:
            raise RuntimeError("fastapi is not installed")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = header[len("Bearer ") :].strip()
        creds = self._tokens.get(token)
        if creds is None:
            raise HTTPException(status_code=401, detail="invalid token")
        return creds


def make_auth_middleware(extractor: AuthExtractor):
    """Build a Starlette middleware that calls `extractor` and stashes the
    result on `request.state.adk_cc_auth = (user_id, tenant_id)`.

    The TenancyPlugin's resolver reads from this via the request that
    flows into ADK's session-creation path.
    """
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError("fastapi is not installed")

    class _AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: "Request", call_next: Callable[..., Awaitable["Response"]]
        ):
            try:
                user_id, tenant_id = await extractor(request)
            except HTTPException as e:
                return Response(
                    content=e.detail, status_code=e.status_code, media_type="text/plain"
                )
            request.state.adk_cc_auth = (user_id, tenant_id)
            return await call_next(request)

    return _AuthMiddleware
