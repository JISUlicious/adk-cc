"""Live e2e for the desktop container sandbox — spins a REAL container via the
detected Docker/Podman runtime and asserts the load-bearing behaviors. SKIPS
(exit 0) when no runtime is available, so it's safe in CI without Docker.

Only BENIGN commands run inside the container. Isolation is asserted by OBSERVING
the container's view (e.g. a host file outside the mount is not visible), never by
running anything destructive.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_container_sandbox.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.sandbox.backends.container_runtime import detect_runtime
from adk_cc.sandbox.backends.local_container_backend import LocalContainerBackend, sweep_orphans
from adk_cc.sandbox.config import FsReadConfig, FsWriteConfig, NetworkConfig
from adk_cc.sandbox.workspace import WorkspaceRoot

_passed = _failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _ws(path: str, extra: tuple[str, ...] = ()) -> WorkspaceRoot:
    return WorkspaceRoot(tenant_id="local", session_id="e2e-sbx", abs_path=path, extra_roots=extra)


async def _exec(b, cmd, cwd, timeout=30, network=NetworkConfig()):
    return await b.exec(cmd, fs_write=FsWriteConfig(), network=network, timeout_s=timeout, cwd=cwd)


async def run(rt) -> None:
    proj = tempfile.mkdtemp(prefix="sbx-proj-")
    outside = tempfile.mkdtemp(prefix="sbx-outside-")  # a host dir NOT mounted
    Path(outside, "secret_host_file").write_text("host-only")
    Path(proj, "seed.txt").write_text("hello")

    b = LocalContainerBackend(session_id="e2e-sbx", runtime=rt, network_enabled=True)

    # Make _runtime_env() yield a fake secret so we can prove env-by-name
    # forwarding reaches the command (the value is passed via the CLI subprocess
    # env, referenced by name on argv — never as a literal arg).
    async def _fake_env():
        return {"MY_API_KEY": "sk-secret-value-123"}

    b._runtime_env = _fake_env  # type: ignore[method-assign]

    try:
        await b.ensure_workspace(_ws(proj))

        # 1. cwd/pwd is the REAL host path (identical-path mount, no /workspace remap)
        r = await _exec(b, "pwd", cwd=proj)
        check("pwd is the real host project path (no remap)",
              r.exit_code == 0 and r.stdout.strip() == os.path.realpath(proj),
              f"got {r.stdout.strip()!r} want {os.path.realpath(proj)!r}")

        # 2. runs in a container, not the host (different kernel/hostname; no host uname)
        r = await _exec(b, "cat /etc/os-release 2>/dev/null | head -1; uname -s", cwd=proj)
        check("executes inside a Linux container", r.exit_code == 0 and "Linux" in r.stdout)

        # 3. in-place: a file the container writes appears on the HOST immediately
        r = await _exec(b, "echo 'from-container' > made_in_container.txt", cwd=proj)
        host_file = Path(proj, "made_in_container.txt")
        check("container writes land in the real project (in-place)",
              r.exit_code == 0 and host_file.exists() and host_file.read_text().strip() == "from-container")

        # 4. host-user ownership — the file is owned by us, not root
        check("in-place writes are owned by the host user (not root)",
              host_file.stat().st_uid == os.getuid())

        # 5. and the host can already see the seed file through the mount
        r = await _exec(b, "cat seed.txt", cwd=proj)
        check("mounted project contents are visible in the container",
              r.exit_code == 0 and r.stdout.strip() == "hello")

        # 6. HOST ISOLATION: a host path OUTSIDE the mount is NOT visible inside
        r = await _exec(b, f"cat {os.path.join(outside, 'secret_host_file')} 2>&1 || echo BLOCKED", cwd=proj)
        check("host files outside the mount are NOT reachable",
              "host-only" not in r.stdout and "BLOCKED" in r.stdout)

        # 7. secret injection: the VALUE is visible to the command...
        r = await _exec(b, 'echo "$MY_API_KEY"', cwd=proj)
        check("injected secret is available to the command",
              r.exit_code == 0 and r.stdout.strip() == "sk-secret-value-123")

        # 8. host-direct file I/O (inherited from NoopBackend) reads the same bytes
        text = await b.read_text(str(host_file), fs_read=FsReadConfig(allow_paths=(f"{proj}/**",)))
        check("host-direct read_text sees the container's write",
              text.strip() == "from-container")

        # 9. resource-limit flag actually applied (pids-limit visible in inspect)
        import subprocess
        insp = subprocess.run(
            [rt.cli_path, "inspect", "-f", "{{.HostConfig.PidsLimit}}", b._name],
            capture_output=True, text=True, timeout=15)
        check("pids-limit is applied to the container",
              insp.returncode == 0 and insp.stdout.strip() not in ("", "0", "<nil>"))

        # 10. timeout actually kills a hung command
        r = await _exec(b, "sleep 30", cwd=proj, timeout=2)
        check("a hung command is killed at the timeout", r.timed_out and r.exit_code != 0)
    finally:
        await b.close()

    # 11. network OFF: a fresh container with network disabled can't resolve/reach out
    b2 = LocalContainerBackend(session_id="e2e-sbx-nonet", runtime=rt, network_enabled=False)
    try:
        await b2.ensure_workspace(_ws(proj))
        r = await _exec(b2, "getent hosts example.com >/dev/null 2>&1 && echo UP || echo NONET", cwd=proj)
        check("network-off container cannot resolve/reach the internet", "NONET" in r.stdout)
    finally:
        await b2.close()

    # 12. close() is per-turn and must LEAVE the container (per-session survival);
    # remove() is the explicit teardown that reaps it.
    import subprocess

    def _exists(name: str) -> str:
        # anchored so 'adk-cc-e2e-sbx' doesn't also match 'adk-cc-e2e-sbx-nonet'
        return subprocess.run([rt.cli_path, "ps", "-aq", "--filter", f"name=^{name}$"],
                              capture_output=True, text=True, timeout=15).stdout.strip()

    check("close() LEAVES the session container (per-session, not per-turn)",
          bool(_exists("adk-cc-e2e-sbx")))
    await b.remove()
    check("remove() reaps the session container", not _exists("adk-cc-e2e-sbx"))


def main() -> int:
    rt = detect_runtime()
    if rt is None:
        print("[SKIP] no local container runtime (docker/podman) detected")
        return 0
    print(f"live container e2e via {rt.name} {rt.version}:")
    # wiring: desktop + container-mode must construct a LocalContainerBackend
    os.environ["ADK_CC_DESKTOP"] = "1"
    os.environ["ADK_CC_SANDBOX_MODE"] = "container"
    from adk_cc.sandbox import make_default_backend
    from adk_cc import deployment
    wired = make_default_backend(session_id="wire-check")
    check("make_default_backend yields the container backend when selected",
          deployment.sandbox_backend_name() == "container"
          and isinstance(wired, LocalContainerBackend))
    try:
        asyncio.run(run(rt))
    finally:
        sweep_orphans(rt)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
