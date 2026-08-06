"""The proxy build args reach the tools that need them.

Reported from a restricted network: apt could not reach its repositories at
build time until `no_proxy` was set. That case is invisible to every other
test here, because an unrestricted build never exercises the flags at all —
they default to empty and nothing notices whether they are wired up.

Proving a proxy WORKS needs a proxy. Proving the wiring works does not: point
the proxy at a dead port and the behaviour becomes unambiguous.

  - proxy set, no bypass      -> apt must FAIL (it really used the proxy)
  - proxy set, NO_PROXY set   -> apt must SUCCEED (the bypass really applied)

The second is the one the user hit, and the first is what stops the second
from passing vacuously — without it, "apt succeeded" could just mean the
proxy setting was ignored.

Also checks the vars survive into the RUNTIME image, since a skill's lazy
`pip install` (#94) needs them long after the build.

Run: .venv/bin/python tests/e2e_sandbox_build_proxy.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEAD = "http://127.0.0.1:9"          # discard port: refuses instantly
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _build(tag: str, args: dict[str, str]) -> tuple[int, str]:
    """Build only as far as the apt layer; return (exit, combined output)."""
    cmd = ["docker", "build", "-t", tag, "-f", "Dockerfile.sandbox"]
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    # --target would be cleaner, but this Dockerfile is single-stage; the apt
    # layer is early enough that a failure there ends the build in seconds.
    p = subprocess.run(cmd + ["."], cwd=REPO, capture_output=True, text=True,
                       timeout=3600)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    if not shutil.which("docker"):
        print("SKIP: docker not installed."); return 0
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP: docker daemon not running."); return 0

    # 1. A dead proxy with no bypass must break the apt layer. This is what
    #    proves the proxy plumbing is live rather than silently ignored.
    rc, out = _build("adk-cc-sandbox:proxy-none", {"APT_PROXY": DEAD})
    used_proxy = ("127.0.0.1:9" in out
                  and ("Unable to connect" in out or "Could not connect" in out))
    check("APT_PROXY actually reaches apt (dead proxy breaks the build)",
          rc != 0 and used_proxy,
          f"exit={rc}; expected apt to fail against {DEAD}")

    # 2. The same dead proxy, plus a bypass for the Debian repos. apt must now
    #    reach them directly and the build must get PAST the apt layer.
    #    (Verified behaviour: env no_proxy overrides the apt.conf proxy.)
    rc2, out2 = _build("adk-cc-sandbox:proxy-bypass", {
        "APT_PROXY": DEAD,
        "NO_PROXY": "deb.debian.org,security.debian.org",
    })
    apt_failed = "Unable to connect to 127.0.0.1:9" in out2
    check("NO_PROXY lets apt bypass the proxy (the reported failure)",
          not apt_failed,
          "apt still went through the dead proxy despite NO_PROXY")
    check("and the build proceeds past the apt layer", rc2 == 0, f"exit={rc2}")

    # 3. The vars must persist into the running container — a skill's lazy pip
    #    install happens at RUNTIME, long after these args are gone.
    if rc2 == 0:
        p = subprocess.run(
            ["docker", "run", "--rm", "adk-cc-sandbox:proxy-bypass",
             "sh", "-c", "echo \"$no_proxy|$NO_PROXY\""],
            capture_output=True, text=True, timeout=300)
        got = (p.stdout or "").strip()
        check("no_proxy survives into the runtime image, in both cases",
              got.count("deb.debian.org") == 2, f"got {got!r}")

    # Untag rather than force-remove. These images share base layers with
    # adk-cc-sandbox:latest, and `rmi -f` here made the NEXT build of latest
    # die in the exporter with "parent snapshot ... does not exist" on Docker
    # Desktop's containerd store — a build that had otherwise completed every
    # step. Plain rmi drops the tag and leaves shared layers to normal GC.
    for tag in ("adk-cc-sandbox:proxy-none", "adk-cc-sandbox:proxy-bypass"):
        subprocess.run(["docker", "rmi", tag], capture_output=True)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
