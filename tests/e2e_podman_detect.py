"""EVIDENCE for review finding #6 — Podman detection, against a REAL Podman.

The detection module is stdlib-only, so we run the ACTUAL detect_runtime() code
inside a Fedora-family `quay.io/podman/stable` container (real Podman) and assert
it identifies the runtime. This tests the DETECTION probe (which is all #6 is
about) — not Podman running the sandbox (nested Podman needs privileges and isn't
representative of a native rootless host for that).

Needs Docker + the quay.io/podman/stable image PRESENT locally (we don't
auto-pull ~300 MB); SKIPS otherwise. Podman-in-Docker needs --privileged to
re-exec into its user namespace.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_podman_detect.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PODMAN_IMAGE = "quay.io/podman/stable"
MODULE = os.path.join(REPO, "agents/adk_cc/sandbox/backends/container_runtime.py")

_INNER = r"""
import sys, importlib.util, json
spec = importlib.util.spec_from_file_location('cr', '/tmp/cr.py')
m = importlib.util.module_from_spec(spec)
sys.modules['cr'] = m           # so the Runtime dataclass resolves under py3.14
spec.loader.exec_module(m)
rt = m.detect_runtime()
print("RESULT " + json.dumps(
    None if rt is None else {"name": rt.name, "version": rt.version, "cli": rt.cli_path}))
"""


def _have(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    if not _have(["docker", "version", "--format", "{{.Server.Version}}"]):
        print("[SKIP] docker not available to host the Podman container")
        return 0
    if not _have(["docker", "image", "inspect", PODMAN_IMAGE]):
        print(f"[SKIP] {PODMAN_IMAGE} not present — `docker pull {PODMAN_IMAGE}` to enable")
        return 0

    print("podman detection evidence (real Podman in a Fedora container):")
    proc = subprocess.run(
        ["docker", "run", "--rm", "--privileged",
         "-e", "ADK_CC_SANDBOX_RUNTIME=podman",
         "-v", f"{MODULE}:/tmp/cr.py:ro",
         PODMAN_IMAGE, "python3", "-c", _INNER],
        capture_output=True, text=True, timeout=180)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    if line is None:
        print("  [FAIL] no RESULT from the container")
        print("  stderr:", proc.stderr.strip()[:400])
        return 1
    rt = json.loads(line[len("RESULT "):])
    print(f"  detect_runtime() inside Fedora/Podman → {rt}")
    ok = rt is not None and rt["name"] == "podman" and rt["version"]
    print(f"  [{'PASS' if ok else 'FAIL'}] real Podman is detected"
          + ("" if ok else " — #6 (undetected Podman) reproduced"))
    # Also record whether the ORIGINAL single-tier probe would have sufficed
    # (i.e. does modern Podman populate .Server.Version?).
    srv = subprocess.run(
        ["docker", "run", "--rm", "--privileged", PODMAN_IMAGE,
         "podman", "version", "--format", "{{.Server.Version}}"],
        capture_output=True, text=True, timeout=60)
    print(f"  note: podman '{{{{.Server.Version}}}}' → {srv.stdout.strip()!r} "
          f"(non-empty ⇒ tier-1 alone already detects it)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
