"""#110 live: background processes INSIDE real containers, both backends.

Drives a real daemon end to end for the whole lifecycle the UI exposes:
start (setsid in the container's PID namespace) → live log tail → liveness →
Stop through a route-rebuilt handle (`for_container`) → dead-container
bookkeeping (removal marks records exited instead of leaving them "running").

Needs a Docker daemon and adk-cc-sandbox:latest; skips cleanly without them.

Run: .venv/bin/python tests/e2e_container_background.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
# NEVER the user's real registry (a test once leaked 21 records into it).
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="cbg-desktop-")
os.environ["ADK_CC_DATA_DIR"] = tempfile.mkdtemp(prefix="cbg-data-")
os.environ["ADK_CC_SANDBOX_IMAGE"] = "adk-cc-sandbox:latest"

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


TICK = "i=0; while true; do echo tick $i; i=$((i+1)); sleep 0.3; done"


async def _local_container() -> None:
    from adk_cc.sandbox.backends.local_container_backend import (
        LocalContainerBackend,
    )
    from adk_cc.sandbox.config import NetworkConfig
    from adk_cc.sandbox.process_registry import get_registry
    from adk_cc.sandbox.workspace import WorkspaceRoot

    print("\n--- LocalContainerBackend ---")
    reg = get_registry()
    sid = f"cbg-{uuid.uuid4().hex[:8]}"
    ws_dir = tempfile.mkdtemp(prefix="cbg-ws-")
    be = LocalContainerBackend(session_id=sid)
    ws = WorkspaceRoot(tenant_id="local", session_id=sid, abs_path=ws_dir)
    await be.ensure_workspace(ws)
    try:
        row = await be.start_background(
            TICK, fs_write=ws.fs_write_config(), network=NetworkConfig(),
            cwd=ws.abs_path, label="ticker", session_key=sid, project_id="p")
        check("starts and reports running",
              row["status"] == "running" and row["pid"], row)
        rec = reg.get(row["id"])
        check("record carries the container name",
              rec is not None and rec.container_name == be._name, rec)

        await asyncio.sleep(1.2)
        log = reg.read_log(row["id"])
        check("log is host-visible and live (identical-path mount)",
              "tick" in log, log[:120])

        await be.sync_background_log(row["id"])
        check("liveness probe keeps it running",
              (reg.get(row["id"]) or rec).status == "running")

        # Stop through the ROUTE'S path: a rebuilt handle from the record.
        handle = LocalContainerBackend.for_container(rec.container_name)
        ok = await handle.terminate_background(row["id"])
        check("terminate through a for_container handle", ok is True)
        check("record is killed", (reg.get(row["id"]) or rec).status == "killed")
        probe = subprocess.run(
            ["docker", "exec", rec.container_name, "sh", "-c",
             f"kill -0 -- -{rec.pgid} 2>/dev/null && echo LIVE || echo DEAD"],
            capture_output=True, text=True, timeout=15)
        check("the process group is really dead in the container",
              "DEAD" in probe.stdout, probe.stdout)

        # A short-lived command: liveness marks it exited, not running-forever.
        row2 = await be.start_background(
            "echo one-shot", fs_write=ws.fs_write_config(),
            network=NetworkConfig(), cwd=ws.abs_path, label="oneshot",
            session_key=sid, project_id="p")
        await asyncio.sleep(1.0)
        await be.sync_background_log(row2["id"])
        check("a finished one-shot is marked exited by the liveness probe",
              (reg.get(row2["id"]) or {}).status == "exited",
              reg.get(row2["id"]))

        # Container removal takes its processes' records along.
        row3 = await be.start_background(
            TICK, fs_write=ws.fs_write_config(), network=NetworkConfig(),
            cwd=ws.abs_path, label="doomed", session_key=sid, project_id="p")
        check("third process running", row3["status"] == "running")
        await be.remove()
        rec3 = reg.get(row3["id"])
        check("container removal marks its records exited",
              rec3 is not None and rec3.status == "exited", rec3)
        check("and says why in the log",
              "container was removed" in reg.read_log(row3["id"]))
    finally:
        await be.remove()
        shutil.rmtree(ws_dir, ignore_errors=True)


async def _docker_backend() -> None:
    from adk_cc.sandbox.backends.docker_backend import DockerBackend
    from adk_cc.sandbox.config import NetworkConfig
    from adk_cc.sandbox.process_registry import get_registry
    from adk_cc.sandbox.workspace import WorkspaceRoot

    print("\n--- DockerBackend ---")
    reg = get_registry()
    sid = f"cbgd-{uuid.uuid4().hex[:8]}"
    ws_dir = tempfile.mkdtemp(prefix="cbgd-ws-")
    be = DockerBackend(session_id=sid)
    ws = WorkspaceRoot(tenant_id="local", session_id=sid, abs_path=ws_dir)
    try:
        await be.ensure_workspace(ws)
        row = await be.start_background(
            TICK, fs_write=ws.fs_write_config(), network=NetworkConfig(),
            cwd=ws.abs_path, label="ticker", session_key=sid, project_id="p")
        check("starts and reports running",
              row["status"] == "running" and row["pid"], row)
        rec = reg.get(row["id"])

        await asyncio.sleep(1.2)
        await be.sync_background_log(row["id"])
        check("log tail is PULLED from the container",
              "tick" in reg.read_log(row["id"]),
              reg.read_log(row["id"])[:120])
        check("still running after the pull",
              (reg.get(row["id"]) or rec).status == "running")

        # end-of-turn close() must NOT remove a container hosting a live
        # background process.
        await be.close()
        insp = subprocess.run(["docker", "ps", "-q", "-f",
                               f"name={rec.container_name}"],
                              capture_output=True, text=True, timeout=15)
        check("close() keeps the container while the process lives",
              bool(insp.stdout.strip()), insp.stdout)

        handle = DockerBackend.for_container(rec.container_name)
        check("terminate through a for_container handle",
              await handle.terminate_background(row["id"]) is True)
        check("record is killed", (reg.get(row["id"]) or rec).status == "killed")

        # With nothing running, close() reaps as before.
        await be._ensure_container()
        await be.close()
        insp = subprocess.run(["docker", "ps", "-aq", "-f",
                               f"name={rec.container_name}"],
                              capture_output=True, text=True, timeout=15)
        check("close() removes the container once the process is stopped",
              not insp.stdout.strip(), insp.stdout)
    finally:
        try:
            await be.close()
        except Exception:  # noqa: BLE001
            pass
        subprocess.run(["docker", "rm", "-f", f"adk-cc-{sid}"],
                       capture_output=True, timeout=30)
        shutil.rmtree(ws_dir, ignore_errors=True)


def main() -> int:
    if not shutil.which("docker"):
        print("SKIP: docker CLI not found")
        return 0
    probe = subprocess.run(["docker", "images", "-q", "adk-cc-sandbox:latest"],
                           capture_output=True, text=True)
    if probe.returncode != 0 or not probe.stdout.strip():
        print("SKIP: adk-cc-sandbox:latest not present (or daemon down)")
        return 0
    asyncio.run(_local_container())
    asyncio.run(_docker_backend())
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
