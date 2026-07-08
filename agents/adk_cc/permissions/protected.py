"""Protected-path floor for desktop filesystem access.

Once the desktop agent can be *granted* directories outside its bound project
(see the grantable-scope plan), some paths must never be freely reachable —
above all our own secret material. This module classifies a resolved absolute
path as:

  - ``"deny"`` — hard block, wins even over ``bypassPermissions`` and any
    grant / Allow-always rule. Reserved for **secret material**: the desktop
    credential store + key, and common on-disk credential locations
    (``~/.ssh``, ``~/.aws``, cloud SDK creds, ``~/.netrc``, …). This upholds the
    project's non-negotiable rule that secrets never enter model input/output —
    which only becomes reachable once reads open up.
  - ``"ask"`` — never auto-approved (a grant can't silently cover it), but the
    user may confirm. Mirrors Claude Code's "protected paths always prompt" for
    shell/tool config (``~/.gitconfig``, ``~/.zshrc``, ``**/.git/config``, …).
  - ``None`` — not protected.

DESKTOP ONLY: `classify_path` returns None off the desktop profile — in
web/multi-tenant the hard per-tenant sandbox already confines access, and these
host paths never occur inside a tenant workspace.

The default lists are overridable/extendable via ``ADK_CC_PROTECTED_DENY`` and
``ADK_CC_PROTECTED_ASK`` (comma/colon-separated fnmatch patterns; ``~`` expanded).
"""

from __future__ import annotations

import fnmatch
import os
from typing import Optional

# Hard-deny: pure credential material. `**` behaves like `*` under fnmatch (it
# spans `/`), so `~/.ssh/**` covers everything below `~/.ssh`; the bare dir entry
# covers the directory itself.
_DENY_DEFAULT = (
    "~/.ssh", "~/.ssh/**",
    "~/.aws", "~/.aws/**",
    "~/.config/gcloud/**",
    "~/.config/gh/hosts.yml",
    "~/.gnupg", "~/.gnupg/**",
    "~/.kube/config",
    "~/.docker/config.json",
    "~/.git-credentials",
    "~/.netrc",
)

# Always-ask: shell / tool config an agent might legitimately touch, but never
# without the user seeing it (matches Claude Code's protected paths).
_ASK_DEFAULT = (
    "~/.gitconfig",
    "~/.npmrc", "~/.pypirc",
    "~/.zshrc", "~/.zprofile", "~/.bashrc", "~/.bash_profile", "~/.profile",
    "**/.git/config",
)


def _env_patterns(var: str) -> list[str]:
    raw = os.environ.get(var, "") or ""
    return [p.strip() for p in raw.replace(":", ",").split(",") if p.strip()]


def _secret_store_patterns() -> list[str]:
    """Absolute patterns for our own credential store + Fernet key (desktop data
    dir). Kept dynamic because the data dir is env-configurable."""
    try:
        from .. import deployment

        # realpath so it lines up with the realpath'd input in classify_path
        # (macOS /var/folders → /private/var/folders, symlinked homes, …).
        d = os.path.realpath(str(deployment.desktop_data_dir())).rstrip("/")
    except Exception:
        return []
    return [f"{d}/secrets", f"{d}/secrets/**", f"{d}/credential.key"]


def _expand(patterns) -> list[str]:
    return [os.path.expanduser(p) for p in patterns if p]


def _deny_patterns() -> list[str]:
    return (
        _secret_store_patterns()
        + _expand(_DENY_DEFAULT)
        + _expand(_env_patterns("ADK_CC_PROTECTED_DENY"))
    )


def _ask_patterns() -> list[str]:
    return _expand(_ASK_DEFAULT) + _expand(_env_patterns("ADK_CC_PROTECTED_ASK"))


def _realpath_prefix(pattern: str) -> str:
    """Realpath the non-glob prefix of an (already expanduser'd) pattern so a
    symlinked `$HOME` (`/home`→`/var/home`, NFS automounts) still matches the
    realpath'd input. `~/.ssh/**` → `/var/home/user/.ssh/**`."""
    star = next((i for i, ch in enumerate(pattern) if ch in "*?["), len(pattern))
    prefix, glob = pattern[:star], pattern[star:]
    if not prefix:
        return pattern
    try:
        rp = os.path.realpath(prefix)
    except OSError:
        return pattern
    if prefix.endswith("/") and not rp.endswith("/"):
        rp += "/"
    return rp + glob


def _matches(target: str, pattern: str) -> bool:
    """Case-folded fnmatch against both the pattern and its realpath-prefixed
    form — so `~/.SSH` (case-insensitive FS) and a symlinked `$HOME` can't evade
    the floor."""
    for form in (pattern, _realpath_prefix(pattern)):
        if fnmatch.fnmatch(target, form.lower()):
            return True
    return False


def classify_path(abs_path: str) -> Optional[str]:
    """Return ``"deny"`` | ``"ask"`` | ``None`` for a resolved absolute path.

    Desktop-only (returns None otherwise). Deny takes precedence over ask. Match
    is case-folded and covers a symlinked `$HOME`, since this is a hard security
    floor and the FS may be case-insensitive (macOS)."""
    if not abs_path:
        return None
    from .. import deployment

    if not deployment.is_desktop():
        return None
    target = os.path.realpath(abs_path).lower()
    for pat in _deny_patterns():
        if _matches(target, pat):
            return "deny"
    for pat in _ask_patterns():
        if _matches(target, pat):
            return "ask"
    return None
