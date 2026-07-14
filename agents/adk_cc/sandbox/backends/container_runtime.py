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


def _version(cli: str, fmt: str) -> str:
    """`<cli> version --format <fmt>` → stripped stdout, or "" on failure /
    empty / a Go '<no value>' render (a nil field)."""
    try:
        p = subprocess.run([cli, "version", "--format", fmt],
                           capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    v = (p.stdout or "").strip()
    if p.returncode != 0 or not v or "no value" in v:
        return ""
    return v


def _probe(name: str) -> "Runtime | None":
    """Return a Runtime if `name` is on PATH AND its runtime actually answers.

    Two-tier so it works for Docker, Podman-machine (macOS/Windows), AND
    daemonless Podman on native Linux:
      1. `{{.Server.Version}}` — populated for Docker + a Podman machine; a fast
         liveness probe (empty/error when the daemon/VM is down).
      2. If that's empty (native-Linux Podman has no .Server), confirm the
         runtime is usable via `info` and report `{{.Client.Version}}`.
    """
    cli = shutil.which(name)
    if not cli:
        return None
    ver = _version(cli, "{{.Server.Version}}")
    if ver:
        return Runtime(name=name, version=ver, cli_path=cli)
    # Tier 2: no server field — is the runtime nonetheless usable locally?
    try:
        info = subprocess.run([cli, "info"], capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if info.returncode != 0:
        return None  # runtime genuinely down (e.g. Docker daemon not running)
    return Runtime(name=name, version=_version(cli, "{{.Client.Version}}") or "unknown", cli_path=cli)


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


def image_present(rt: Runtime, image: str) -> bool:
    """True if `image` is already available locally (no pull needed)."""
    try:
        proc = subprocess.run(
            [rt.cli_path, "image", "inspect", image],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def pull_image(rt: Runtime, image: str, *, timeout_s: float = 600.0) -> tuple[bool, str]:
    """Pull `image`. Returns (ok, message). Blocking — run off the event loop."""
    try:
        proc = subprocess.run(
            [rt.cli_path, "pull", image],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"pull timed out after {int(timeout_s)}s"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "pull failed").strip()
    return True, "ok"
