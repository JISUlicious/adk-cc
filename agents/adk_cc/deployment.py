"""Deployment profile — the single place that reads the mode-selection env vars.

adk-cc runs as a multi-tenant **web service** OR a local single-user **desktop
app**. Historically the "which mode" signals (`ADK_CC_DESKTOP`,
`ADK_CC_SANDBOX_BACKEND`, the desktop data dir, …) were read at many scattered
points, so "what desktop means" was smeared across files. This module is the one
true reader for the handful that matter; callers delegate here.

Kept deliberately **dependency-light** (stdlib only) so it is safe to import from
the package's import-time dotenv bootstrap without pulling FastAPI/ADK.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def is_desktop() -> bool:
    """True in the local single-user desktop deployment (`ADK_CC_DESKTOP=1`)."""
    return os.environ.get("ADK_CC_DESKTOP") == "1"


def desktop_data_dir() -> Path:
    """The desktop data dir (sessions, worktrees, secrets, settings.env): value of
    `$ADK_CC_DESKTOP_DATA`, else `~/.adk-cc-desktop`. Absolute; created if missing.

    (Canonical version — `service/desktop_routes.desktop_data_dir` delegates here.
    The import-time bootstrap in `__init__.py` keeps its own no-mkdir path build to
    stay dependency-light, and mirrors this location.)"""
    raw = os.environ.get("ADK_CC_DESKTOP_DATA") or os.path.expanduser("~/.adk-cc-desktop")
    p = Path(os.path.abspath(os.path.expanduser(raw)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def sandbox_backend_name() -> str:
    """The configured sandbox backend name (noop/docker/e2b/sandbox_service/
    daytona); default `noop`. The single reader of `ADK_CC_SANDBOX_BACKEND`."""
    return os.environ.get("ADK_CC_SANDBOX_BACKEND", "noop").lower()


def noop_ack_host_exec() -> bool:
    """Whether the noop backend may exec against a "prod-shaped" host path (outside
    `$HOME`/`/tmp`/…). `ADK_CC_NOOP_ACK_HOST_EXEC` wins if set; otherwise defaults
    to `is_desktop()` — desktop is explicitly single-user host exec, and working
    in-place in the user's real project root (which may be under `/opt`,
    `/Volumes/…`, `/Users/Shared`, …) is the point.

    (Phase-1: defined but not yet consulted, so behavior is unchanged. Phase-2
    wires it — the Tauri sidecar also sets the env var for the shipped app.)"""
    v = os.environ.get("ADK_CC_NOOP_ACK_HOST_EXEC")
    if v is not None:
        return v == "1"
    return is_desktop()


def workspace_root() -> Optional[str]:
    """Explicit web/multi-tenant workspace root (`ADK_CC_WORKSPACE_ROOT`), or None
    (callers fall back to CWD)."""
    return os.environ.get("ADK_CC_WORKSPACE_ROOT")
