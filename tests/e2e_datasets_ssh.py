"""#75 live acceptance: the dataset panel served over REAL SSH.

Mirrors `desktop_backend_factory`'s SSH arm exactly (shared per-host
transport, SshBackend, wire_runtime_env), then drives the same helpers the
routes use: put → list → stat → env status → ensure_env (REAL uv provision
on the box, first run is slow) → REAL pandas profile → delete.

Gated on env, like acceptance_ssh_device.py:

    ADK_CC_ACCEPT_SSH_HOST   e.g. "mybox" or "user@192.168.0.42"
    ADK_CC_ACCEPT_SSH_PATH   absolute scratch path the test MAY create and
                             write under

The provisioned analysis env is LEFT on the box (`<path>/.adk-cc/`) so the
run is inspectable and the second run is warm.

Run:
  ADK_CC_ACCEPT_SSH_HOST=... ADK_CC_ACCEPT_SSH_PATH=/abs/scratch \
    .venv/bin/python tests/e2e_datasets_ssh.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_HOST = os.environ.get("ADK_CC_ACCEPT_SSH_HOST") or ""
_PATH = (os.environ.get("ADK_CC_ACCEPT_SSH_PATH") or "").rstrip("/")
if not _HOST or not _PATH.startswith("/"):
    print("[SKIP] set ADK_CC_ACCEPT_SSH_HOST and ADK_CC_ACCEPT_SSH_PATH (absolute)")
    sys.exit(0)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


async def _run() -> None:
    from adk_cc.sandbox import wire_runtime_env
    from adk_cc.sandbox import analysis_env as ae
    from adk_cc.sandbox.backends.ssh_backend import SshBackend
    from adk_cc.sandbox.config import NetworkConfig
    from adk_cc.sandbox.ssh_transport import get_transport
    from adk_cc.sandbox.workspace import WorkspaceRoot
    from adk_cc.service import datasets as ds
    from adk_cc.service import datasets_backend as dsb

    transport = get_transport(_HOST)
    be = SshBackend(session_id="dsssh", tenant_id="local",
                    transport=transport, workspace_path=_PATH)
    wire_runtime_env(be, tenant_id="local", user_id="local")
    ws = WorkspaceRoot(tenant_id="local", session_id="dsssh",
                       abs_path=_PATH, remote=True)

    await be.ensure_workspace(ws)

    csv = b"month,revenue\n2026-01,1200\n2026-02,1500\n2026-03,900\n"
    t0 = time.monotonic()
    row = await dsb.put_via(ws, be, "sales.csv", csv)
    check("put lands over ssh", row["name"] == "sales.csv"
          and row["bytes"] == len(csv), row)

    t1 = time.monotonic()
    rows = await dsb.listing_via(ws, be)
    t2 = time.monotonic()
    check("listing sees it on the remote", "sales.csv" in
          [r["name"] for r in rows], rows)
    check("stat matches",
          ((await dsb.stat_via(ws, be, "sales.csv")) or {}).get("bytes")
          == len(csv))
    print(f"  [info] warm ops: put {t1 - t0:.2f}s, list {t2 - t1:.2f}s")

    st = await ae.status_via(be, ws)
    check("env status before provisioning is absent/ready (not unknown)",
          st.get("state") in ("absent", "ready"), st)

    print("  [info] provisioning core tier on the box (first run is slow)…")
    t3 = time.monotonic()
    try:
        env = await ae.ensure_env(be, ws, tiers=("core",))
    except Exception as e:  # noqa: BLE001
        check("ensure_env provisions over ssh", False, f"{type(e).__name__}: {e}")
        return
    print(f"  [info] ensure_env: {time.monotonic() - t3:.1f}s → {env.python}")

    cmd = ds.profile_command("data/sales.csv", python=env.python)
    res = await be.exec(cmd, fs_write=ws.fs_write_config(),
                        network=NetworkConfig(), timeout_s=240, cwd=_PATH)
    prof = ds.parse_profile(getattr(res, "stdout", "") or "")
    check("profile ran on the box", prof is not None,
          (getattr(res, "stderr", "") or "")[-300:])
    if prof:
        cols = {c["name"] for c in prof.get("columns", [])}
        check("profile is real: columns + exact rows",
              cols == {"month", "revenue"} and prof.get("rows") == 3
              and prof.get("rows_exact") is True, prof)

    st = await ae.status_via(be, ws)
    check("env status now READY with the provisioned tier",
          st.get("state") == "ready" and "core" in (st.get("tiers") or []), st)
    check("remote status agrees with what ensure_env built",
          st.get("python") == env.python, (st, env.python))

    check("remove deletes it there",
          await dsb.remove_via(ws, be, "sales.csv") is True)
    check("second remove reports absent",
          await dsb.remove_via(ws, be, "sales.csv") is False)


def main() -> int:
    asyncio.run(_run())
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
