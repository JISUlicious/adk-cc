"""Live e2e for the sandbox v2 items: live output streaming (exec_stream) and
container teardown (delete-session hook + idle reaper). Real Docker/Podman; SKIPS
with no runtime. Only benign commands run.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_container_v2.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.sandbox.backends.container_runtime import detect_runtime
from adk_cc.sandbox.backends.local_container_backend import (
    LocalContainerBackend, reap_idle, remove_session_container, sweep_orphans, _LAST_ACTIVE,
)
from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig
from adk_cc.sandbox.workspace import WorkspaceRoot

_passed = _failed = 0


def check(name: str, ok: bool) -> None:
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    _passed += ok
    _failed += not ok


def _ws(p: str) -> WorkspaceRoot:
    return WorkspaceRoot(tenant_id="local", session_id="v2", abs_path=p)


def _exists(rt, name: str) -> bool:
    return bool(subprocess.run([rt.cli_path, "ps", "-aq", "--filter", f"name=^{name}$"],
                               capture_output=True, text=True, timeout=15).stdout.strip())


async def run(rt) -> None:
    proj = tempfile.mkdtemp(prefix="v2e2e-")

    # --- item 3: live streaming ---------------------------------------------
    b = LocalContainerBackend(session_id="v2-stream", runtime=rt, network_enabled=False)
    await b.ensure_workspace(_ws(proj))
    kinds, outs, res = [], [], None
    async for ch in b.exec_stream(
            "for i in 1 2 3; do echo line$i; sleep 0.05; done; echo boom >&2; exit 7",
            fs_write=FsWriteConfig(), network=NetworkConfig(), timeout_s=20, cwd=proj):
        kinds.append(ch.kind)
        if ch.kind == "stdout":
            outs.append(ch.data.strip())
        if ch.kind == "result":
            res = ch.result
    check("exec_stream yields live stdout chunks then one result",
          outs == ["line1", "line2", "line3"] and kinds[-1] == "result" and kinds.count("result") == 1)
    check("streamed result carries the real exit code + stderr",
          res is not None and res.exit_code == 7 and "boom" in res.stderr)

    # streamed timeout is reported (in-container `timeout` → 124)
    tkinds = []
    async for ch in b.exec_stream("sleep 30", fs_write=FsWriteConfig(),
                                  network=NetworkConfig(), timeout_s=2, cwd=proj):
        tkinds.append(ch)
    tres = tkinds[-1].result
    check("exec_stream reports a timeout", tres.timed_out and tres.exit_code != 0)

    # --- item 1: deterministic delete hook ----------------------------------
    check("session container exists before teardown", _exists(rt, b._name))
    remove_session_container("v2-stream", rt)
    check("remove_session_container reaps it", not _exists(rt, b._name))
    await b.close()

    # --- item 1: idle reaper -------------------------------------------------
    b2 = LocalContainerBackend(session_id="v2-idle", runtime=rt, network_enabled=False)
    await b2.ensure_workspace(_ws(proj))
    await b2.exec("true", fs_write=FsWriteConfig(), network=NetworkConfig(), timeout_s=10, cwd=proj)
    _LAST_ACTIVE[b2._name] = time.monotonic() - 10_000  # pretend long-idle
    reaped = await asyncio.to_thread(reap_idle, 60, rt)
    check("reap_idle removes an idle container", reaped >= 1 and not _exists(rt, b2._name))

    # idle reaper leaves an ACTIVE container alone
    b3 = LocalContainerBackend(session_id="v2-active", runtime=rt, network_enabled=False)
    await b3.ensure_workspace(_ws(proj))
    await b3.exec("true", fs_write=FsWriteConfig(), network=NetworkConfig(), timeout_s=10, cwd=proj)
    reaped2 = await asyncio.to_thread(reap_idle, 60, rt)  # b3 was just active
    check("reap_idle keeps a recently-active container", not reaped2 and _exists(rt, b3._name))
    await b3.remove()


def main() -> int:
    rt = detect_runtime()
    if rt is None:
        print("[SKIP] no container runtime")
        return 0
    print(f"sandbox v2 e2e via {rt.name} {rt.version}:")
    try:
        asyncio.run(run(rt))
    finally:
        sweep_orphans(rt)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
