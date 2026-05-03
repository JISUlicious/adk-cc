"""Auth extraction for the FastAPI server layer.

Bring-your-own-auth design: operators pass an `AuthExtractor` callable to
the server factory. Two stock impls ship:

  - `BearerTokenExtractor` — dev only. Validates an opaque token against
    a static map from `ADK_CC_AUTH_TOKENS`.
  - `JwtAuthExtractor` — production-grade. Validates JWT signature
    against a JWKS endpoint (cached with TTL); validates `exp`, `nbf`,
    `iss`, `aud`; maps configurable claims to `(user_id, tenant_id)`.

Operators with bespoke auth (session DB, mTLS, IdP integration) implement
the `AuthExtractor` protocol directly and pass it to `build_fastapi_app`.

The extractor returns a (user_id, tenant_id) pair or raises HTTPException.
The middleware attaches the pair to `request.state` where the
TenancyPlugin's resolver picks them up.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable, Optional, Protocol

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


class JwtAuthExtractor:
    """Validates a Bearer JWT against a JWKS endpoint.

    Construction: pass `jwks_url` (the IdP's `/.well-known/jwks.json` or
    equivalent) and the expected `issuer` / `audience`. Optionally pass
    `jwks` dict directly to bypass the network fetch (useful for tests).

    Validation steps on each request:
      1. Bearer token extracted from `Authorization` header.
      2. Signature verified against a key from the JWKS (selected by
         the JWT's `kid` header). JWKS is cached for `jwks_cache_ttl_seconds`.
      3. `exp` and `nbf` checked.
      4. `iss` checked against configured issuer (if set).
      5. `aud` checked against configured audience (if set).
      6. `user_claim` (default `sub`) and `tenant_claim` (default
         `tenant`) extracted from claims.

    Any failure raises HTTPException(401) with a non-leaky detail.
    """

    def __init__(
        self,
        *,
        jwks_url: Optional[str] = None,
        jwks: Optional[dict] = None,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
        user_claim: str = "sub",
        tenant_claim: str = "tenant",
        jwks_cache_ttl_seconds: int = 300,
    ) -> None:
        if jwks_url is None and jwks is None:
            raise ValueError("JwtAuthExtractor needs either jwks_url or jwks")
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience
        self._user_claim = user_claim
        self._tenant_claim = tenant_claim
        self._ttl = jwks_cache_ttl_seconds

        # If a static `jwks` dict was passed, prime the cache and skip
        # all fetching. Otherwise fetch lazily on first call.
        from authlib.jose import JsonWebKey

        self._jwks_cache: Any = (
            JsonWebKey.import_key_set(jwks) if jwks is not None else None
        )
        self._jwks_fetched_at = time.monotonic() if jwks is not None else 0.0
        self._lock = asyncio.Lock()

    async def _get_jwks(self) -> Any:
        # Static jwks dict: cache never expires.
        if self._jwks_url is None:
            return self._jwks_cache

        now = time.monotonic()
        if self._jwks_cache is not None and now - self._jwks_fetched_at < self._ttl:
            return self._jwks_cache

        async with self._lock:
            now = time.monotonic()
            if self._jwks_cache is not None and now - self._jwks_fetched_at < self._ttl:
                return self._jwks_cache
            jwks_data = await asyncio.to_thread(self._fetch_jwks_sync)
            from authlib.jose import JsonWebKey

            self._jwks_cache = JsonWebKey.import_key_set(jwks_data)
            self._jwks_fetched_at = time.monotonic()
        return self._jwks_cache

    def _fetch_jwks_sync(self) -> dict:
        from urllib.request import Request, urlopen

        req = Request(self._jwks_url, headers={"User-Agent": "adk-cc/0.1"})
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def __call__(self, request: "Request") -> tuple[str, str]:
        if not _FASTAPI_AVAILABLE:
            raise RuntimeError("fastapi is not installed")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = header[len("Bearer ") :].strip()

        try:
            jwks = await self._get_jwks()
        except Exception as e:
            # JWKS fetch failure is operator-side (network, IdP down) —
            # surface as 503 so it's distinguishable from a token problem.
            raise HTTPException(
                status_code=503, detail=f"jwks fetch failed: {type(e).__name__}"
            )

        from authlib.jose import jwt as _jwt
        from authlib.jose.errors import JoseError

        try:
            claims = _jwt.decode(token, jwks)
        except JoseError as e:
            raise HTTPException(
                status_code=401, detail=f"token decode failed: {type(e).__name__}"
            )
        except Exception as e:  # noqa: BLE001 — defensive; surface as 401
            raise HTTPException(
                status_code=401, detail=f"token decode failed: {type(e).__name__}"
            )

        try:
            claims.validate()  # exp, nbf, iat
        except JoseError as e:
            raise HTTPException(
                status_code=401, detail=f"claim validation failed: {type(e).__name__}"
            )

        if self._issuer and claims.get("iss") != self._issuer:
            raise HTTPException(status_code=401, detail="wrong issuer")
        if self._audience:
            aud = claims.get("aud")
            aud_list = aud if isinstance(aud, list) else [aud] if aud else []
            if self._audience not in aud_list:
                raise HTTPException(status_code=401, detail="wrong audience")

        user_id = claims.get(self._user_claim)
        tenant_id = claims.get(self._tenant_claim)
        if not user_id:
            raise HTTPException(
                status_code=401, detail=f"missing {self._user_claim} claim"
            )
        if not tenant_id:
            raise HTTPException(
                status_code=401, detail=f"missing {self._tenant_claim} claim"
            )
        return (str(user_id), str(tenant_id))


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
