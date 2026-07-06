"""Desktop workspace: the resolver binds a session IN-PLACE to its project root
(the user's real repo). The git-worktree helpers (ensure_worktree /
remove_worktree) are retained but DORMANT — no longer on the desktop path,
reserved for a future "isolate this session" toggle — so this also smoke-tests
that they still function standalone. Model-free / server-free.

Run: PYTHONPATH=agents .venv/bin/python tests/test_desktop_workspace.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    _passed += 1 if ok else 0
    _failed += 0 if ok else 1


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def main() -> int:
    root = tempfile.mkdtemp(prefix="desktop-wt-")
    data = os.path.join(root, "data"); os.makedirs(data)
    repo = os.path.join(root, "proj"); os.makedirs(repo)
    _git(["init"], repo)
    with open(os.path.join(repo, "base.txt"), "w") as f:
        f.write("base")
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"], repo)

    os.environ["ADK_CC_DESKTOP"] = "1"
    os.environ["ADK_CC_DESKTOP_DATA"] = data

    from adk_cc.service.desktop_routes import save_projects, project_repo_path
    save_projects([{"id": "projX", "name": "proj", "repo_path": repo}])
    check("project_repo_path resolves the registry", project_repo_path("projX") == repo)

    from adk_cc.service.desktop_workspace import (
        ensure_worktree, remove_worktree, desktop_tenant_resolver,
    )

    # --- Dormant worktree helpers still function standalone (not on the desktop
    #     path anymore, but kept for a future isolate-session mode). ---
    wt1 = ensure_worktree(repo, "projX", "sid1")
    check("session1 worktree created", os.path.exists(os.path.join(wt1, ".git")))
    check("session1 worktree has the repo's files", os.path.exists(os.path.join(wt1, "base.txt")))
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt1).stdout.strip()
    check("session1 worktree on branch adk-cc/sid1", branch == "adk-cc/sid1")
    check("ensure_worktree is idempotent", ensure_worktree(repo, "projX", "sid1") == wt1)

    # isolation: a file written in session1 must NOT be in session2
    with open(os.path.join(wt1, "only_in_1.txt"), "w") as f:
        f.write("x")
    wt2 = ensure_worktree(repo, "projX", "sid2")
    check("session2 worktree isolated (no session1's file)",
          not os.path.exists(os.path.join(wt2, "only_in_1.txt")))
    check("session2 worktree has the base repo files",
          os.path.exists(os.path.join(wt2, "base.txt")))

    # --- The live desktop path: resolver → context.workspace() binds IN-PLACE
    #     to the project root (NOT a per-session worktree). ---
    ctx = desktop_tenant_resolver("projX")
    ws = ctx.workspace("sid3")
    check("resolver workspace binds in-place to the project root",
          os.path.realpath(ws.abs_path) == os.path.realpath(repo))
    check("resolver workspace has the repo's files", os.path.exists(os.path.join(ws.abs_path, "base.txt")))
    check("resolver did NOT create a worktree for the session",
          not os.path.isdir(os.path.join(data, "worktrees", "projX", "sid3")))

    # the path a real turn uses: TenancyPlugin seeds the project root into state
    from adk_cc.service.tenancy import TenancyPlugin
    plugin = TenancyPlugin(tenant_resolver=desktop_tenant_resolver)
    state: dict = {}
    plugin._seed_state(state, user_id="projX", session=SimpleNamespace(id="sid4"))
    seeded = state.get("temp:sandbox_workspace")
    check("TenancyPlugin seeds the project root as sandbox_workspace",
          seeded is not None and os.path.realpath(seeded.abs_path) == os.path.realpath(repo))

    # unbound user (no project) → flat scratch, not a worktree
    ws_local = desktop_tenant_resolver("local").workspace("sidL")
    check("unbound user falls back to a scratch dir (no project)",
          "scratch" in ws_local.abs_path and "worktrees" not in ws_local.abs_path)

    # teardown removes the worktree + branch
    remove_worktree("projX", "sid1")
    check("remove_worktree removes the worktree dir", not os.path.isdir(wt1))
    check("remove_worktree deletes the branch",
          _git(["rev-parse", "--verify", "adk-cc/sid1"], repo).returncode != 0)

    shutil.rmtree(root, ignore_errors=True)
    print(f"\ndesktop worktree: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
