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
from .config.schema import as_bool, env_bool


def is_desktop() -> bool:
    """True in the local single-user desktop deployment (`ADK_CC_DESKTOP=1`)."""
    return env_bool("ADK_CC_DESKTOP")


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


def data_dir() -> Path:
    """Server-side data ROOT — identity, admin/tenant registry, credential
    store, audit log, central task store, and codex token all default UNDER
    here.

    Resolution: `$ADK_CC_DATA_DIR`, else the desktop data dir in desktop mode
    (so `$ADK_CC_DESKTOP_DATA` is its desktop alias), else `~/.adk-cc` for the
    web service. Returns the path WITHOUT creating it — each subsystem makes
    its own subdir only when it activates, so a bare web deployment never
    materializes dirs it doesn't use.

    The web fallback is HOME-based on purpose: it matches where the audit log,
    task store, and codex token lived before this root existed (so upgrades
    don't relocate data), and persistent state must not depend on whatever cwd
    the process happened to start in (systemd vs shell vs cron would otherwise
    each see a different "root"). A cwd-relative root briefly shipped and
    silently orphaned data — don't reintroduce it; use ADK_CC_DATA_DIR for an
    explicit per-deployment location.

    NB: the WORKSPACE root (`ADK_CC_WORKSPACE_ROOT`) and its `.memory`/`.wiki`
    siblings are a DIFFERENT axis — that data travels with the tenant workspace,
    not this server-data root."""
    raw = os.environ.get("ADK_CC_DATA_DIR")
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw)))
    if is_desktop():
        return desktop_data_dir()
    return Path.home() / ".adk-cc"


# --- desktop sandbox setting (live JSON, no restart) ------------------------
# Persisted in <desktop-data>/sandbox.json so the Settings toggle takes effect
# for new sessions without an app restart. An explicit env var always overrides
# the stored value (operator / test escape hatch).

def _sandbox_settings_path() -> Path:
    return desktop_data_dir() / "sandbox.json"


def read_sandbox_settings() -> dict:
    """The stored desktop sandbox settings ({mode, network, image}); {} if none
    or unreadable. Never raises."""
    try:
        import json

        p = _sandbox_settings_path()
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_sandbox_settings(settings: dict) -> None:
    """Persist {mode, network, image}. Atomic temp-swap."""
    import json

    p = _sandbox_settings_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(p)


def sandbox_mode() -> str:
    """Desktop sandbox preference: `host` (run on the host, today's default) or
    `container` (run the shell inside a local Docker/Podman container). Opt-in.
    Precedence: `ADK_CC_SANDBOX_MODE` env → stored setting → `host`. Only
    consulted in desktop mode; ignored (host) elsewhere."""
    env = os.environ.get("ADK_CC_SANDBOX_MODE")
    if env:
        return env.strip().lower()
    return str(read_sandbox_settings().get("mode") or "host").strip().lower()


def sandbox_network_enabled() -> bool:
    """Whether the container sandbox gets network. `ADK_CC_SANDBOX_NETWORK` env →
    stored setting → True (dev-friendly default)."""
    env = os.environ.get("ADK_CC_SANDBOX_NETWORK")
    if env is not None:
        return as_bool(env)
    val = read_sandbox_settings().get("network")
    return True if val is None else bool(val)


def sandbox_image() -> str:
    """The container image for the desktop sandbox. `ADK_CC_SANDBOX_IMAGE` env →
    stored setting → python:3.12-slim."""
    return (os.environ.get("ADK_CC_SANDBOX_IMAGE")
            or read_sandbox_settings().get("image")
            or "python:3.12-slim")


def sandbox_require() -> bool:
    """When True, an opted-in `container` sandbox that can't be brought up
    (no runtime) makes run_bash ERROR rather than silently falling back to host
    execution — a hard isolation guarantee. `ADK_CC_SANDBOX_REQUIRE` env → stored
    setting → False (default: warn + fall back to host, which stays usable)."""
    env = os.environ.get("ADK_CC_SANDBOX_REQUIRE")
    if env is not None:
        return as_bool(env)
    return bool(read_sandbox_settings().get("require"))


def container_runtime_available() -> bool:
    """True if a local container runtime (Docker/Podman) is detected. Cached
    inside the detector; safe to call repeatedly. Never raises."""
    try:
        from .sandbox.backends.container_runtime import detect_runtime

        return detect_runtime() is not None
    except Exception:  # noqa: BLE001
        return False


def sandbox_backend_name() -> str:
    """The configured sandbox backend name (noop/container/docker/e2b/
    sandbox_service/daytona/ssh); default `noop`. The single reader of
    `ADK_CC_SANDBOX_BACKEND`.

    Precedence: an explicit `ADK_CC_SANDBOX_BACKEND` always wins. Otherwise, in
    desktop mode with the Sandbox setting on, resolve to `container` — reflecting
    the user's INTENT. Whether a runtime is actually available is decided (and
    SIGNALED) at construction (`make_default_backend`), so an opted-in-but-
    unavailable sandbox warns/fails-closed there instead of silently reading as
    `noop` here (review #2)."""
    explicit = os.environ.get("ADK_CC_SANDBOX_BACKEND")
    if explicit:
        return explicit.lower()
    if is_desktop() and sandbox_mode() == "container":
        return "container"
    return "noop"


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
        return as_bool(v)
    return is_desktop()


def workspace_root() -> Optional[str]:
    """Explicit web/multi-tenant workspace root (`ADK_CC_WORKSPACE_ROOT`), or None
    (callers fall back to CWD)."""
    return os.environ.get("ADK_CC_WORKSPACE_ROOT")
