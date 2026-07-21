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
import uuid
from pathlib import Path
from typing import Any, Optional

from .desktop_routes import desktop_data_dir
from ..config.schema import env_bool

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

    return deployment.is_desktop() and env_bool("ADK_CC_CHECKPOINT", True)


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


def _entry_id(e: dict[str, Any]) -> str:
    """Stable unique id for a checkpoint entry. New entries carry `id`; older ones
    (pre-`id`) fall back to their invocation_id, else sha+ts — so a checkpoint is
    addressable independent of its git sha (which is NOT unique: a turn that
    changes no files reuses the previous commit)."""
    return e.get("id") or e.get("invocation_id") or f"{e.get('sha', '')}-{e.get('ts', '')}"


def _append_checkpoint(
    git_dir: Path, sha: str, reason: str, invocation_id: Optional[str]
) -> None:
    """Record one checkpoint in the log — once per turn (invocation). Idempotent:
    a repeated call for the same invocation (belt-and-suspenders vs the plugin's
    once-per-turn guard) doesn't duplicate the entry."""
    entries = _read_log(git_dir)
    if (
        invocation_id
        and entries
        and entries[-1].get("invocation_id") == invocation_id
    ):
        return
    entries.append(
        {
            "id": uuid.uuid4().hex[:12],
            "sha": sha,
            "reason": reason,
            "ts": time.time(),
            "invocation_id": invocation_id,
        }
    )
    _write_log(git_dir, entries[-MAX_CHECKPOINTS:])


def snapshot(
    project_id: str,
    session_id: str,
    work_tree: str,
    *,
    reason: str = "",
    invocation_id: Optional[str] = None,
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
        if head and _run_git(["diff", "--cached", "--quiet"], gd, work_tree).returncode == 0:
            # No file change vs HEAD (e.g. this turn only ran `ls -la`). Don't add
            # an empty git commit — but STILL record this turn's checkpoint (same
            # sha, THIS invocation) so every turn maps to its own rewind point.
            # Otherwise a no-file-change turn has no checkpoint and the NEXT turn's
            # rewind lands at the WRONG (earlier) invocation → conversation jumps
            # back too far.
            _append_checkpoint(gd, head, reason, invocation_id)
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

        _append_checkpoint(gd, sha, reason, invocation_id)
        return sha
    except subprocess.TimeoutExpired:
        _log.warning("checkpoint snapshot timed out for %s/%s", project_id, session_id)
        return None
    except Exception as e:  # noqa: BLE001 — never break the tool loop
        _log.debug("checkpoint snapshot error: %s", e)
        return None


# --- remote (SSH) projects -------------------------------------------------
# Same undo-net semantics for a workspace on another device: the SHADOW git
# lives on the REMOTE machine (objects next to the files they snapshot — a
# restore is a remote-local `reset --hard`, no bulk transfer), driven over the
# shared SshTransport with explicit `--git-dir/--work-tree` flags (env vars
# don't survive ssh cleanly). The checkpoint LOG stays LOCAL (same
# `adk_cc_checkpoints.json` as local projects) so the restore menu lists
# instantly and survives the remote being unreachable; log and remote store
# are reconciled by sha at restore time. The user's remote `.git` is NEVER
# touched — same guarantee as local, asserted in the e2e.

_REMOTE_GIT_CONF = (
    "-c user.email=checkpoint@adk-cc.local "
    "-c 'user.name=adk-cc checkpoint' "
    "-c commit.gpgsign=false -c gc.auto=0 -c core.hooksPath=/dev/null"
)


def _remote_shadow_dir(home: str, project_id: str, session_id: str) -> str:
    return f"{home.rstrip('/')}/.adk-cc/checkpoints/{project_id}/{session_id}"


async def remote_checkpoint_supported(transport) -> bool:  # noqa: ANN001
    """True when the remote has git (probed, cached). Never raises."""
    try:
        probe = await transport.probe()
        return bool(probe.get("git"))
    except Exception:  # noqa: BLE001
        return False


async def snapshot_remote(
    project_id: str,
    session_id: str,
    transport,  # noqa: ANN001 — SshTransport (typed loosely; service layer)
    work_tree: str,
    *,
    reason: str = "",
    invocation_id: Optional[str] = None,
) -> Optional[str]:
    """Remote analogue of `snapshot()`: snapshot the REMOTE work tree into the
    remote shadow repo; return the sha (or existing HEAD when unchanged).
    Returns None and swallows on ANY failure — a checkpoint must never break
    a tool call. No-op (None) when the remote has no git."""
    import shlex as _sh

    try:
        if not await remote_checkpoint_supported(transport):
            return None
        probe = await transport.probe()
        home = probe.get("home") or ""
        if not home:
            return None
        gd = _sh.quote(_remote_shadow_dir(home, project_id, session_id))
        wt = _sh.quote(work_tree)
        base = f"git --git-dir {gd} --work-tree {wt}"

        # One round trip: init-once (with the belt-and-suspenders /.git/
        # exclude), stage everything.
        res = await transport.run(
            f"[ -f {gd}/HEAD ] || {{ mkdir -p {gd} && git --git-dir {gd} init -q "
            f"&& mkdir -p {gd}/info && printf '/.git/\\n' > {gd}/info/exclude; }} "
            f"&& {base} add -A",
            timeout_s=_ADD_TIMEOUT,
        )
        if res.exit_code != 0:
            _log.debug("remote checkpoint add failed: %s", res.stderr.strip()[:300])
            return None

        # Local log lives in the LOCAL shadow dir (log only, no git there).
        local_gd = _shadow_dir(project_id, session_id)
        local_gd.mkdir(parents=True, exist_ok=True)

        head_res = await transport.run(
            f"{base} rev-parse -q --verify HEAD", timeout_s=_QUERY_TIMEOUT
        )
        head = head_res.stdout.strip() if head_res.exit_code == 0 else ""
        if head:
            quiet = await transport.run(
                f"{base} diff --cached --quiet", timeout_s=_QUERY_TIMEOUT
            )
            if quiet.exit_code == 0:
                # No file change this turn — still log THIS invocation so every
                # turn maps to its own rewind point (same rationale as local).
                _append_checkpoint(local_gd, head, reason, invocation_id)
                return head

        commit = await transport.run(
            f"{base} {_REMOTE_GIT_CONF} commit -q -m {_sh.quote('checkpoint: ' + reason)} "
            f"&& {base} rev-parse HEAD",
            timeout_s=_COMMIT_TIMEOUT,
        )
        if commit.exit_code != 0:
            _log.debug("remote checkpoint commit failed: %s", commit.stderr.strip()[:300])
            return None
        sha = commit.stdout.strip().splitlines()[-1] if commit.stdout.strip() else ""
        if not sha:
            return None
        _append_checkpoint(local_gd, sha, reason, invocation_id)
        return sha
    except Exception as e:  # noqa: BLE001 — never break the tool loop
        _log.debug("remote checkpoint snapshot error: %s", e)
        return None


async def restore_remote(
    project_id: str,
    session_id: str,
    transport,  # noqa: ANN001
    work_tree: str,
    *,
    checkpoint_id: Optional[str] = None,
) -> dict[str, Any]:
    """Remote analogue of `restore()`: pre-snapshot (reversible + removes
    files created since the target), `reset --hard` the remote work tree,
    truncate the local log from the target onward."""
    import shlex as _sh

    local_gd = _shadow_dir(project_id, session_id)
    entries = _read_log(local_gd)
    if not entries:
        return {"status": "no_checkpoints"}
    if checkpoint_id is None:
        target_entry = entries[-1]
    else:
        target_entry = next(
            (e for e in entries if _entry_id(e) == checkpoint_id), None
        )
        if target_entry is None:
            return {"status": "error", "error": f"unknown checkpoint: {checkpoint_id}"}
    target = target_entry["sha"]
    invocation_id = target_entry.get("invocation_id")

    try:
        if not await remote_checkpoint_supported(transport):
            return {"status": "error", "error": "remote has no git — undo unavailable"}
        pre = await snapshot_remote(
            project_id, session_id, transport, work_tree, reason="pre-restore"
        )
        probe = await transport.probe()
        gd = _sh.quote(
            _remote_shadow_dir(probe.get("home") or "", project_id, session_id)
        )
        wt = _sh.quote(work_tree)
        reset = await transport.run(
            f"git --git-dir {gd} --work-tree {wt} {_REMOTE_GIT_CONF} "
            f"reset --hard {_sh.quote(target)}",
            timeout_s=_RESET_TIMEOUT,
        )
        if reset.exit_code != 0:
            return {"status": "error", "error": reset.stderr.strip()[:300]}
        # Drop the rewound checkpoints from the local history (same as local).
        log = _read_log(local_gd)
        tid = _entry_id(target_entry)
        idx = next((i for i, e in enumerate(log) if _entry_id(e) == tid), None)
        if idx is not None:
            _write_log(local_gd, log[:idx])
        return {
            "status": "ok",
            "restored_to": target,
            "pre_restore": pre,
            "invocation_id": invocation_id,
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}


def list_checkpoints(project_id: str, session_id: str) -> list[dict[str, Any]]:
    """Most-recent-first checkpoints for the restore menu. Each carries a unique
    `id` (the git sha is NOT unique — a no-file-change turn reuses a commit), which
    callers pass back to restore()."""
    gd = _shadow_dir(project_id, session_id)
    out = []
    for e in reversed(_read_log(gd)):
        out.append({**e, "id": _entry_id(e)})
    return out


def restore(
    project_id: str,
    session_id: str,
    work_tree: str,
    *,
    checkpoint_id: Optional[str] = None,
) -> dict[str, Any]:
    """Restore the work tree to a checkpoint (default: the most recent one → undo
    the last turn). `checkpoint_id` addresses a specific entry by its unique id
    (NOT its git sha, which can repeat). Snapshots the current state first
    (reversible undo + removes files created since the target), then hard-resets."""
    if not work_tree or not os.path.isdir(work_tree):
        return {"status": "error", "error": "no bound project workspace"}
    gd = _shadow_dir(project_id, session_id)
    if not (gd / "HEAD").exists():
        return {"status": "no_checkpoints"}

    entries = _read_log(gd)
    if checkpoint_id is None:
        if not entries:
            return {"status": "no_checkpoints"}
        target_entry = entries[-1]
    else:
        target_entry = next((e for e in entries if _entry_id(e) == checkpoint_id), None)
        if target_entry is None:
            return {"status": "error", "error": f"unknown checkpoint: {checkpoint_id}"}
    target = target_entry["sha"]
    invocation_id = target_entry.get("invocation_id")

    try:
        # Capture the pre-restore state so files created since `target` become
        # tracked → removed by the reset. (Its log entry is dropped just below.)
        pre = snapshot(project_id, session_id, work_tree, reason="pre-restore")
        reset = _run_git(
            _GIT_CONF + ["reset", "--hard", target],
            gd,
            work_tree,
            timeout=_RESET_TIMEOUT,
        )
        if reset.returncode != 0:
            return {"status": "error", "error": reset.stderr.strip()[:300]}
        # Drop the rewound checkpoints from the history: the target and everything
        # after it (including the pre-restore snapshot) correspond to turns we just
        # undid, so the menu shouldn't list them. Repeated undo then steps back turn
        # by turn, and the history reflects only the surviving conversation.
        log = _read_log(gd)
        tid = _entry_id(target_entry)
        idx = next((i for i, e in enumerate(log) if _entry_id(e) == tid), None)
        if idx is not None:
            _write_log(gd, log[:idx])
        # invocation_id lets the caller also roll the conversation back to this turn.
        return {"status": "ok", "restored_to": target, "pre_restore": pre, "invocation_id": invocation_id}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "restore timed out"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)}
