"""#75: the dataset panel served THROUGH the sandbox backend.

The helpers are exercised REAL: a minimal backend that runs `sh -c` in the
workspace dir and writes bytes to disk — so the heredoc scripts, marker
parsing, ext filtering, delete probe and env snapshot all actually run,
exactly as they would over SSH/docker (same commands, different transport).

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_datasets_backend.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="dsb-desktop-")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


class ShBackend:
    """Executes for real in the workspace dir — the transport is the only
    thing faked, which is the point: the COMMANDS must be portable."""

    name = "sh-test"

    def __init__(self, root: str) -> None:
        self.root = root

    async def exec(self, cmd, *, fs_write=None, network=None, timeout_s=60,
                   cwd=None):  # noqa: ANN001, ANN202
        p = subprocess.run(["sh", "-c", cmd], cwd=cwd or self.root,
                           capture_output=True, text=True, timeout=timeout_s)
        return SimpleNamespace(stdout=p.stdout, stderr=p.stderr,
                               exit_code=p.returncode)

    async def ensure_workspace(self, ws):  # noqa: ANN001, ANN202
        os.makedirs(ws.abs_path, exist_ok=True)

    async def write_bytes(self, path, content, *, fs_write=None):  # noqa: ANN001, ANN202
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def main() -> int:
    from adk_cc.sandbox.workspace import WorkspaceRoot
    from adk_cc.service import datasets as ds
    from adk_cc.service import datasets_backend as dsb

    root = tempfile.mkdtemp(prefix="dsb-ws-")
    ws = WorkspaceRoot(tenant_id="local", session_id="s1", abs_path=root)
    be = ShBackend(root)
    run = asyncio.run

    # ---- helpers, end to end ---------------------------------------------
    check("empty workspace lists []", run(dsb.listing_via(ws, be)) == [])

    row = run(dsb.put_via(ws, be, "sales.csv", b"m,r\n1,2\n"))
    check("put lands the bytes",
          (Path(root) / "data" / "sales.csv").read_bytes() == b"m,r\n1,2\n")
    check("put returns a real listing row",
          row["name"] == "sales.csv" and row["path"] == "data/sales.csv"
          and row["bytes"] == 8 and row["format"] == "csv", row)

    (Path(root) / "data" / "wip.csv.part").write_text("x")
    (Path(root) / "data" / "notes.txt").write_text("x")
    (Path(root) / "data" / "subdir").mkdir()
    rows = run(dsb.listing_via(ws, be))
    check("listing filters partials, unsupported ext, and dirs",
          [r["name"] for r in rows] == ["sales.csv"], rows)

    check("stat hit", (run(dsb.stat_via(ws, be, "sales.csv")) or {}).get("bytes") == 8)
    check("stat miss is None", run(dsb.stat_via(ws, be, "nope.csv")) is None)

    check("remove deletes and reports it", run(dsb.remove_via(ws, be, "sales.csv"))
          and not (Path(root) / "data" / "sales.csv").exists())
    check("remove of an absent dataset is False",
          run(dsb.remove_via(ws, be, "sales.csv")) is False)

    try:
        run(dsb.put_via(ws, be, "evil.py", b"x"))
        check("bad name rejected before any IO", False)
    except ds.DatasetError:
        check("bad name rejected before any IO", True)

    # ---- env status through the same transport ---------------------------
    from adk_cc.sandbox import analysis_env as ae

    for k in ("ADK_CC_ANALYSIS_ENV",):
        os.environ.pop(k, None)
    st = run(ae.status_via(be, ws))
    check("fresh workspace env is 'absent'", st.get("state") == "absent", st)
    envdir = Path(root) / ".adk-cc" / "analysis-env" / "bin"
    envdir.mkdir(parents=True)
    (envdir / "python").write_text("")
    (envdir.parent / ".adk-cc-tiers").write_text("core,viz|abc")
    st = run(ae.status_via(be, ws))
    check("marker + interpreter reads as ready with tiers",
          st.get("state") == "ready" and st.get("tiers") == ["core", "viz"], st)
    check("remote status matches local status on the same workspace",
          st == ae.status(root), (st, ae.status(root)))

    # ---- the routes take the backend path for a 'remote' workspace --------
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import adk_cc.service.desktop_files as df

    root2 = tempfile.mkdtemp(prefix="dsb-ws2-")
    ws2 = WorkspaceRoot(tenant_id="local", session_id="s1",
                        abs_path=root2, remote=True)
    df._dataset_serving_ctx = lambda pid, sid: (
        "this project runs over SSH; its files are on the remote host",
        ws2, ShBackend(root2))

    app = FastAPI()
    df.mount_desktop_dataset_routes(app)
    client = TestClient(app)
    q = "?project_id=p1&session_id=s1"

    r = client.put(f"/desktop/datasets/remote.csv{q}", content=b"a,b\n1,2\n")
    check("PUT delivers through the backend", r.status_code == 200
          and (Path(root2) / "data" / "remote.csv").is_file(), r.text[:200])

    body = client.get(f"/desktop/datasets{q}").json()
    check("listing serves rows, not a refusal",
          [d["name"] for d in body["datasets"]] == ["remote.csv"]
          and "unavailable" not in body, body)
    check("and names the transport", body.get("served_via") == "sh-test", body)

    src = Path(tempfile.mkdtemp(prefix="dsb-src-")) / "picked.parquet"
    src.write_bytes(b"PAR1fake")
    r = client.post(f"/desktop/datasets/from-path{q}", json={"path": str(src)})
    check("from-path reads locally, delivers remotely", r.status_code == 200
          and (Path(root2) / "data" / "picked.parquet").read_bytes() == b"PAR1fake",
          r.text[:200])

    check("profile 404s on a dataset that is not there",
          client.get(f"/desktop/datasets/ghost.csv/profile{q}").status_code == 404)

    r = client.delete(f"/desktop/datasets/remote.csv{q}")
    check("DELETE removes through the backend",
          r.json().get("status") == "deleted"
          and not (Path(root2) / "data" / "remote.csv").exists(), r.text[:200])
    check("second DELETE reports not_found",
          client.delete(f"/desktop/datasets/remote.csv{q}").json().get("status")
          == "not_found")

    env = client.get(f"/desktop/analysis-env{q}").json()
    check("env route serves the remote snapshot", env.get("state") == "absent", env)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
