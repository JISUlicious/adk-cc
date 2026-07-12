"""Local container-runtime detection (Docker / Podman).

Desktop opt-in sandbox: detect whether a local container runtime is available so
the agent's shell can run inside a container instead of on the host. Kept
deliberately small — a CLI probe, cached — because we drive the runtime through
its `docker`/`podman` CLI (Podman is a drop-in `docker` CLI, and both transparently
front their macOS/Windows VM, which a raw SDK/socket does not).

`detect_runtime()` is cached module-level; call `reset_cache()` after a settings
change. The probe shells out (`<rt> version`) with a short timeout, so callers
should treat it as blocking and keep it off hot paths / cache the result.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Runtime:
    """A usable local container runtime."""

    name: str  # "docker" | "podman"
    version: str  # server version string (best-effort)
    cli_path: str  # absolute path to the CLI binary


_PROBE_TIMEOUT_S = 4.0


class _Unset:
    """Sentinel so a cached 'no runtime' (None) is distinct from 'not probed'."""


_UNSET = _Unset()
# Starts UNSET (not None) — None is a valid *cached* result meaning "probed, none
# found", so the initial value must be a distinct sentinel or the first call
# would short-circuit to None without ever probing.
_cache: "Runtime | None | _Unset" = _UNSET


def _candidates() -> list[str]:
    """Runtime names to try, honoring ADK_CC_SANDBOX_RUNTIME=auto|docker|podman."""
    pref = (os.environ.get("ADK_CC_SANDBOX_RUNTIME") or "auto").strip().lower()
    if pref in ("docker", "podman"):
        return [pref]
    return ["docker", "podman"]  # auto: prefer docker, then podman


def _probe(name: str) -> "Runtime | None":
    """Return a Runtime if `name` is on PATH AND its daemon/VM answers."""
    cli = shutil.which(name)
    if not cli:
        return None
    try:
        # `version --format {{.Server.Version}}` hits the daemon and exits
        # non-zero (with an empty/blank stdout) when it's not reachable — a
        # fast, side-effect-free liveness probe.
        proc = subprocess.run(
            [cli, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    ver = (proc.stdout or "").strip()
    if proc.returncode != 0 or not ver:
        return None
    return Runtime(name=name, version=ver, cli_path=cli)


def detect_runtime() -> "Runtime | None":
    """The first available local runtime, or None. Cached; never raises."""
    global _cache
    if not isinstance(_cache, _Unset):
        return _cache  # type: ignore[return-value]
    found: "Runtime | None" = None
    try:
        for name in _candidates():
            found = _probe(name)
            if found is not None:
                break
    except Exception:  # noqa: BLE001 — detection must never break bring-up
        found = None
    _cache = found
    return found


def reset_cache() -> None:
    """Forget the cached probe (e.g. after the user changes the Sandbox setting
    or starts Docker Desktop). The next detect_runtime() re-probes."""
    global _cache
    _cache = _UNSET
