"""DockerBackend against a real daemon: can it actually host a session?

Everything here was reported from production and none of it was catchable by
the existing tests, which either mocked the backend or probed the image alone.
Four separate user-visible failures, one root cause each:

  read-only rootfs   `uv` -> "failed to initialize cache at
                     /home/sandbox/.cache/uv: read-only file system", because
                     the image's HOME sits on the read-only rootfs
  read-only rootfs   write_file -> raw "400 ... container rootfs is marked
                     read-only" from Docker's archive API on /tmp — a path a
                     shell in the same container writes without complaint
  network=none       pip install / DB / API calls all fail, and the parameter
                     exec() takes to express this was never read
  wrong HOME         the per-user cache mount pointed at /root/.cache while
                     the container runs as uid 1000

So this drives the REAL backend: create a container, write, exec, provision.
Needs a Docker daemon and adk-cc-sandbox:latest; skips cleanly without them.

Run: .venv/bin/python tests/e2e_docker_backend_runtime.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


async def _run(net: bool) -> None:
    from adk_cc.sandbox.backends.docker_backend import (CONTAINER_HOME,
                                                        DockerBackend)
    from adk_cc.sandbox.workspace import WorkspaceRoot

    os.environ["ADK_CC_SANDBOX_NETWORK"] = "1" if net else "0"
    ws_dir = tempfile.mkdtemp(prefix=f"dockbe-{'net' if net else 'nonet'}-")
    ws = WorkspaceRoot(tenant_id="local", session_id=f"dockbe-{net}",
                       abs_path=ws_dir)
    ws_dir = ws.abs_path          # canonical; cwd must match to translate
    be = DockerBackend(session_id=f"dockbe-{'net' if net else 'nonet'}",
                       tenant_id="local", workspace_abs_path=ws_dir)
    fsw, fsr = ws.fs_write_config(), ws.fs_read_config()
    from adk_cc.sandbox.config import NetworkConfig

    print(f"\n--- ADK_CC_SANDBOX_NETWORK={'1' if net else '0'} ---")
    try:
        await be.ensure_workspace(ws)

        # 1. HOME must be writable, or nothing that caches works.
        r = await be.exec('echo "$HOME"; touch "$HOME/.probe" && echo HOME_WRITABLE',
                          fs_write=fsw, network=NetworkConfig(), timeout_s=120,
                          cwd=ws_dir)
        check("HOME is writable", "HOME_WRITABLE" in r.stdout,
              f"HOME={r.stdout.strip()!r} {r.stderr.strip()[:200]}")
        check("HOME is the workspace-backed dir, not the read-only rootfs",
              CONTAINER_HOME in r.stdout, r.stdout.strip()[:120])

        # 2. The uv cache — the exact reported failure.
        r = await be.exec(
            'uv cache dir && uv venv --clear "$HOME/.probe-venv" 2>&1 | tail -2',
            fs_write=fsw, network=NetworkConfig(), timeout_s=300, cwd=ws_dir)
        out = r.stdout + r.stderr
        # Positive evidence required: "no read-only error" is also true when
        # the exec never ran at all, which is exactly how this passed
        # vacuously against a broken harness.
        check("uv can initialize its cache (the reported error)",
              r.exit_code == 0 and "read-only file system" not in out.lower()
              and CONTAINER_HOME in out,
              f"exit={r.exit_code} " + " ".join(out.split())[:220])

        # 3. write_file into /tmp — put_archive refuses it on a read-only
        #    rootfs; the fallback must carry it.
        try:
            await be.write_text("/tmp/probe-write.txt", "hello-from-write\n",
                                fs_write=fsw)
            r = await be.exec("cat /tmp/probe-write.txt", fs_write=fsw,
                              network=NetworkConfig(), timeout_s=60, cwd=ws_dir)
            check("write_text works for /tmp (400 read-only rootfs)",
                  "hello-from-write" in r.stdout, f"{r.stdout!r} {r.stderr[:150]}")
        except Exception as e:  # noqa: BLE001
            check("write_text works for /tmp (400 read-only rootfs)", False,
                  f"{type(e).__name__}: {str(e)[:200]}")

        # 4. and into the workspace, which always worked — guard the fallback
        #    did not break the normal path.
        await be.write_text(os.path.join(ws_dir, "in_ws.txt"), "ws-ok\n",
                            fs_write=fsw)
        got = await be.read_text(os.path.join(ws_dir, "in_ws.txt"), fs_read=fsr)
        check("write_text still works for the workspace", "ws-ok" in got, repr(got))

        # 5. Egress must follow the knob in BOTH directions — a fix that just
        #    turns the network on unconditionally would pass a one-sided test.
        r = await be.exec(
            "python -c \"import socket;socket.setdefaulttimeout(8);"
            "socket.create_connection(('pypi.org',443));print('NET_OK')\" 2>&1 | tail -1",
            fs_write=fsw, network=NetworkConfig(), timeout_s=120, cwd=ws_dir)
        reached = "NET_OK" in r.stdout
        if net:
            check("network=1 reaches the outside (DB/API/pip)", reached,
                  " ".join((r.stdout + r.stderr).split())[:200])
        else:
            check("network=0 still denies egress (default stays closed)",
                  not reached, "container had egress with the knob off")

        # 6. With egress, the full provisioning chain must complete — this is
        #    what the analysis env actually does on first use.
        if net:
            r = await be.exec(
                'uv venv --clear --python 3.12 "$HOME/.p312" 2>&1 | tail -1 && '
                '"$HOME/.p312/bin/python" -c "import sys;print(sys.version.split()[0])"',
                fs_write=fsw, network=NetworkConfig(), timeout_s=900, cwd=ws_dir)
            check("uv provisions a 3.12 interpreter end to end",
                  r.exit_code == 0 and "3.12" in r.stdout,
                  " ".join((r.stdout + r.stderr).split())[-220:])
    finally:
        await be.close()
        subprocess.run(["docker", "rm", "-f",
                        f"adk-cc-dockbe-{'net' if net else 'nonet'}"],
                       capture_output=True)
        shutil.rmtree(ws_dir, ignore_errors=True)


def main() -> int:
    if not shutil.which("docker"):
        print("SKIP: docker not installed."); return 0
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP: docker daemon not running."); return 0
    if subprocess.run(["docker", "image", "inspect", "adk-cc-sandbox:latest"],
                      capture_output=True).returncode != 0:
        print("SKIP: adk-cc-sandbox:latest not built."); return 0

    for net in (False, True):
        asyncio.run(_run(net))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
