"""Desktop workspace binding: a session in a project binds IN-PLACE to that
project's repo root (the user's real files); all sessions of a project share
that root (no per-session worktree); no-project falls back to a flat scratch dir.
Also covers the NoopBackend host-exec ack that makes in-place work for projects
outside $HOME.

This exercises the SAME path the desktop TenancyPlugin uses at runtime
(`desktop_tenant_resolver(user_id).workspace(session_id)` — where user_id is the
project id), so it directly checks the "binds workspace correctly in desktop
mode" behavior. No model / server needed.

Run: `.venv/bin/python tests/test_desktop_workspace_bind.py`
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

# Point the desktop data dir at a throwaway dir BEFORE importing the resolver
# (project_repo_path / desktop_data_dir read this env).
_TMP = tempfile.mkdtemp(prefix="adk-cc-ws-bind-")
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP

from adk_cc.service.desktop_workspace import desktop_tenant_resolver


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _make_project(project_id: str) -> str:
    """Create a git repo with one committed file + register it as a project."""
    repo = Path(_TMP) / f"repo-{project_id}"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("hello from the project repo\n", encoding="utf-8")
    _git(["init", "-q"], str(repo))
    _git(["add", "-A"], str(repo))
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], str(repo))
    # register in projects.json (what project_repo_path reads)
    reg = Path(_TMP) / "projects.json"
    items = json.loads(reg.read_text()) if reg.is_file() else []
    items.append({"id": project_id, "repo_path": str(repo)})
    reg.write_text(json.dumps(items), encoding="utf-8")
    return str(repo)


def test_session_binds_to_project_root() -> None:
    repo = _make_project("projA")
    ws = desktop_tenant_resolver("projA").workspace("sessA")

    # In-place: the workspace IS the project root (the user's real git repo), not
    # a per-session worktree. resolve() ignores a /var → /private/var symlink.
    assert Path(ws.abs_path).resolve() == Path(repo).resolve(), f"{ws.abs_path} != {repo}"
    assert ws.session_id == "sessA"
    assert (Path(ws.abs_path) / ".git").is_dir(), "workspace isn't the real git repo"
    assert (Path(ws.abs_path) / "README.md").read_text().startswith("hello")
    # And NO worktree was created.
    assert not (Path(_TMP) / "worktrees" / "projA").exists(), "in-place must not create a worktree"
    print("OK test_session_binds_to_project_root")


def test_sessions_share_project_root() -> None:
    # In-place: two sessions of one project both work in the SAME project root —
    # per-session isolation is intentionally removed for the single-user desktop
    # app (was `test_sessions_are_isolated`).
    repo = _make_project("projB")
    a = desktop_tenant_resolver("projB").workspace("s1").abs_path
    b = desktop_tenant_resolver("projB").workspace("s2").abs_path
    assert Path(a).resolve() == Path(b).resolve() == Path(repo).resolve(), \
        "in-place sessions should share the project root"
    print("OK test_sessions_share_project_root")


def test_same_session_is_idempotent() -> None:
    _make_project("projC")
    a = desktop_tenant_resolver("projC").workspace("dup").abs_path
    b = desktop_tenant_resolver("projC").workspace("dup").abs_path
    assert a == b, "same (project, session) resolved to different worktrees"
    print("OK test_same_session_is_idempotent")


def test_no_project_falls_back_to_scratch() -> None:
    # An unregistered / default user has no repo → a flat scratch dir, NOT a
    # git worktree (nothing to branch from).
    ws = desktop_tenant_resolver(None).workspace("s")
    assert Path(ws.abs_path).resolve() == (Path(_TMP) / "scratch" / "local").resolve(), ws.abs_path
    assert Path(ws.abs_path).is_dir()
    assert not (Path(ws.abs_path) / ".git").exists(), "scratch should not be a worktree"
    print("OK test_no_project_falls_back_to_scratch")


def test_noop_ack_defaults_to_desktop() -> None:
    # The in-place workspace can sit outside $HOME (e.g. /opt, /Volumes), which
    # trips NoopBackend's prod-path guard. The desktop profile acks host exec by
    # default so run_bash works there; web mode does not.
    from adk_cc import deployment

    os.environ.pop("ADK_CC_NOOP_ACK_HOST_EXEC", None)
    os.environ["ADK_CC_DESKTOP"] = "1"
    assert deployment.noop_ack_host_exec() is True, "desktop should ack host exec by default"
    del os.environ["ADK_CC_DESKTOP"]
    assert deployment.noop_ack_host_exec() is False, "non-desktop must not ack by default"
    os.environ["ADK_CC_NOOP_ACK_HOST_EXEC"] = "1"
    assert deployment.noop_ack_host_exec() is True, "explicit env must override"
    os.environ.pop("ADK_CC_NOOP_ACK_HOST_EXEC", None)
    print("OK test_noop_ack_defaults_to_desktop")


def test_out_of_home_project_execs_in_desktop() -> None:
    # End-to-end of the ack: an in-place project OUTSIDE the safe prefixes trips
    # NoopBackend guard 1; the desktop ack lets run_bash through, web mode doesn't.
    # We can't create a real /opt dir in a test, so neutralize the safe-prefix
    # allowlist to make the (real, under-tmp) project root look "prod-shaped".
    import asyncio

    from adk_cc.sandbox.backends import noop_backend
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig, SandboxViolation

    repo = _make_project("projX")
    orig = noop_backend._safe_prefixes
    noop_backend._safe_prefixes = lambda: []  # every path is now "prod-shaped"
    try:
        be = noop_backend.NoopBackend()
        # desktop → ack default True → run_bash proceeds without SandboxViolation
        os.environ["ADK_CC_DESKTOP"] = "1"
        os.environ.pop("ADK_CC_NOOP_ACK_HOST_EXEC", None)
        res = asyncio.run(
            be.exec("true", fs_write=FsWriteConfig(), network=NetworkConfig(), timeout_s=10, cwd=repo)
        )
        assert res.exit_code == 0, f"expected exec to run in-place, got {res}"
        # web mode (no desktop, no ack) → the guard fires
        del os.environ["ADK_CC_DESKTOP"]
        try:
            asyncio.run(
                be.exec("true", fs_write=FsWriteConfig(), network=NetworkConfig(), timeout_s=10, cwd=repo)
            )
        except SandboxViolation:
            pass
        else:
            raise AssertionError("expected SandboxViolation for a prod-shaped path without the ack")
    finally:
        noop_backend._safe_prefixes = orig
        os.environ.pop("ADK_CC_DESKTOP", None)
        os.environ.pop("ADK_CC_NOOP_ACK_HOST_EXEC", None)
    print("OK test_out_of_home_project_execs_in_desktop")


def main() -> None:
    test_session_binds_to_project_root()
    test_sessions_share_project_root()
    test_same_session_is_idempotent()
    test_no_project_falls_back_to_scratch()
    test_noop_ack_defaults_to_desktop()
    test_out_of_home_project_execs_in_desktop()
    print("\nall desktop workspace-bind tests passed")


if __name__ == "__main__":
    main()
