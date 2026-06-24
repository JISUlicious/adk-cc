"""In-house identity: local email+password accounts that issue the same JWT
contract the server's `JwtAuthExtractor` already validates.

Email+password is the implemented variant; `IdentityProvider` is the seam for
future OIDC / Keycloak / SAML variants (see `provider.py`). Enabled by
``ADK_CC_AUTH_PASSWORD=1`` (see `service/server.py`)."""

from .models import UserRecord
from .provider import EmailPasswordProvider, Identity, IdentityProvider
from .service import IdentityService
from .store import JsonFileUserStore, UserStore
from .tokens import TokenIssuer

__all__ = [
    "IdentityService",
    "IdentityProvider",
    "EmailPasswordProvider",
    "Identity",
    "UserStore",
    "JsonFileUserStore",
    "TokenIssuer",
    "UserRecord",
]
