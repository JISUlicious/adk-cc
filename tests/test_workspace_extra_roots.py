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
    _SESSION_ROOTS_KEY,
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
    # Outside the system scratch space, which is allowed by default — a
    # mkdtemp dir would be permitted before AND after the grant, proving
    # nothing about granting or revoking.
    data = "/opt/adk-cc-ungranted"
    ctx = _Ctx(proj)
    # `data` is a mkdtemp dir, and the system scratch space is allowed by
    # default — so the denial has to be asserted on a path outside it.
    assert not _allows(ctx, "/opt/adk-cc-ungranted/x")   # nothing granted yet
    add_granted_root(ctx, data)
    assert _allows(ctx, f"{data}/x")                # session grant folds in
    assert data in list_granted_roots(ctx)
    # one-shot grant of an exact file
    onefile = "/opt/adk-cc-once/loose.txt"   # distinct from the granted root
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
    # Outside the system scratch space, which is allowed by default — a
    # mkdtemp dir would be permitted before AND after the grant, proving
    # nothing about granting or revoking.
    data = "/opt/adk-cc-ungranted"
    ctx = _Ctx(proj)
    add_granted_root(ctx, data, persist=True)       # user: scope
    assert "user:adk_cc_extra_roots" in ctx.state
    assert _allows(ctx, f"{data}/x")
    print("OK test_desktop_folds_user_persistent_grants")


def test_remote_workspace_grants_are_not_locally_canonicalized() -> None:
    """#109, reported live: on a REMOTE (ssh) workspace the agent created a
    git worktree beside the project, and every write there re-asked forever.

    `_append_root` realpath'd unconditionally, so on macOS the grant was
    stored as /System/Volumes/Data/home/... (and /tmp -> /private/tmp) —
    paths that do not exist on the remote — and never matched the next write.
    WorkspaceRoot.__post_init__ already refused to do this for remote paths;
    the grant helpers did not."""
    os.environ["ADK_CC_DESKTOP"] = "1"
    try:
        class _RemoteCtx:
            def __init__(self, root):
                self.state = {
                    _STATE_KEY: WorkspaceRoot(tenant_id="local", session_id="s",
                                              abs_path=root, remote=True)
                }

        ctx = _RemoteCtx("/home/u/proj")
        target = "/home/u/proj-feature/new.py"
        allows = lambda: get_workspace(ctx).fs_write_config().allows(target)  # noqa: E731
        assert not allows(), "fixture: the worktree must start out of scope"
        add_granted_root(ctx, "/home/u/proj-feature")
        assert ctx.state[_SESSION_ROOTS_KEY] == ["/home/u/proj-feature"], \
            ctx.state[_SESSION_ROOTS_KEY]
        assert allows(), "a granted remote folder must widen the scope"

        # /tmp is the other shape that silently diverged.
        ctx2 = _RemoteCtx("/home/u/proj")
        add_granted_root(ctx2, "/tmp/build")
        assert ctx2.state[_SESSION_ROOTS_KEY] == ["/tmp/build"], \
            ctx2.state[_SESSION_ROOTS_KEY]

        # State written by the BUGGY version must still be recognised, or a
        # user mid-session stays stuck and the fix looks like it did nothing.
        ctx3 = _RemoteCtx("/home/u/proj")
        ctx3.state[_SESSION_ROOTS_KEY] = ["/System/Volumes/Data/home/u/proj-feature"]
        add_granted_root(ctx3, "/home/u/proj-feature")
        assert len(ctx3.state[_SESSION_ROOTS_KEY]) == 1, \
            f"stale grant re-added: {ctx3.state[_SESSION_ROOTS_KEY]}"
    finally:
        os.environ.pop("ADK_CC_DESKTOP", None)
    print("OK test_remote_workspace_grants_are_not_locally_canonicalized")


def test_web_mode_ignores_grants() -> None:
    os.environ["ADK_CC_DESKTOP"] = "0"
    try:
        proj = os.path.realpath(tempfile.mkdtemp())
        data = "/opt/adk-cc-ungranted"              # outside the scratch allowance
        ctx = _Ctx(proj)
        add_granted_root(ctx, data)                 # written to state...
        assert not _allows(ctx, f"{data}/x")        # ...but never folded in
        # The project root is the only GRANT-derived entry; the scratch
        # dirs (/tmp and $TMPDIR) are unconditional defaults in every mode.
        paths = get_workspace(ctx).fs_write_config().allow_paths
        assert paths[:2] == (f"{proj}/**", proj), paths
        assert not any(data in p for p in paths), paths
    finally:
        os.environ["ADK_CC_DESKTOP"] = "1"
    print("OK test_web_mode_ignores_grants")


def main() -> None:
    test_allow_paths_include_extra_roots_read_and_write()
    test_extra_roots_canonicalized_and_deduped()
    test_desktop_folds_session_and_once_grants()
    test_desktop_folds_user_persistent_grants()
    test_remote_workspace_grants_are_not_locally_canonicalized()
    test_web_mode_ignores_grants()
    print("\nall workspace-extra-roots tests passed")


if __name__ == "__main__":
    main()
