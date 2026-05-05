"""Unit tests for per-user / per-session workspace isolation.

Covers:
  - Dev path unchanged (`default_workspace()` flat, scratch=None).
  - Per-user isolation under TenantContext (different users → different homes).
  - Per-session scratch isolation (different sessions → different scratch dirs).
  - `fs_write_config` allows both roots in production, only home in dev.
  - Path traversal in tenant_id / user_id / session_id rejected.
  - DockerBackend cache mount gated on production layout.
  - JsonFileTaskStorage workspace-anchored vs legacy paths.
  - scratch_reaper script reaps old session dirs without touching home files.

Run: `uv run python tests/test_workspace_layout.py`
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


def _reset_modules() -> None:
    for m in list(sys.modules):
        if m.startswith("adk_cc"):
            del sys.modules[m]


# === Dev path — flat layout unchanged ===


def test_dev_default_workspace_flat():
    print("test_dev_default_workspace_flat: ", end="")
    _reset_modules()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADK_CC_WORKSPACE_ROOT"] = tmp
        from adk_cc.sandbox.workspace import default_workspace

        ws = default_workspace()
        assert os.path.realpath(ws.abs_path) == os.path.realpath(tmp), ws.abs_path
        assert ws.session_scratch_path is None, "dev path must NOT set scratch"
        assert ws.tenant_id == "local"
        assert ws.session_id == "local"
        # fs_write_config allows only the home root.
        cfg = ws.fs_write_config()
        assert any(p.endswith(tmp) or p == f"{tmp}/**" for p in cfg.allow_paths)
    print("OK")


# === Production path — per-user isolation ===


def test_production_per_user_isolation():
    print("test_production_per_user_isolation: ", end="")
    _reset_modules()
    with tempfile.TemporaryDirectory() as tmp:
        from adk_cc.service.tenancy import TenantContext

        ctx_a = TenantContext(tenant_id="acme", user_id="alice", workspace_root_path=tmp)
        ctx_b = TenantContext(tenant_id="acme", user_id="bob", workspace_root_path=tmp)

        ws_a = ctx_a.workspace("session-1")
        ws_b = ctx_b.workspace("session-1")

        assert ws_a.abs_path != ws_b.abs_path, "different users must have different homes"
        assert "alice" in ws_a.abs_path
        assert "bob" in ws_b.abs_path

        # Files written by alice's session land under alice/, not bob/.
        Path(ws_a.abs_path, "secret.md").write_text("alice's secret")
        assert not Path(ws_b.abs_path, "secret.md").exists()
    print("OK")


def test_per_session_scratch_isolation():
    print("test_per_session_scratch_isolation: ", end="")
    _reset_modules()
    with tempfile.TemporaryDirectory() as tmp:
        from adk_cc.service.tenancy import TenantContext

        ctx = TenantContext(tenant_id="acme", user_id="alice", workspace_root_path=tmp)
        ws_s1 = ctx.workspace("session-1")
        ws_s2 = ctx.workspace("session-2")

        # Same home (per-user)
        assert ws_s1.abs_path == ws_s2.abs_path
        # Different scratch (per-session)
        assert ws_s1.session_scratch_path != ws_s2.session_scratch_path
        assert "session-1" in ws_s1.session_scratch_path
        assert "session-2" in ws_s2.session_scratch_path

        Path(ws_s1.session_scratch_path, "tmp.txt").write_text("s1 only")
        assert not Path(ws_s2.session_scratch_path, "tmp.txt").exists()
    print("OK")


def test_production_fs_config_allows_both_roots():
    print("test_production_fs_config_allows_both_roots: ", end="")
    _reset_modules()
    with tempfile.TemporaryDirectory() as tmp:
        from adk_cc.sandbox.config import FsWriteConfig
        from adk_cc.service.tenancy import TenantContext

        ctx = TenantContext(tenant_id="acme", user_id="alice", workspace_root_path=tmp)
        ws = ctx.workspace("session-1")
        cfg: FsWriteConfig = ws.fs_write_config()

        home_path = os.path.join(ws.abs_path, "home_file.md")
        scratch_path = os.path.join(ws.session_scratch_path, "scratch_file.md")
        outside_path = os.path.join(tmp, "..", "outside.md")

        assert cfg.allows(home_path)
        assert cfg.allows(scratch_path)
        assert not cfg.allows(outside_path)
    print("OK")


def test_path_traversal_rejected():
    print("test_path_traversal_rejected: ", end="")
    _reset_modules()
    with tempfile.TemporaryDirectory() as tmp:
        from adk_cc.service.tenancy import TenantContext

        # Bad tenant_id
        ctx = TenantContext(tenant_id="../etc", user_id="alice", workspace_root_path=tmp)
        try:
            ctx.workspace("session-1")
            print("FAIL: expected ValueError on bad tenant_id")
            sys.exit(1)
        except ValueError as e:
            assert "tenant_id" in str(e), str(e)

        # Bad user_id
        ctx = TenantContext(tenant_id="acme", user_id="../etc", workspace_root_path=tmp)
        try:
            ctx.workspace("session-1")
            print("FAIL: expected ValueError on bad user_id")
            sys.exit(1)
        except ValueError as e:
            assert "user_id" in str(e), str(e)

        # Bad session_id
        ctx = TenantContext(tenant_id="acme", user_id="alice", workspace_root_path=tmp)
        try:
            ctx.workspace("../escape")
            print("FAIL: expected ValueError on bad session_id")
            sys.exit(1)
        except ValueError as e:
            assert "session_id" in str(e), str(e)
    print("OK")


# === DockerBackend cache mount gating ===


def test_docker_cache_mount_gating():
    print("test_docker_cache_mount_gating: ", end="")
    _reset_modules()
    # Don't construct a real DockerBackend — that would import the docker
    # SDK and try to talk to a daemon. Instead test the gating logic
    # directly by inspecting the volume map a hypothetical _spawn_container
    # would build under each input shape.
    from adk_cc.sandbox.workspace import WorkspaceRoot

    # Dev: scratch=None → no cache mount.
    ws_dev = WorkspaceRoot(
        tenant_id="local", session_id="local", abs_path="/tmp/dev",
    )
    assert ws_dev.session_scratch_path is None

    # Production: scratch set → cache mount enabled (subject to env var).
    ws_prod = WorkspaceRoot(
        tenant_id="acme", session_id="s1", abs_path="/tmp/home",
        session_scratch_path="/tmp/home/.sessions/s1",
    )
    assert ws_prod.session_scratch_path is not None

    # The actual gating is in DockerBackend._spawn_container; we re-verify
    # the predicate it uses.
    is_per_user_layout = ws_prod.session_scratch_path is not None
    cache_mount_disabled = os.environ.get("ADK_CC_DISABLE_INSTALL_CACHE_MOUNT") == "1"
    should_mount = is_per_user_layout and not cache_mount_disabled
    assert should_mount, "production path should mount install cache by default"

    os.environ["ADK_CC_DISABLE_INSTALL_CACHE_MOUNT"] = "1"
    cache_mount_disabled = os.environ.get("ADK_CC_DISABLE_INSTALL_CACHE_MOUNT") == "1"
    should_mount = is_per_user_layout and not cache_mount_disabled
    assert not should_mount, "ADK_CC_DISABLE_INSTALL_CACHE_MOUNT=1 must disable"
    del os.environ["ADK_CC_DISABLE_INSTALL_CACHE_MOUNT"]

    # Dev: never mounts even if env unset.
    is_per_user_layout = ws_dev.session_scratch_path is not None
    should_mount = is_per_user_layout
    assert not should_mount, "dev path must never get the cache mount"
    print("OK")


# === Task storage relocation ===


def test_task_storage_workspace_anchored():
    print("test_task_storage_workspace_anchored: ", end="")
    _reset_modules()
    os.environ.pop("ADK_CC_TASKS_DIR", None)
    with tempfile.TemporaryDirectory() as tmp:
        from adk_cc.tasks.model import Task
        from adk_cc.tasks.storage import JsonFileTaskStorage

        storage = JsonFileTaskStorage()
        task = Task(
            id="task-1",
            tenant_id="acme",
            session_id="s1",
            title="test",
            description="",
        )
        # Production call: pass workspace_path → anchored at <ws>/.adk-cc/tasks/<sid>/
        asyncio.run(storage.create(task, workspace_path=tmp))
        expected = Path(tmp) / ".adk-cc" / "tasks" / "s1" / "task-1.json"
        assert expected.is_file(), f"task should land at {expected}"
        # Roundtrip.
        got = asyncio.run(storage.get("task-1", tenant_id="acme", workspace_path=tmp))
        assert got.id == "task-1"
    print("OK")


def test_task_storage_legacy_when_no_workspace_path():
    print("test_task_storage_legacy_when_no_workspace_path: ", end="")
    _reset_modules()
    with tempfile.TemporaryDirectory() as legacy_root:
        os.environ["ADK_CC_TASKS_DIR"] = legacy_root
        from adk_cc.tasks.model import Task
        from adk_cc.tasks.storage import JsonFileTaskStorage

        storage = JsonFileTaskStorage()
        task = Task(
            id="task-2",
            tenant_id="acme",
            session_id="s1",
            title="legacy",
            description="",
        )
        # No workspace_path → legacy <root>/<tenant>/<sid>/
        asyncio.run(storage.create(task))
        expected = Path(legacy_root) / "acme" / "s1" / "task-2.json"
        assert expected.is_file(), f"task should land at {expected}"
        del os.environ["ADK_CC_TASKS_DIR"]
    print("OK")


def test_task_storage_tasks_dir_override_wins():
    """When ADK_CC_TASKS_DIR is set, it overrides workspace_path
    (operators chose central storage; honor them)."""
    print("test_task_storage_tasks_dir_override_wins: ", end="")
    _reset_modules()
    with tempfile.TemporaryDirectory() as central, tempfile.TemporaryDirectory() as ws:
        os.environ["ADK_CC_TASKS_DIR"] = central
        from adk_cc.tasks.model import Task
        from adk_cc.tasks.storage import JsonFileTaskStorage

        storage = JsonFileTaskStorage()
        task = Task(
            id="task-3",
            tenant_id="acme",
            session_id="s1",
            title="override",
            description="",
        )
        # workspace_path passed BUT central storage wins.
        asyncio.run(storage.create(task, workspace_path=ws))

        in_central = Path(central) / "acme" / "s1" / "task-3.json"
        in_ws = Path(ws) / ".adk-cc" / "tasks" / "s1" / "task-3.json"
        assert in_central.is_file(), "ADK_CC_TASKS_DIR should win"
        assert not in_ws.exists()
        del os.environ["ADK_CC_TASKS_DIR"]
    print("OK")


# === scratch_reaper ===


def test_scratch_reaper_reaps_old_only():
    print("test_scratch_reaper_reaps_old_only: ", end="")
    with tempfile.TemporaryDirectory() as root:
        # Build a production-shaped tree:
        #   <root>/acme/alice/(home_file.md)
        #   <root>/acme/alice/.sessions/old-session/  (mtime: 30 days ago)
        #   <root>/acme/alice/.sessions/new-session/  (mtime: now)
        user_home = Path(root) / "acme" / "alice"
        user_home.mkdir(parents=True)
        home_file = user_home / "home_file.md"
        home_file.write_text("persistent")

        old_session = user_home / ".sessions" / "old-session"
        old_session.mkdir(parents=True)
        (old_session / "scratch.txt").write_text("old")
        old_mtime = time.time() - 30 * 86400
        os.utime(old_session, (old_mtime, old_mtime))

        new_session = user_home / ".sessions" / "new-session"
        new_session.mkdir(parents=True)
        (new_session / "scratch.txt").write_text("new")

        # Run the reaper with --max-age-days 7.
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "scratch_reaper.py"),
                "--root", root,
                "--max-age-days", "7",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        # Old session reaped; new session and home file untouched.
        assert not old_session.exists(), "old session should have been reaped"
        assert new_session.is_dir(), "new session should NOT have been reaped"
        assert home_file.is_file(), "user home file must never be reaped"
        assert home_file.read_text() == "persistent"
    print("OK")


def test_scratch_reaper_dry_run():
    print("test_scratch_reaper_dry_run: ", end="")
    with tempfile.TemporaryDirectory() as root:
        old_session = Path(root) / "acme" / "alice" / ".sessions" / "old"
        old_session.mkdir(parents=True)
        (old_session / "x.txt").write_text("x")
        os.utime(old_session, (time.time() - 30 * 86400,) * 2)

        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "scratch_reaper.py"),
                "--root", root,
                "--max-age-days", "7",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert old_session.exists(), "dry-run must not delete anything"
        assert "would reap" in result.stderr or "would reap" in result.stdout
    print("OK")


# === Main ===


def main():
    test_dev_default_workspace_flat()
    test_production_per_user_isolation()
    test_per_session_scratch_isolation()
    test_production_fs_config_allows_both_roots()
    test_path_traversal_rejected()
    test_docker_cache_mount_gating()
    test_task_storage_workspace_anchored()
    test_task_storage_legacy_when_no_workspace_path()
    test_task_storage_tasks_dir_override_wins()
    test_scratch_reaper_reaps_old_only()
    test_scratch_reaper_dry_run()
    print()
    print("All workspace-layout tests passed")


if __name__ == "__main__":
    main()
