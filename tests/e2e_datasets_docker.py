"""#75 live acceptance: the dataset panel served through a REAL DockerBackend.

Drives the exact deployed configuration: `adk-cc-sandbox:latest` with the
image's own interpreter (`ADK_CC_ANALYSIS_ENV=/usr/local/bin/python`, ships
pandas per #117), a real container, a real `data/` inside its workspace.
Covers put → list → stat → env status → a REAL pandas profile → delete —
the full panel surface the routes now serve for container workspaces.

Needs a Docker daemon and adk-cc-sandbox:latest; skips cleanly without them.

Run: .venv/bin/python tests/e2e_datasets_docker.py
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
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


async def _run() -> None:
    from adk_cc.sandbox.backends.docker_backend import DockerBackend
    from adk_cc.sandbox.workspace import WorkspaceRoot
    from adk_cc.sandbox import analysis_env as ae
    from adk_cc.service import datasets as ds
    from adk_cc.service import datasets_backend as dsb

    ws_dir = tempfile.mkdtemp(prefix="dsdock-")
    ws = WorkspaceRoot(tenant_id="local", session_id="dsdock",
                       abs_path=ws_dir)
    ws_dir = ws.abs_path
    be = DockerBackend(session_id="dsdock", tenant_id="local",
                       workspace_abs_path=ws_dir)
    try:
        await be.ensure_workspace(ws)

        csv = b"month,revenue\n2026-01,1200\n2026-02,1500\n2026-03,900\n"
        row = await dsb.put_via(ws, be, "sales.csv", csv)
        check("put lands through the docker backend",
              row["name"] == "sales.csv" and row["bytes"] == len(csv), row)

        rows = await dsb.listing_via(ws, be)
        check("listing sees it from inside the container",
              [r["name"] for r in rows] == ["sales.csv"], rows)

        check("stat matches",
              ((await dsb.stat_via(ws, be, "sales.csv")) or {}).get("bytes")
              == len(csv))

        # Env status: the deployed config supplies the image interpreter.
        os.environ["ADK_CC_ANALYSIS_ENV"] = "/usr/local/bin/python"
        try:
            st = await ae.status_via(be, ws)
            check("env status reports the operator interpreter",
                  st.get("state") == "external"
                  and st.get("python") == "/usr/local/bin/python", st)

            # The REAL profile: pandas in the image reads the csv we delivered.
            env = await ae.ensure_env(be, ws, tiers=("core",))
            cmd = ds.profile_command("data/sales.csv", python=env.python)
            from adk_cc.sandbox.config import NetworkConfig

            res = await be.exec(cmd, fs_write=ws.fs_write_config(),
                                network=NetworkConfig(), timeout_s=240,
                                cwd=ws_dir)
            prof = ds.parse_profile(getattr(res, "stdout", "") or "")
            check("profile ran in the container", prof is not None,
                  (getattr(res, "stderr", "") or "")[-300:])
            if prof:
                cols = {c["name"] for c in prof.get("columns", [])}
                check("profile is real: columns + exact rows",
                      cols == {"month", "revenue"} and prof.get("rows") == 3
                      and prof.get("rows_exact") is True, prof)
        finally:
            os.environ.pop("ADK_CC_ANALYSIS_ENV", None)

        # Unset interpreter → the snapshot actually runs in the container.
        st = await ae.status_via(be, ws)
        check("auto-mode snapshot runs inside the container",
              st.get("state") in ("absent", "ready"), st)

        check("remove deletes it there",
              await dsb.remove_via(ws, be, "sales.csv") is True)
        check("and the listing agrees", await dsb.listing_via(ws, be) == [])
    finally:
        try:
            await be.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(ws_dir, ignore_errors=True)


def main() -> int:
    if not shutil.which("docker"):
        print("SKIP: docker CLI not found")
        return 0
    # NOT `docker image inspect`: under the containerd image store that can
    # fail by NAME while the tag resolves fine for `docker run` (observed
    # live on Docker Desktop 28.x).
    probe = subprocess.run(["docker", "images", "-q", "adk-cc-sandbox:latest"],
                           capture_output=True, text=True)
    if probe.returncode != 0 or not probe.stdout.strip():
        print("SKIP: adk-cc-sandbox:latest not present (or daemon down)")
        return 0
    asyncio.run(_run())
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
