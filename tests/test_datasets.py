"""W5: get a dataset into the workspace, safely.

Unit-tests the validation (the part with teeth), then drives the real desktop
routes with a TestClient — the thing that matters is that a dataset lands where
the AGENT reads, which on desktop is the bound project root, not some
server-side upload area.

Run: uv run python tests/test_datasets.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ["ADK_CC_DESKTOP"] = "1"
# The serving-ctx fork reads the desktop project registry — keep the test
# away from the user's real ~/.adk-cc-desktop.
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="ds-desktop-")

_ROOT = tempfile.mkdtemp(prefix="ds-ws-")

from adk_cc.service import datasets as ds  # noqa: E402


def test_name_validation() -> None:
    assert ds.check_name("sales.csv") == "sales.csv"
    assert ds.check_name("2026 Q1 export.parquet")
    for bad in ("../escape.csv", "/etc/passwd.csv", ".hidden.csv",
                "sub/dir.csv", "notes.txt", "script.py", ""):
        try:
            ds.check_name(bad)
        except ds.DatasetError:
            continue
        raise AssertionError(f"accepted unsafe/unsupported name: {bad!r}")
    print("OK name_validation")


def test_compound_extensions_resolve_longest_first() -> None:
    """`.csv.gz` must not be read as `.gz` (unsupported) — or as `.csv`."""
    assert ds.lower_ext("export.csv.gz") == ".csv.gz"
    assert ds.lower_ext("export.CSV") == ".csv"
    assert ds.lower_ext("archive.zip") is None
    print("OK compound_extensions_resolve_longest_first")


def test_size_cap_message_is_actionable() -> None:
    os.environ["ADK_CC_DATASET_UPLOAD_MAX_MB"] = "1"
    try:
        ds.check_size(500)
        try:
            ds.check_size(5 * 1024 * 1024)
        except ds.DatasetError as e:
            msg = str(e)
            assert "5.0MB" in msg and "1MB limit" in msg, msg
            assert "parquet" in msg and "ADK_CC_DATASET_UPLOAD_MAX_MB" in msg, msg
        else:
            raise AssertionError("oversized dataset was accepted")
    finally:
        os.environ.pop("ADK_CC_DATASET_UPLOAD_MAX_MB", None)
    print("OK size_cap_message_is_actionable")


def test_target_path_cannot_escape() -> None:
    root = Path(_ROOT)
    good = ds.target_path(root, "a.csv")
    assert good.parent == (root / ds.DATA_DIR).resolve(), good
    for bad in ("../../a.csv", "x/../../a.csv"):
        try:
            ds.target_path(root, bad)
        except ds.DatasetError:
            continue
        raise AssertionError(f"escaped the data dir: {bad}")
    print("OK target_path_cannot_escape")


def test_routes_land_the_file_in_the_project(tmp=None) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import adk_cc.service.desktop_files as df

    # A project whose workspace is a real directory the agent would read.
    project = Path(tempfile.mkdtemp(prefix="ds-proj-"))
    df._resolve_within = lambda pid, sid, rel: (project / rel).resolve() if rel else project

    app = FastAPI()
    df.mount_desktop_dataset_routes(app)
    client = TestClient(app)
    q = "?project_id=p1&session_id=s1"

    src = Path(tempfile.mkdtemp(prefix="ds-src-")) / "sales.csv"
    src.write_text("month,revenue\n2026-01,1200\n2026-02,1500\n")

    r = client.post(f"/desktop/datasets/from-path{q}", json={"path": str(src)})
    assert r.status_code == 200, r.text
    row = r.json()["dataset"]
    assert row["name"] == "sales.csv" and row["format"] == "csv", row
    landed = project / ds.DATA_DIR / "sales.csv"
    assert landed.is_file(), "dataset did not land in the project workspace"
    assert landed.read_text() == src.read_text(), "contents differ"
    print("OK ingest_from_local_path")

    r = client.get(f"/desktop/datasets{q}")
    body = r.json()
    assert [d["name"] for d in body["datasets"]] == ["sales.csv"], body
    assert body["location"] == "data" and ".parquet" in body["supported"], body
    print("OK listing_reports_location_and_supported_formats")

    r = client.put(f"/desktop/datasets/upload.jsonl{q}", content=b'{"a":1}\n{"a":2}\n')
    assert r.status_code == 200, r.text
    assert (project / "data" / "upload.jsonl").is_file()
    # a half-written .part must never appear in the listing
    (project / "data" / "wip.csv.part").write_text("x")
    names = [d["name"] for d in client.get(f"/desktop/datasets{q}").json()["datasets"]]
    assert names == ["sales.csv", "upload.jsonl"], names
    print("OK upload_and_partials_hidden")

    assert client.put(f"/desktop/datasets/evil.py{q}", content=b"x").status_code == 400
    assert client.put(f"/desktop/datasets/..%2Fescape.csv{q}", content=b"x").status_code in (400, 404)
    assert client.put(f"/desktop/datasets/empty.csv{q}", content=b"").status_code == 400
    print("OK rejects_unsupported_unsafe_and_empty")

    assert client.delete(f"/desktop/datasets/sales.csv{q}").json()["status"] == "deleted"
    assert not landed.exists()
    assert client.delete(f"/desktop/datasets/sales.csv{q}").json()["status"] == "not_found"
    print("OK delete")

    # project_id/session_id are required — a dataset with no workspace has
    # nowhere correct to go.
    assert client.get("/desktop/datasets").status_code == 400
    print("OK workspace_is_required")


def main() -> None:
    test_name_validation()
    test_compound_extensions_resolve_longest_first()
    test_size_cap_message_is_actionable()
    test_target_path_cannot_escape()
    test_routes_land_the_file_in_the_project()
    test_non_local_workspace_is_reported_not_guessed()
    print("\nall dataset-ingestion tests passed")




def test_non_local_workspace_is_reported_not_guessed() -> None:
    """A workspace the server cannot REACH (SSH host down, dead container)
    must say so. #75 made the routes serve remote workspaces through the
    backend; when that backend cannot answer, an empty listing would read as
    "no datasets" and "env not built" — the worst kind of wrong."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from adk_cc.sandbox.workspace import WorkspaceRoot
    import adk_cc.service.desktop_files as df

    class _DeadBackend:
        name = "ssh"

        async def exec(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("host unreachable")

        async def ensure_workspace(self, ws):  # noqa: ANN001, ANN202
            raise RuntimeError("host unreachable")

        async def write_bytes(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("host unreachable")

    ws = WorkspaceRoot(tenant_id="local", session_id="s1",
                       abs_path="/srv/app", remote=True)
    df._dataset_serving_ctx = lambda pid, sid: (
        "this project runs over SSH; its files are on the remote host",
        ws, _DeadBackend())

    app = FastAPI()
    df.mount_desktop_dataset_routes(app)
    client = TestClient(app)
    q = "?project_id=p1&session_id=s1"

    body = client.get(f"/desktop/datasets{q}").json()
    assert body["datasets"] == [] and "SSH" in body.get("unavailable", ""), body
    env = client.get(f"/desktop/analysis-env{q}").json()
    assert env["state"] == "unknown" and "SSH" in env["detail"], env
    src = Path(tempfile.mkdtemp(prefix="ds-src2-")) / "real.csv"
    src.write_text("a,b\n1,2\n")
    for call in (
        lambda: client.post(f"/desktop/datasets/from-path{q}",
                            json={"path": str(src)}),
        lambda: client.put(f"/desktop/datasets/a.csv{q}", content=b"x"),
        lambda: client.delete(f"/desktop/datasets/a.csv{q}"),
        lambda: client.get(f"/desktop/datasets/a.csv/profile{q}"),
    ):
        r = call()
        assert r.status_code == 409, (r.status_code, r.text[:120])
    print("OK non_local_workspace_is_reported_not_guessed")


if __name__ == "__main__":
    main()
