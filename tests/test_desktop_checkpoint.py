"""Desktop checkpoint/undo: shadow-git snapshots + restore.

Pins the load-bearing behavior of the undo net:
  - exactly ONE snapshot per turn, no matter how many mutating tool calls;
  - NO snapshot in web mode, for the no-project scratch dir, or when disabled;
  - restore reverts modified AND newly-created files, while the user's REAL
    .git (HEAD / branch / reflog) is left completely untouched.

Model-free / server-free — drives the CheckpointPlugin's before_tool_callback
directly with a fake ToolContext, plus the desktop_checkpoint API.

Run: `.venv/bin/python tests/test_desktop_checkpoint.py`
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_TMP = tempfile.mkdtemp(prefix="adk-cc-ckpt-")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP

from adk_cc.plugins.checkpoint import CheckpointPlugin
from adk_cc.service import desktop_checkpoint as dc
from adk_cc.service.desktop_routes import save_projects


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _make_repo(name: str) -> str:
    repo = os.path.join(_TMP, name)
    os.makedirs(repo, exist_ok=True)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("v1\n")
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], repo)
    return repo


def _register(project_id: str, repo: str) -> None:
    items = []
    try:
        from adk_cc.service.desktop_routes import load_projects

        items = load_projects()
    except Exception:
        items = []
    items = [it for it in items if it.get("id") != project_id]
    items.append({"id": project_id, "name": project_id, "repo_path": repo})
    save_projects(items)


def _ctx(*, project_id: str, session_id: str, workspace_path: str, inv: str) -> SimpleNamespace:
    ws = SimpleNamespace(abs_path=workspace_path)
    return SimpleNamespace(
        state={"temp:sandbox_workspace": ws},
        user_id=project_id,
        session=SimpleNamespace(id=session_id),
        invocation_id=inv,
    )


def _fire(plugin: CheckpointPlugin, tool_name: str, ctx: SimpleNamespace) -> None:
    tool = SimpleNamespace(name=tool_name)
    asyncio.run(plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=ctx))


def test_snapshot_once_per_turn() -> None:
    repo = _make_repo("proj_once")
    _register("proj_once", repo)
    plugin = CheckpointPlugin()
    ctx = _ctx(project_id="proj_once", session_id="s1", workspace_path=repo, inv="inv-1")

    # 3 mutating calls in ONE turn, mutating the tree between each — the once-per-
    # turn guard must still yield exactly ONE checkpoint.
    _fire(plugin, "write_file", ctx)
    Path(repo, "README.md").write_text("v2\n")
    _fire(plugin, "run_bash", ctx)
    Path(repo, "new.txt").write_text("x")
    _fire(plugin, "edit_file", ctx)

    cps = dc.list_checkpoints("proj_once", "s1")
    assert len(cps) == 1, f"expected 1 checkpoint for the turn, got {len(cps)}"
    print("OK test_snapshot_once_per_turn")


def test_new_turn_snapshots_again() -> None:
    repo = _make_repo("proj_two")
    _register("proj_two", repo)
    plugin = CheckpointPlugin()
    _fire(plugin, "write_file", _ctx(project_id="proj_two", session_id="s1", workspace_path=repo, inv="inv-A"))
    Path(repo, "README.md").write_text("v2\n")  # change so the next snapshot commits
    _fire(plugin, "write_file", _ctx(project_id="proj_two", session_id="s1", workspace_path=repo, inv="inv-B"))
    cps = dc.list_checkpoints("proj_two", "s1")
    assert len(cps) == 2, f"a second turn should add a checkpoint; got {len(cps)}"
    print("OK test_new_turn_snapshots_again")


def test_no_snapshot_in_web_mode() -> None:
    repo = _make_repo("proj_web")
    _register("proj_web", repo)
    plugin = CheckpointPlugin()
    os.environ.pop("ADK_CC_DESKTOP", None)  # web mode
    try:
        _fire(plugin, "write_file", _ctx(project_id="proj_web", session_id="s1", workspace_path=repo, inv="inv-w"))
    finally:
        os.environ["ADK_CC_DESKTOP"] = "1"
    assert dc.list_checkpoints("proj_web", "s1") == [], "web mode must not snapshot"
    print("OK test_no_snapshot_in_web_mode")


def test_no_snapshot_for_scratch() -> None:
    # No bound project (user_id 'local') + a scratch workspace → nothing to undo.
    scratch = os.path.join(_TMP, "scratch_dir")
    os.makedirs(scratch, exist_ok=True)
    plugin = CheckpointPlugin()
    _fire(plugin, "write_file", _ctx(project_id="local", session_id="s1", workspace_path=scratch, inv="inv-s"))
    assert dc.list_checkpoints("local", "s1") == [], "scratch dir must not snapshot"
    print("OK test_no_snapshot_for_scratch")


def test_disabled_by_env() -> None:
    repo = _make_repo("proj_off")
    _register("proj_off", repo)
    plugin = CheckpointPlugin()
    os.environ["ADK_CC_CHECKPOINT"] = "0"
    try:
        _fire(plugin, "write_file", _ctx(project_id="proj_off", session_id="s1", workspace_path=repo, inv="inv-o"))
    finally:
        os.environ.pop("ADK_CC_CHECKPOINT", None)
    assert dc.list_checkpoints("proj_off", "s1") == [], "ADK_CC_CHECKPOINT=0 must disable"
    print("OK test_disabled_by_env")


def test_restore_reverts_and_real_git_untouched() -> None:
    repo = _make_repo("proj_restore")
    _register("proj_restore", repo)

    # Record the user's REAL git identity BEFORE any checkpoint activity.
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    branch_before = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    reflog_before = _git(["reflog", "--format=%H"], repo).stdout

    plugin = CheckpointPlugin()
    # Turn 1: snapshot the pristine tree, THEN the turn mutates it.
    _fire(plugin, "write_file", _ctx(project_id="proj_restore", session_id="s1", workspace_path=repo, inv="inv-1"))
    Path(repo, "README.md").write_text("MUTATED\n")     # modify tracked file
    Path(repo, "created.txt").write_text("new turn file")  # create a new file

    # Undo the last turn (default → most recent checkpoint).
    res = dc.restore("proj_restore", "s1", repo)
    assert res["status"] == "ok", res

    # Modified file reverted; newly-created file removed.
    assert Path(repo, "README.md").read_text() == "v1\n", "README not reverted"
    assert not Path(repo, "created.txt").exists(), "turn-created file not removed"

    # The user's REAL git is byte-for-byte untouched: same HEAD, branch, reflog.
    assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before, "real HEAD moved"
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip() == branch_before, "real branch changed"
    assert _git(["reflog", "--format=%H"], repo).stdout == reflog_before, "real reflog changed"
    # And the working tree is clean relative to the real repo (README back to HEAD).
    assert _git(["status", "--porcelain"], repo).stdout.strip() == "", "real repo not clean after undo"
    print("OK test_restore_reverts_and_real_git_untouched")


def test_checkpoint_routes() -> None:
    # The HTTP wrapper: validation (400/404) + wiring to list/restore. Mounts the
    # real desktop routes on a bare app (the e2e covers the git logic; this covers
    # the route layer).
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception as e:  # pragma: no cover
        print(f"SKIP test_checkpoint_routes (no fastapi/httpx): {e}")
        return

    from adk_cc.service.desktop_routes import mount_desktop_routes

    repo = _make_repo("proj_routes")
    _register("proj_routes", repo)
    dc.snapshot("proj_routes", "s1", repo, reason="seed")  # one checkpoint to find

    app = FastAPI()
    mount_desktop_routes(app)
    client = TestClient(app)

    r = client.get("/desktop/checkpoint/list", params={"project_id": "proj_routes", "session_id": "s1"})
    assert r.status_code == 200, r.text
    assert len(r.json()["checkpoints"]) == 1, r.json()

    # missing param → 400
    assert client.get("/desktop/checkpoint/list", params={"project_id": "proj_routes"}).status_code == 400
    # unknown project → 404
    assert client.get(
        "/desktop/checkpoint/list", params={"project_id": "nope", "session_id": "s1"}
    ).status_code == 404

    Path(repo, "README.md").write_text("routes-mutated\n")
    r = client.post(
        "/desktop/checkpoint/restore",
        json={"project_id": "proj_routes", "session_id": "s1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok", r.json()
    assert Path(repo, "README.md").read_text() == "v1\n", "route restore did not revert"
    assert client.post("/desktop/checkpoint/restore", json={"project_id": "proj_routes"}).status_code == 400
    print("OK test_checkpoint_routes")


def test_no_file_change_turn_gets_its_own_checkpoint() -> None:
    # The reported bug: a turn that changes NO files (e.g. `ls -la`) then a
    # file-change turn must produce TWO checkpoints — one per turn — each with its
    # own invocation_id. Otherwise the file-change turn's checkpoint dedups into the
    # ls turn's (same file state) and rewinding jumps back to the wrong/earlier
    # point (the user saw it rewind to the empty start of the conversation).
    repo = _make_repo("proj_ls")
    _register("proj_ls", repo)
    dc.snapshot("proj_ls", "s1", repo, reason="run_bash", invocation_id="inv-1")   # ls turn (no change)
    dc.snapshot("proj_ls", "s1", repo, reason="write_file", invocation_id="inv-2")  # before a file change
    cps = dc.list_checkpoints("proj_ls", "s1")
    assert len(cps) == 2, f"expected 2 checkpoints (one per turn), got {len(cps)}"
    assert [c["invocation_id"] for c in cps] == ["inv-2", "inv-1"], cps
    # idempotent: the same invocation again does not duplicate.
    dc.snapshot("proj_ls", "s1", repo, reason="write_file", invocation_id="inv-2")
    assert len(dc.list_checkpoints("proj_ls", "s1")) == 2

    # Both checkpoints share the same git sha (ls changed nothing) — restore must
    # resolve by the unique id, landing on the RIGHT invocation, not the first
    # entry with that sha.
    cps = dc.list_checkpoints("proj_ls", "s1")  # most-recent-first: [edit(inv-2), ls(inv-1)]
    assert cps[0]["sha"] == cps[1]["sha"], "precondition: shared sha"
    assert cps[0]["id"] != cps[1]["id"], "ids must be unique"
    res = dc.restore("proj_ls", "s1", repo, checkpoint_id=cps[0]["id"])
    assert res["status"] == "ok" and res["invocation_id"] == "inv-2", res
    res2 = dc.restore("proj_ls", "s1", repo, checkpoint_id=cps[1]["id"])
    assert res2["status"] == "ok" and res2["invocation_id"] == "inv-1", res2
    print("OK test_no_file_change_turn_gets_its_own_checkpoint")


def test_rewind_drops_rewound_checkpoints_from_history() -> None:
    # After rewinding, the checkpoints for the undone turns must leave the history
    # (so the menu only lists surviving turns + repeated undo steps back).
    repo = _make_repo("proj_hist")
    _register("proj_hist", repo)
    # 3 file-changing turns (snapshot is taken BEFORE each turn's change).
    dc.snapshot("proj_hist", "s1", repo, reason="write_file", invocation_id="inv-1")
    Path(repo, "a.txt").write_text("a")
    dc.snapshot("proj_hist", "s1", repo, reason="write_file", invocation_id="inv-2")
    Path(repo, "b.txt").write_text("b")
    dc.snapshot("proj_hist", "s1", repo, reason="write_file", invocation_id="inv-3")
    Path(repo, "c.txt").write_text("c")
    assert [c["invocation_id"] for c in dc.list_checkpoints("proj_hist", "s1")] == ["inv-3", "inv-2", "inv-1"]

    # Rewind to inv-2's checkpoint (undo turns 2 & 3) → history keeps only inv-1.
    c2 = next(c for c in dc.list_checkpoints("proj_hist", "s1") if c["invocation_id"] == "inv-2")
    assert dc.restore("proj_hist", "s1", repo, checkpoint_id=c2["id"])["status"] == "ok"
    assert [c["invocation_id"] for c in dc.list_checkpoints("proj_hist", "s1")] == ["inv-1"]
    # Files created in the undone turns are gone; the earlier one stays.
    assert Path(repo, "a.txt").exists()
    assert not Path(repo, "b.txt").exists() and not Path(repo, "c.txt").exists()
    print("OK test_rewind_drops_rewound_checkpoints_from_history")


def main() -> None:
    test_snapshot_once_per_turn()
    test_new_turn_snapshots_again()
    test_no_file_change_turn_gets_its_own_checkpoint()
    test_rewind_drops_rewound_checkpoints_from_history()
    test_no_snapshot_in_web_mode()
    test_no_snapshot_for_scratch()
    test_disabled_by_env()
    test_restore_reverts_and_real_git_untouched()
    test_checkpoint_routes()
    print("\nall desktop checkpoint tests passed")


if __name__ == "__main__":
    main()
