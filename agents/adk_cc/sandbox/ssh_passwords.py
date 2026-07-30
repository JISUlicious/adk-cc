"""Encrypted store for remote SSH passwords (desktop).

Why a small dedicated store instead of `CredentialProvider`: the provider's API
is async, and the four places that build an `SshTransport` — the backend
factory, the desktop workspace resolver, the file panel and the checkpoint store
— are all SYNCHRONOUS. Threading async credential reads through them would be a
refactor of everything on the path for one field. This is sync, uses the SAME
Fernet key as the credential store (`ADK_CC_CREDENTIAL_KEY`, which the desktop
launcher persists in `credential.key`), and lives under the same admin-data
root.

What is deliberately NOT here:
  - The password never goes in `projects.json` next to the host and path. That
    file is plain JSON the user can open; a binding records only
    `"auth": "password"` so the UI knows to offer the field.
  - No plaintext fallback. Without a key, `put` refuses and says why, rather
    than quietly writing a readable secret.

Keys are `sha256(host:port)`, so the filename does not leak the hostname to
anyone listing the directory.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


class SshPasswordStoreUnavailable(RuntimeError):
    """No encryption key, so a password cannot be stored safely."""


def _key() -> Optional[str]:
    return os.environ.get("ADK_CC_CREDENTIAL_KEY") or None


def _root() -> Path:
    from .. import deployment

    return Path(deployment.data_dir()) / "admin-data" / "ssh-passwords"


def _slot(host: str, port: Optional[int]) -> str:
    raw = f"{host}:{port or 22}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _path(host: str, port: Optional[int]) -> Path:
    return _root() / f"{_slot(host, port)}.enc"


def available() -> bool:
    """True when a password CAN be stored (a key exists)."""
    return _key() is not None


def put(host: str, port: Optional[int], password: str) -> None:
    key = _key()
    if not key:
        raise SshPasswordStoreUnavailable(
            "no ADK_CC_CREDENTIAL_KEY, so an SSH password cannot be encrypted. "
            "The desktop app generates one on first launch; for a bare server "
            "run, set it (see credentials/impls.py for how to generate)."
        )
    from cryptography.fernet import Fernet

    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    p = _path(host, port)
    blob = Fernet(key.encode()).encrypt(password.encode("utf-8"))
    # Write-then-chmod via a private temp file: a reader racing the write must
    # never catch it at default permissions.
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    os.replace(tmp, p)


def get(host: str, port: Optional[int]) -> Optional[str]:
    """The stored password, or None. Never raises — a broken store must not
    take down every remote operation; it degrades to "no password", which the
    ssh layer reports as an ordinary auth failure."""
    key = _key()
    if not key:
        return None
    p = _path(host, port)
    if not p.exists():
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode()).decrypt(p.read_bytes()).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        _log.warning("could not read stored ssh password for %s: %s",
                     host, type(e).__name__)
        return None


def delete(host: str, port: Optional[int]) -> None:
    try:
        _path(host, port).unlink(missing_ok=True)
    except OSError:
        pass
