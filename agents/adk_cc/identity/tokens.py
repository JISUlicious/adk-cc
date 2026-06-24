"""JWT issuance for the in-house identity provider.

Mints RS256 access tokens whose claims match EXACTLY what `JwtAuthExtractor`
already validates (`sub` / `tenant` / `roles` / `scope` + iss/exp/nbf/iat). The
signing keypair is generated once and persisted, so tokens survive restarts and
across workers. The public half is exposed two ways:

  - `public_jwks()` → handed directly to a `JwtAuthExtractor(jwks=...)` so the
    SAME process validates its own tokens with NO network round-trip.
  - the `/.well-known/jwks.json` route serves it for external verifiers, and so
    swapping to Keycloak later is just pointing the extractor at a different JWKS.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from filelock import FileLock


class TokenIssuer:
    def __init__(
        self,
        *,
        key_path: str,
        issuer: str = "adk-cc",
        audience: str | None = None,
        ttl_s: int = 43200,
        user_claim: str = "sub",
        tenant_claim: str = "tenant",
        roles_claim: str = "roles",
        scopes_claim: str = "scope",
    ) -> None:
        self._key_path = Path(key_path)
        self.issuer = issuer
        self.audience = audience
        self.ttl_s = ttl_s
        self.user_claim = user_claim
        self.tenant_claim = tenant_claim
        self.roles_claim = roles_claim
        self.scopes_claim = scopes_claim
        self._key = None
        self._kid = ""
        self._load_or_generate()

    def _load_or_generate(self) -> None:
        from authlib.jose import JsonWebKey

        with FileLock(str(self._key_path) + ".lock"):
            if self._key_path.exists():
                doc = json.loads(self._key_path.read_text(encoding="utf-8"))
                self._key = JsonWebKey.import_key(doc["jwk"])
                self._kid = doc["kid"]
                return
            key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
            kid = key.thumbprint()
            self._key_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._key_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"kid": kid, "jwk": key.as_dict(is_private=True)}),
                encoding="utf-8",
            )
            tmp.replace(self._key_path)
            self._key, self._kid = key, kid

    def issue(
        self,
        *,
        user_id: str,
        tenant_id: str,
        roles=(),
        scopes=(),
        email: str = "",
        name: str = "",
        ttl_s: int | None = None,
    ) -> str:
        from authlib.jose import jwt

        now = int(time.time())
        ttl = self.ttl_s if ttl_s is None else ttl_s
        payload = {
            "iss": self.issuer,
            self.user_claim: user_id,
            self.tenant_claim: tenant_id,
            self.roles_claim: list(roles),
            self.scopes_claim: " ".join(scopes),
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
        }
        if self.audience:
            payload["aud"] = self.audience
        if email:
            payload["email"] = email
        if name:
            payload["name"] = name
        tok = jwt.encode({"alg": "RS256", "kid": self._kid}, payload, self._key)
        return tok.decode("utf-8") if isinstance(tok, bytes) else tok

    def public_jwks(self) -> dict:
        pub = self._key.as_dict(is_private=False)
        pub["kid"] = self._kid
        return {"keys": [pub]}
