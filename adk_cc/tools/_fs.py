"""Shared filesystem helpers used by multiple tools.

Kept private (`_fs`) so tools depend on this module, not on each other.
In Stage C this module's `_resolve` becomes workspace-aware.
"""

from __future__ import annotations

from pathlib import Path


def resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()
