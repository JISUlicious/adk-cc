"""Single construction point for the CredentialProvider.

Every consumer (MCP toolset, sandbox env injection, the per-user Settings API,
the secret-redaction plugin) builds the provider through this one function so
they all observe the SAME store — the encrypted-file impl shares state via the
filesystem, the in-memory impl via a process-wide singleton, so multiple
instances built the same way are interchangeable.

Knobs (unchanged from the original inline construction in agent.py):
    ADK_CC_CREDENTIAL_PROVIDER   memory (default) | encrypted_file
    ADK_CC_CREDENTIAL_STORE_DIR  root dir (defaults to <ADK_CC_DATA_DIR>/secrets)
    ADK_CC_CREDENTIAL_KEY        Fernet key (encrypted_file; see impls)
"""

from __future__ import annotations

import os
from typing import Optional

from .provider import CredentialProvider


def credential_provider_from_env() -> Optional[CredentialProvider]:
    """Build the configured provider, or None when explicitly disabled.

    Returns None only when `ADK_CC_CREDENTIAL_PROVIDER=none`; otherwise defaults
    to the in-memory provider (dev). Callers treat None as "secrets feature off".
    """
    kind = os.environ.get("ADK_CC_CREDENTIAL_PROVIDER", "memory").lower()
    if kind == "none":
        return None
    if kind == "memory":
        from .impls import InMemoryCredentialProvider

        return InMemoryCredentialProvider()
    if kind == "encrypted_file":
        from .impls import EncryptedFileCredentialProvider

        store_dir = os.environ.get("ADK_CC_CREDENTIAL_STORE_DIR")
        if not store_dir:
            from .. import deployment as _dep

            store_dir = str(_dep.data_dir() / "secrets")
            # Loud on purpose: encrypted secrets are landing at a DERIVED
            # location. The old code hard-failed here; we default instead (dev
            # convenience) but the operator must be able to see where the
            # store lives, or a later cwd/home change looks like data loss.
            import logging

            logging.getLogger(__name__).warning(
                "ADK_CC_CREDENTIAL_STORE_DIR not set — encrypted secret store "
                "defaulting to %s (set it explicitly for production).", store_dir,
            )
        return EncryptedFileCredentialProvider(root=store_dir)
    raise RuntimeError(
        f"unknown ADK_CC_CREDENTIAL_PROVIDER={kind!r}; "
        "valid: memory, encrypted_file, none"
    )
