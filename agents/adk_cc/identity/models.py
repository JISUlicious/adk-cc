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
    status: str = "active"  # active | disabled
    created: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UserRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
