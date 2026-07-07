"""Unit tests for desktop granted-directory plumbing on WorkspaceRoot /
get_workspace (Phase 1 of the grantable-scope change).

Run: `.venv/bin/python tests/test_workspace_extra_roots.py`
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.sandbox.workspace import (
    WorkspaceRoot,
    _STATE_KEY,
    add_granted_root,
    clear_grant_once,
    get_workspace,
    grant_once,
    list_granted_roots,
    remove_granted_root,
)


class _Ctx:
    def __init__(self, project: str):
        self.state = {
            _STATE_KEY: WorkspaceRoot(tenant_id="local", session_id="s", abs_path=project)
        }


def _allows(ctx, p: str) -> bool:
    return get_workspace(ctx).fs_write_config().allows(os.path.realpath(p))


def test_allow_paths_include_extra_roots_read_and_write() -> None:
    r = os.path.realpath(tempfile.mkdtemp())
    ws = WorkspaceRoot(tenant_id="t", session_id="s", abs_path="/tmp/proj", extra_roots=(r,))
    for cfg in (ws.fs_read_config(), ws.fs_write_config()):
        assert f"{r}/**" in cfg.allow_paths and r in cfg.allow_paths, cfg.allow_paths
    print("OK test_allow_paths_include_extra_roots_read_and_write")


def test_extra_roots_canonicalized_and_deduped() -> None:
    # /tmp → /private/tmp on macOS; blanks + the primary root are dropped.
    ws = WorkspaceRoot(
        tenant_id="t", session_id="s", abs_path="/tmp/proj",
        extra_roots=("/tmp/data", "/tmp/data", "", "/tmp/proj"),
    )
    assert ws.extra_roots == (os.path.realpath("/tmp/data"),), ws.extra_roots
    print("OK test_extra_roots_canonicalized_and_deduped")


def test_desktop_folds_session_and_once_grants() -> None:
    os.environ["ADK_CC_DESKTOP"] = "1"
    proj = os.path.realpath(tempfile.mkdtemp())
    data = os.path.realpath(tempfile.mkdtemp())
    ctx = _Ctx(proj)
    assert not _allows(ctx, f"{data}/x")            # nothing granted yet
    add_granted_root(ctx, data)
    assert _allows(ctx, f"{data}/x")                # session grant folds in
    assert data in list_granted_roots(ctx)
    # one-shot grant of an exact file
    onefile = os.path.join(proj, "..", "loose.txt")
    grant_once(ctx, onefile)
    assert _allows(ctx, onefile)
    clear_grant_once(ctx)
    assert not _allows(ctx, onefile)
    remove_granted_root(ctx, data)
    assert not _allows(ctx, f"{data}/x")
    print("OK test_desktop_folds_session_and_once_grants")


def test_desktop_folds_user_persistent_grants() -> None:
    os.environ["ADK_CC_DESKTOP"] = "1"
    proj = os.path.realpath(tempfile.mkdtemp())
    data = os.path.realpath(tempfile.mkdtemp())
    ctx = _Ctx(proj)
    add_granted_root(ctx, data, persist=True)       # user: scope
    assert "user:adk_cc_extra_roots" in ctx.state
    assert _allows(ctx, f"{data}/x")
    print("OK test_desktop_folds_user_persistent_grants")


def test_web_mode_ignores_grants() -> None:
    os.environ["ADK_CC_DESKTOP"] = "0"
    try:
        proj = os.path.realpath(tempfile.mkdtemp())
        data = os.path.realpath(tempfile.mkdtemp())
        ctx = _Ctx(proj)
        add_granted_root(ctx, data)                 # written to state...
        assert not _allows(ctx, f"{data}/x")        # ...but never folded in
        assert get_workspace(ctx).fs_write_config().allow_paths == (f"{proj}/**", proj)
    finally:
        os.environ["ADK_CC_DESKTOP"] = "1"
    print("OK test_web_mode_ignores_grants")


def main() -> None:
    test_allow_paths_include_extra_roots_read_and_write()
    test_extra_roots_canonicalized_and_deduped()
    test_desktop_folds_session_and_once_grants()
    test_desktop_folds_user_persistent_grants()
    test_web_mode_ignores_grants()
    print("\nall workspace-extra-roots tests passed")


if __name__ == "__main__":
    main()
