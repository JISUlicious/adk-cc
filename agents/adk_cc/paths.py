"""One canonicalisation for paths ENTERING the system (P3, path audit).

Two independent implementations existed (WorkspaceRoot: realpath;
skill_trust: expanduser+resolve with a raw-string OSError fallback), and the
raw fallback produced keys that could never match a resolved key — trust for
such a root silently never persisted. Apply this at edges only (construction,
persistent keys, env roots), never mid-pipeline; REMOTE paths stay lexical by
design and must not pass through here.
"""
from __future__ import annotations

import os


def canonical(p: "os.PathLike | str") -> str:
    """expanduser + realpath, with a LEXICAL (still-stable) fallback."""
    s = os.path.expanduser(str(p))
    try:
        return os.path.realpath(s)
    except OSError:
        return os.path.normpath(os.path.abspath(s))
