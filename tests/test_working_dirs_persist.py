"""Persistence test for desktop "Working directories" (Phase 4a).

A working dir written to the project's shared `user:` state is overlaid onto a
FRESH session of that project and folded into the sandbox scope by
`get_workspace` — i.e. it survives across sessions.

Run: `.venv/bin/python tests/test_working_dirs_persist.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ["ADK_CC_DESKTOP"] = "1"
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.sandbox.workspace import WorkspaceRoot, _STATE_KEY, get_workspace
from adk_cc.service.file_session_service import FileSessionService

_APP = "adk_cc"


class _Ctx:
    def __init__(self, state):
        self.state = state


def _allows(state, project, path) -> bool:
    state = dict(state)
    state[_STATE_KEY] = WorkspaceRoot(tenant_id="local", session_id="s", abs_path=project)
    return get_workspace(_Ctx(state)).fs_write_config().allows(os.path.realpath(path))


def test_set_user_value_round_trips() -> None:
    fss = FileSessionService(tempfile.mkdtemp(prefix="wd-"))
    granted = os.path.realpath(tempfile.mkdtemp())
    fss.set_user_value("proj-1", "adk_cc_extra_roots", [granted])
    assert fss.get_user_value("proj-1", "adk_cc_extra_roots") == [granted]
    print("OK test_set_user_value_round_trips")


def test_persistent_dir_visible_in_fresh_session() -> None:
    base = tempfile.mkdtemp(prefix="wd-")
    fss = FileSessionService(base)
    pid = "proj-2"
    project = os.path.realpath(tempfile.mkdtemp(prefix="proj-"))
    granted = os.path.realpath(tempfile.mkdtemp(prefix="data-"))

    # Persist a working directory for the project (what POST /desktop/working-dirs does).
    fss.set_user_value(pid, "adk_cc_extra_roots", [granted])

    # A brand-new session for the same project overlays the shared user state...
    sess = asyncio.run(fss.create_session(app_name=_APP, user_id=pid))
    got = asyncio.run(fss.get_session(app_name=_APP, user_id=pid, session_id=sess.id))
    assert got.state.get("user:adk_cc_extra_roots") == [granted], got.state
    # ...and get_workspace folds it into the sandbox scope.
    assert _allows(got.state, project, f"{granted}/x.txt")
    assert not _allows(got.state, project, "/tmp/ungranted/x.txt")
    print("OK test_persistent_dir_visible_in_fresh_session")


def main() -> None:
    test_set_user_value_round_trips()
    test_persistent_dir_visible_in_fresh_session()
    print("\nall working-dirs persistence tests passed")


if __name__ == "__main__":
    main()
