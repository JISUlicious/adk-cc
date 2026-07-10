"""Identity data records (storage-facing)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class UserRecord:
    """A local account. `tenant_id` is the org the user belongs to (= the JWT
    `tenant` claim); `roles` become the JWT `roles` claim. `password_hash` is
    opaque to everything but `passwords.py`."""

    user_id: str
    email: str
    password_hash: str
    name: str = ""
    tenant_id: str = "local"
    roles: list[str] = field(default_factory=list)
    status: str = "active"  # active | disabled | pending (access request awaiting approval)
    created: str = ""
    note: str = ""  # requester's message on an access request (shown to the approving admin)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UserRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class ApiKeyRecord:
    """A personal access token (PAT). The token itself is a long-lived JWT shown
    ONCE at creation and never stored; this record is the revocable handle —
    validation rejects a token whose `id` (the JWT `jti`) is revoked or absent."""

    id: str
    user_id: str
    name: str = ""
    created: str = ""
    last_used: str = ""
    revoked: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ApiKeyRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class RefreshTokenRecord:
    """One refresh token, stored by HASH (the raw token never touches disk).
    Rotation revokes the old record and links it forward via `rotated_to`;
    presenting an already-rotated token is treated as theft and kills the
    whole chain."""

    id: str  # sha256 hex of the raw token
    user_id: str
    expires: float  # epoch seconds
    revoked: bool = False
    rotated_to: str = ""  # hash of the successor, once rotated
    created: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RefreshTokenRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class AuditEvent:
    """One identity/org/account action — who did what, when, in which org.
    Powers the audit log + the usage summary (aggregated per actor)."""

    id: str
    ts: str  # ISO-8601 UTC
    tenant_id: str
    actor_id: str
    actor_email: str = ""
    action: str = ""  # e.g. login, user.created, member.role, apikey.created
    target: str = ""  # what it acted on (an email, a key name, …)
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AuditEvent":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class InviteRecord:
    """A pending invitation to join a tenant/org with a given role. The token
    is the share secret (the invite link carries it); accepting it creates a
    user in `tenant_id` with `role`."""

    token: str
    email: str
    tenant_id: str
    role: str = "member"
    created: str = ""
    expires: float = 0.0  # epoch seconds; 0 = never
    status: str = "pending"  # pending | accepted | revoked
    accepted_by: str = ""  # user_id, once accepted

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "InviteRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
