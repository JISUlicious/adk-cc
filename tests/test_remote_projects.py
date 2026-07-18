"""Unit/route tests for remote (SSH) desktop projects — PR 4.

Covers, all pure / local-HTTP (no live ssh; that's e2e_remote_project.py):
  - registry routes: create remote project, dedup, validation, list shape
  - test-remote: unreachable host → {ok:false, actionable error} (fast)
  - resolver: remote project → remote-flagged WorkspaceRoot at the remote path
  - backend factory: remote project → SshBackend on the shared transport;
    local project → the default factory's backend (noop here)
  - session-backend route: config prediction says ssh for a remote project
  - PERMISSION FLOOR (the security piece): with a remote workspace, paths
    resolve lexically and classify against the REMOTE home — `cat
    ~/.ssh/id_rsa` and read_file of `<remote_home>/.ssh/…` are denied for
    the REMOTE machine, `..` collapses lexically, and the LOCAL behavior is
    unchanged (existing suites keep passing).

Run: `uv run python tests/test_remote_projects.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="adk-remote-proj-")
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"

_HOME = "/home/dev"
_WS = f"{_HOME}/proj"


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.service.desktop_routes import mount_desktop_routes

    app = FastAPI()
    mount_desktop_routes(app)
    return TestClient(app)


def test_remote_project_routes():
    from adk_cc.service.desktop_routes import project_remote, project_repo_path

    c = _client()
    # Validation: missing fields / relative path rejected.
    assert c.post("/desktop/projects/remote", json={"host": "h"}).status_code == 400
    assert (
        c.post(
            "/desktop/projects/remote", json={"host": "h", "path": "rel/path"}
        ).status_code
        == 400
    )

    r = c.post(
        "/desktop/projects/remote", json={"host": "dev@box", "path": _WS}
    )
    assert r.status_code == 200, r.text
    proj = r.json()["project"]
    assert proj["remote"] == {"host": "dev@box", "path": _WS}, proj
    assert proj["name"] == "dev@box:proj", proj

    # Dedup by (host, path).
    r2 = c.post("/desktop/projects/remote", json={"host": "dev@box", "path": _WS})
    assert r2.json()["project"]["id"] == proj["id"]

    # Registry helpers branch correctly.
    assert project_remote(proj["id"]) == {"host": "dev@box", "path": _WS}
    assert project_repo_path(proj["id"]) is None  # remote has no local repo
    print("OK remote_project_routes")
    return proj


def test_test_remote_unreachable_fast():
    """Nothing listens on 127.0.0.1:1 → ok:false with an actionable error,
    within the connect timeout (no hang, no prompt)."""
    import time

    c = _client()
    t0 = time.monotonic()
    r = c.post("/desktop/projects/test-remote", json={"host": "127.0.0.1", "port": 1})
    wall = time.monotonic() - t0
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False, body
    assert "failed" in (body.get("error") or "").lower(), body
    assert wall < 30, f"unreachable probe took {wall:.1f}s"
    print("OK test_remote_unreachable_fast")


def test_resolver_and_factory(proj: dict):
    from adk_cc.sandbox.backends.ssh_backend import SshBackend
    from adk_cc.service.desktop_workspace import (
        desktop_backend_factory,
        desktop_tenant_resolver,
    )

    # Remote project → remote ctx → remote-flagged workspace at the remote path.
    ctx = desktop_tenant_resolver(proj["id"])
    assert ctx.remote == {"host": "dev@box", "path": _WS}
    ws = ctx.workspace("sess-1")
    assert ws.remote is True and ws.abs_path == _WS, ws

    b = desktop_backend_factory(ctx, "sess-1")
    assert isinstance(b, SshBackend), type(b)
    assert b.host == "dev@box"

    # Local (unknown) project → default flow, non-ssh backend.
    lctx = desktop_tenant_resolver("no-such-project")
    lb = desktop_backend_factory(lctx, "sess-2")
    assert not isinstance(lb, SshBackend), type(lb)
    lws = lctx.workspace("sess-2")
    assert lws.remote is False
    print("OK resolver_and_factory")


def test_session_backend_config_predicts_ssh(proj: dict):
    c = _client()
    r = c.get(
        "/desktop/sessions/backend",
        params={"session_id": "sess-fresh", "project_id": proj["id"]},
    )
    body = r.json()
    assert body == {
        "source": "config",
        "backend": "ssh",
        "detail": "dev@box",
        "isolated": False,
    }, body
    print("OK session_backend_config_predicts_ssh")


def test_permission_floor_guards_remote_machine():
    """The security piece: with remote_home set, the floor classifies against
    the REMOTE home — lexically, no local fs consultation."""
    from adk_cc.permissions.engine import decide
    from adk_cc.permissions.modes import PermissionMode as M
    from adk_cc.permissions.protected import classify_path
    from adk_cc.permissions.settings import SettingsHierarchy
    from adk_cc.tools.bash.tool import BashTool
    from adk_cc.tools.read_file import ReadFileTool

    # classify_path: remote home expansion + case-fold; local realpath NOT used.
    assert classify_path(f"{_HOME}/.ssh/id_rsa", remote_home=_HOME) == "deny"
    assert classify_path(f"{_HOME}/.SSH/id_rsa", remote_home=_HOME) == "deny"
    assert classify_path(f"{_HOME}/.gitconfig", remote_home=_HOME) == "ask"
    assert classify_path(f"{_WS}/notes.txt", remote_home=_HOME) is None
    # A DIFFERENT home on the remote — the LOCAL ~/.ssh pattern must not leak in.
    assert classify_path("/config/.ssh/key", remote_home="/config") == "deny"
    local_home = os.path.expanduser("~")
    assert classify_path(f"{local_home}/.ssh/x", remote_home="/config") is None

    def _decide(tool, args):
        return decide(
            tool=tool,
            args=args,
            mode=M.DEFAULT,
            settings=SettingsHierarchy([]),
            workspace_root=_WS,
            remote_home=_HOME,
        ).behavior

    # read_file of remote secret material → deny (incl. via ~ and via ..).
    assert _decide(ReadFileTool(), {"path": f"{_HOME}/.ssh/id_rsa"}) == "deny"
    assert _decide(ReadFileTool(), {"path": "~/.ssh/id_rsa"}) == "deny"
    assert _decide(ReadFileTool(), {"path": "../.ssh/id_rsa"}) == "deny"

    # run_bash reading remote secrets → deny; benign in-workspace cmd is not.
    assert _decide(BashTool(), {"command": "cat ~/.ssh/id_rsa"}) == "deny"
    assert _decide(BashTool(), {"command": f"cat {_HOME}/.aws/credentials"}) == "deny"
    assert _decide(BashTool(), {"command": "ls -la"}) != "deny"
    print("OK permission_floor_guards_remote_machine")


def main():
    proj = test_remote_project_routes()
    test_test_remote_unreachable_fast()
    test_resolver_and_factory(proj)
    test_session_backend_config_predicts_ssh(proj)
    test_permission_floor_guards_remote_machine()
    print("\nall remote-projects tests passed")


if __name__ == "__main__":
    main()
