"""Desktop checkpoint / undo via a SHADOW git repo (ADK_CC_DESKTOP=1, in-place).

In-place desktop mode edits the user's real files (see ``desktop_workspace``), so
we keep an UNDO net. Before the first mutating tool of each turn, the
``CheckpointPlugin`` snapshots the project's working tree into a SEPARATE shadow
git repo:

    GIT_DIR    = <desktop_data_dir>/checkpoints/<project>/<session>   (our store)
    GIT_WORK_TREE = the project's repo root                           (user files)

The user's real ``.git`` — its objects, index, HEAD, refs, reflog — is NEVER
touched: the shadow has its own object store, index and HEAD, and the user's
``.git`` directory is excluded from every snapshot. Snapshots honor the
project's ``.gitignore`` (ignored / secret / huge files are neither snapshotted
nor reverted on undo).

Restore ("undo last turn") first snapshots the CURRENT state — so the restore is
itself reversible AND files created during the reverted turn (untracked in the
shadow until now) become tracked and get removed — then ``git reset --hard`` the
shadow work tree to the chosen checkpoint. Only tracked files move; ignored
files are left alone.

Everything here swallows failures and is bounded by per-op timeouts: a checkpoint
must never block or crash a tool call.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .desktop_routes import desktop_data_dir

_log = logging.getLogger(__name__)

# Keep the most recent N checkpoints in the restore menu (the shadow git history
# retains the commits themselves — git dedups blobs, so this stays cheap).
MAX_CHECKPOINTS = 20

# Per-op wall-clock caps — a giant working tree must not stall a tool call.
_ADD_TIMEOUT = 20
_COMMIT_TIMEOUT = 20
_RESET_TIMEOUT = 30
_QUERY_TIMEOUT = 10

# Identity + safety flags for every write op: a fixed author (never the user's),
# no signing, no hooks (the user's hooks must not fire on our shadow), no auto-gc.
_GIT_CONF = [
    "-c", "user.email=checkpoint@adk-cc.local",
    "-c", "user.name=adk-cc checkpoint",
    "-c", "commit.gpgsign=false",
    "-c", "gc.auto=0",
    "-c", "core.hooksPath=/dev/null",
]

_LOG_NAME = "adk_cc_checkpoints.json"


def enabled() -> bool:
    """Checkpointing is on by default in desktop mode; ADK_CC_CHECKPOINT=0 kills it."""
    from .. import deployment

    return deployment.is_desktop() and os.environ.get("ADK_CC_CHECKPOINT") != "0"


def _shadow_dir(project_id: str, session_id: str) -> Path:
    return desktop_data_dir() / "checkpoints" / project_id / session_id


def _run_git(
    args: list[str],
    git_dir: Path,
    work_tree: str,
    *,
    timeout: int = _QUERY_TIMEOUT,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(work_tree),
    }
    # Drop any inherited index/config pointers that could redirect the op at the
    # user's real repo instead of our explicit GIT_DIR/GIT_WORK_TREE.
    for leak in ("GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_GLOBAL"):
        env.pop(leak, None)
    return subprocess.run(
        ["git", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ensure_shadow(git_dir: Path, work_tree: str) -> None:
    """Create the shadow repo once; make it idempotent + exclude the user's .git."""
    if (git_dir / "HEAD").exists():
        return
    git_dir.mkdir(parents=True, exist_ok=True)
    # A bare-style store: we always pass GIT_WORK_TREE explicitly, so init the db
    # directly in git_dir (not a nested `.git`).
    _run_git(["init", "-q"], git_dir, work_tree, timeout=_QUERY_TIMEOUT)
    # Belt-and-suspenders: git already skips a `.git` entry in the work tree, but
    # exclude it (and our own log) explicitly so `add -A` can never descend into
    # the user's real repo db.
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text("/.git/\n", encoding="utf-8")


def _log_path(git_dir: Path) -> Path:
    # Lives inside GIT_DIR (never the work tree) → never itself snapshotted.
    return git_dir / _LOG_NAME


def _read_log(git_dir: Path) -> list[dict[str, Any]]:
    p = _log_path(git_dir)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_log(git_dir: Path, entries: list[dict[str, Any]]) -> None:
    try:
        _log_path(git_dir).write_text(json.dumps(entries), encoding="utf-8")
    except Exception:
        pass


def snapshot(
    project_id: str,
    session_id: str,
    work_tree: str,
    *,
    reason: str = "",
) -> Optional[str]:
    """Snapshot the work tree into the shadow repo; return the commit sha (or the
    existing HEAD when nothing changed). Returns None and swallows on any failure
    — a checkpoint must never break a tool call."""
    try:
        if not work_tree or not os.path.isdir(work_tree):
            return None
        gd = _shadow_dir(project_id, session_id)
        _ensure_shadow(gd, work_tree)

        add = _run_git(["add", "-A"], gd, work_tree, timeout=_ADD_TIMEOUT)
        if add.returncode != 0:
            _log.debug("checkpoint add failed: %s", add.stderr.strip()[:300])
            return None

        head = _run_git(
            ["rev-parse", "-q", "--verify", "HEAD"], gd, work_tree
        ).stdout.strip()
        if head:
            # Nothing staged differs from HEAD → reuse it, don't pile up empties.
            if _run_git(["diff", "--cached", "--quiet"], gd, work_tree).returncode == 0:
                return head

        commit = _run_git(
            _GIT_CONF + ["commit", "-q", "-m", f"checkpoint: {reason}"],
            gd,
            work_tree,
            timeout=_COMMIT_TIMEOUT,
        )
        if commit.returncode != 0:
            _log.debug("checkpoint commit failed: %s", commit.stderr.strip()[:300])
            return None
        sha = _run_git(["rev-parse", "HEAD"], gd, work_tree).stdout.strip()
        if not sha:
            return None

        entries = _read_log(gd)
        entries.append({"sha": sha, "reason": reason, "ts": time.time()})
        _write_log(gd, entries[-MAX_CHECKPOINTS:])
        return sha
    except subprocess.TimeoutExpired:
        _log.warning("checkpoint snapshot timed out for %s/%s", project_id, session_id)
        return None
    except Exception as e:  # noqa: BLE001 — never break the tool loop
        _log.debug("checkpoint snapshot error: %s", e)
        return None


def list_checkpoints(project_id: str, session_id: str) -> list[dict[str, Any]]:
    """Most-recent-first checkpoints for the restore menu."""
    gd = _shadow_dir(project_id, session_id)
    return list(reversed(_read_log(gd)))


def restore(
    project_id: str,
    session_id: str,
    work_tree: str,
    *,
    sha: Optional[str] = None,
) -> dict[str, Any]:
    """Restore the work tree to a checkpoint (default: the most recent one → undo
    the last turn). Snapshots the current state first (reversible undo + removes
    files created since the target), then hard-resets the shadow work tree."""
    if not work_tree or not os.path.isdir(work_tree):
        return {"status": "error", "error": "no bound project workspace"}
    gd = _shadow_dir(project_id, session_id)
    if not (gd / "HEAD").exists():
        return {"status": "no_checkpoints"}

    entries = _read_log(gd)
    if sha is None:
        if not entries:
            return {"status": "no_checkpoints"}
        target = entries[-1]["sha"]
    else:
        if not any(e["sha"] == sha for e in entries):
            return {"status": "error", "error": f"unknown checkpoint: {sha}"}
        target = sha

    try:
        # Capture the pre-restore state so (a) undo is itself reversible and
        # (b) files created since `target` become tracked → removed by the reset.
        pre = snapshot(project_id, session_id, work_tree, reason="pre-restore")
        reset = _run_git(
            _GIT_CONF + ["reset", "--hard", target],
            gd,
            work_tree,
            timeout=_RESET_TIMEOUT,
        )
        if reset.returncode != 0:
            return {"status": "error", "error": reset.stderr.strip()[:300]}
        return {"status": "ok", "restored_to": target, "pre_restore": pre}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "restore timed out"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}
