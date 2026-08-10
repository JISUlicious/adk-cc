"""Desktop file-tree + file-read routes (ADK_CC_DESKTOP=1).

Read-only view of a session's workspace for the desktop right-side file panel.
In in-place desktop mode that workspace IS the project's repo root, so the panel
shows exactly where the agent works. Two routes, both strictly scoped to that
root via a resolve()-based path guard that rejects any target escaping it (via
``..`` OR a symlink pointing outside). Mounted only when ADK_CC_DESKTOP=1;
desktop is a single-user loopback service (no auth), so these are self-scoped by
project + session id, both validated against the project registry.

Read-only by design: no write/rename/delete. Viewing must NOT create anything,
so it uses ``session_workspace_path`` (non-creating): it returns the bound
project root, or None when no repo is bound.
"""

from __future__ import annotations

import logging
import mimetypes
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

_log = logging.getLogger(__name__)

_MAX_READ = 1024 * 1024  # 1 MiB — cap file reads so the panel can't pull a huge blob
_MAX_RAW = 25 * 1024 * 1024  # raw-bytes route (image/PDF viewers, downloads)
_MAX_ENTRIES = 2000       # per-directory entry cap (keeps huge dirs responsive)
_STATUS_TIMEOUT = 10      # wall-clock cap for the `git status` behind change markers


def _safe(value: str, label: str) -> str:
    """Reject a project/session id that isn't plain alnum/-/_ (defense in depth;
    the real containment guard is _resolve_within's root check)."""
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if not safe or safe != value:
        raise HTTPException(status_code=400, detail=f"unsafe {label}: {value!r}")
    return safe


def _resolve_within(project_id: str, session_id: str, rel: str) -> Optional[Path]:
    """Absolute path for ``rel`` inside the session's workspace (in-place: the
    project root).

    Returns None when no project repo is bound. Raises 404 for an unknown
    project, 403 for a path that escapes the workspace root. Both root and target
    are ``.resolve()``d, so ``..`` is collapsed and symlinks are followed before
    the containment check — a symlink inside the workspace pointing outside is
    rejected.
    """
    from .desktop_routes import load_projects
    from .desktop_workspace import session_workspace_path

    project_id = _safe(project_id, "project_id")
    session_id = _safe(session_id, "session_id")
    if not any(p.get("id") == project_id for p in load_projects()):
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")

    # Root at the session's actual workspace (in-place: the project root), so the
    # file panel shows exactly where the agent works. Mirrors the tenant resolver.
    ws = session_workspace_path(project_id, session_id)
    if ws is None or not ws.is_dir():
        return None  # no bound project workspace
    root = ws.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=403, detail="path escapes workspace")
    return target


def _coarse_status(xy: str) -> str:
    """Collapse a git porcelain XY status pair into one coarse marker.

    `new` covers untracked (`??`) and staged-add (`A`); `deleted`, `renamed`
    (only when rename detection is on — we run with --no-renames, so a move
    surfaces as delete + new), else `modified` (M/T/C/…). Staged and unstaged
    are merged: a file changed vs HEAD is "changed", regardless of the index.
    """
    if xy == "??":
        return "new"
    x, y = xy[0], xy[1]
    if x == "A" or y == "A":
        return "new"
    if x == "D" or y == "D":
        return "deleted"
    if x == "R" or y == "R":
        return "renamed"
    return "modified"


def _parse_porcelain_z(out: str, prefix: str) -> dict[str, str]:
    """Parse `git status --porcelain=v1 -z --no-renames` output into
    `{workspace_rel_path: coarse_status}`, stripping the repo→workspace
    `prefix`. Shared by the local and remote (SSH) status paths."""
    statuses: dict[str, str] = {}
    for token in out.split("\0"):
        # Each record is "XY<space>path"; the trailing split yields "".
        if len(token) < 4:
            continue
        xy, path = token[:2], token[3:]
        if prefix:
            if not path.startswith(prefix):
                continue  # change outside the workspace subtree
            path = path[len(prefix):]
        statuses[path] = _coarse_status(xy)
    return statuses


def _git_working_status(root: Path) -> tuple[bool, dict[str, str]]:
    """`(is_repo, {workspace_rel_path: status})` for the workspace subtree.

    Reads the PROJECT'S OWN working-tree status (the same thing a git client
    shows as uncommitted changes) — the checkpoint shadow git is a separate
    GIT_DIR and is never involved. `status` ∈ {new, modified, deleted,
    renamed}. Paths are workspace-relative with POSIX separators, matching the
    file-tree entries. Best-effort: any failure (not a repo, git missing,
    timeout) yields no markers rather than an error.
    """
    base = ["git", "-C", str(root)]
    try:
        # `--show-prefix` both proves this is a repo AND gives our subdir
        # offset when the workspace root sits below the repo root (git prints
        # status paths relative to the REPO root, so we strip the prefix to
        # get workspace-relative paths).
        pref = subprocess.run(
            base + ["rev-parse", "--show-prefix"],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT,
        )
        if pref.returncode != 0:
            return False, {}  # not a git work tree
        prefix = pref.stdout.strip()  # "" at repo root, else "sub/dir/"
        # -z: NUL-delimited, no path quoting. --no-renames: a move shows as
        # D old + ?? new, so every record is a single path (no dual-field
        # rename entries to parse). `-- .` scopes to the workspace subtree.
        res = subprocess.run(
            base
            + [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
                "--",
                ".",
            ],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT,
        )
        if res.returncode != 0:
            return True, {}  # a repo, but status failed — no markers
        return True, _parse_porcelain_z(res.stdout, prefix)
    except (OSError, subprocess.SubprocessError):
        return False, {}


# --- remote (SSH) projects -------------------------------------------------
# The panel serves REMOTE projects over the same shared per-host transport the
# agent's SshBackend uses (one ControlMaster per remote). Same read-only
# contract, same response shapes; sizes are None (a portable remote `ls`
# doesn't give them cheaply) — the tree doesn't render sizes anyway.


def _remote_ctx(project_id: str):
    """`(transport, remote_root)` for a remote project, else None."""
    from .desktop_routes import project_remote
    from ..sandbox.ssh_transport import get_transport

    r = project_remote(project_id)
    if not r:
        return None
    t = get_transport(str(r["host"]), port=r.get("port") or None)
    return t, str(r["path"]).rstrip("/") or "/"


def _remote_ctx_checked(project_id: str, session_id: str):
    """Validated variant for the routes: id hygiene + unknown-project 404
    (mirroring `_resolve_within`), then the remote ctx or None (local)."""
    from .desktop_routes import load_projects

    project_id = _safe(project_id, "project_id")
    _safe(session_id, "session_id")
    if not any(p.get("id") == project_id for p in load_projects()):
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    return _remote_ctx(project_id)


def _remote_target(root: str, rel: str) -> str:
    """Lexical containment guard for remote paths (no local fs, so no
    realpath): normalize root+rel and reject escapes — mirrors
    `_resolve_within`'s contract."""
    import posixpath

    target = posixpath.normpath(posixpath.join(root, rel)) if rel else root
    if target != root and not target.startswith(root + "/"):
        raise HTTPException(status_code=403, detail="path escapes workspace")
    return target


async def _remote_tree(t, root: str, rel: str) -> dict:  # noqa: ANN001
    from ..sandbox.ssh_transport import SshConnectionError

    target = _remote_target(root, rel)
    try:
        # cwd-based `ls` so a missing/non-dir target is the cd's exit 96.
        res = await t.run("ls -1Ap", cwd=target, timeout_s=15)
    except SshConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if res.exit_code == 96:
        if rel:
            raise HTTPException(status_code=400, detail="not a directory")
        # Root not there yet — same empty state as a local unbound project.
        return {"root_exists": False, "path": rel, "entries": [], "truncated": False}
    if res.exit_code != 0:
        raise HTTPException(
            status_code=502, detail=f"remote ls failed: {res.stderr[:200]}"
        )
    dirs: list[dict] = []
    files: list[dict] = []
    for line in res.stdout.splitlines():
        name = line.rstrip("\n")
        if not name:
            continue
        is_dir = name.endswith("/")
        name = name.rstrip("/")
        if name == ".git":
            continue  # repo noise, mirrored from the local branch
        (dirs if is_dir else files).append(
            {"name": name, "type": "dir" if is_dir else "file", "size": None}
        )
    entries = sorted(dirs, key=lambda e: e["name"].lower()) + sorted(
        files, key=lambda e: e["name"].lower()
    )
    truncated = len(entries) > _MAX_ENTRIES
    return {
        "root_exists": True,
        "path": rel,
        "entries": entries[:_MAX_ENTRIES],
        "truncated": truncated,
    }


async def _remote_read(t, root: str, rel: str) -> dict:  # noqa: ANN001
    from ..sandbox.ssh_transport import SshConnectionError

    target = _remote_target(root, rel)
    try:
        size_res = await t.run(f"wc -c < {_shq(target)}", timeout_s=15)
        if size_res.exit_code != 0:
            raise HTTPException(status_code=404, detail="not a file")
        try:
            size = int(size_res.stdout.strip())
        except ValueError:
            raise HTTPException(status_code=404, detail="not a file")
        raw = await t.read_file(target, max_bytes=_MAX_READ)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not a file")
    except SshConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    truncated = size > _MAX_READ
    mime, _ = mimetypes.guess_type(target.rsplit("/", 1)[-1])
    try:
        text: Optional[str] = raw.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = None
        binary = True
    return {
        "path": rel,
        "mime": mime or "application/octet-stream",
        "size": size,
        "truncated": truncated,
        "text": text,
        "binary": binary,
    }


async def _remote_status(t, root: str) -> dict:  # noqa: ANN001
    from ..sandbox.ssh_transport import SshConnectionError

    try:
        probe = await t.probe()
        if not probe.get("git"):
            return {"is_repo": False, "statuses": {}}
        pref = await t.run(
            f"git -C {_shq(root)} rev-parse --show-prefix", timeout_s=_STATUS_TIMEOUT
        )
        if pref.exit_code != 0:
            return {"is_repo": False, "statuses": {}}
        prefix = pref.stdout.strip()
        res = await t.run(
            f"git -C {_shq(root)} status --porcelain=v1 -z "
            f"--untracked-files=all --no-renames -- .",
            cwd=root,
            timeout_s=_STATUS_TIMEOUT,
        )
        if res.exit_code != 0:
            return {"is_repo": True, "statuses": {}}
        return {"is_repo": True, "statuses": _parse_porcelain_z(res.stdout, prefix)}
    except SshConnectionError:
        # Unreachable → no markers, not an error (panel stays usable).
        return {"is_repo": False, "statuses": {}}


def _shq(s: str) -> str:
    import shlex

    return shlex.quote(s)


# --- local serving cores (shared by the desktop and web mounts) -------------

def _dir_listing(target: Path, rel: str) -> dict:
    entries: list[dict] = []
    truncated = False
    # Dirs first, then files, each case-insensitively sorted.
    for child in sorted(
        target.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())
    ):
        if child.name == ".git":
            continue  # the repo's .git dir is noise in the file panel
        is_dir = child.is_dir()
        try:
            size = None if is_dir else child.stat().st_size
        except OSError:
            size = None
        entries.append(
            {"name": child.name, "type": "dir" if is_dir else "file", "size": size}
        )
        if len(entries) >= _MAX_ENTRIES:
            truncated = True
            break
    return {"root_exists": True, "path": rel, "entries": entries,
            "truncated": truncated}


def _read_payload(target: Path, rel: str) -> dict:
    size = target.stat().st_size
    raw = target.read_bytes()[:_MAX_READ]
    truncated = size > _MAX_READ
    mime, _ = mimetypes.guess_type(target.name)
    try:
        text: Optional[str] = raw.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        text = None
        binary = True
    return {
        "path": rel,
        "mime": mime or "application/octet-stream",
        "size": size,
        "truncated": truncated,
        "text": text,
        "binary": binary,
    }


def _raw_response(target: Path, name: str, as_download: bool):  # noqa: ANN201
    from urllib.parse import quote as _urlquote

    from fastapi.responses import FileResponse

    if not target.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    if target.stat().st_size > _MAX_RAW:
        raise HTTPException(status_code=413,
                            detail=f"file exceeds the "
                                   f"{_MAX_RAW // (1024 * 1024)}MB view limit")
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    disposition = (
        f"{'attachment' if as_download else 'inline'}; "
        f"filename*=UTF-8''{_urlquote(name)}"
    )
    return FileResponse(target, media_type=mime,
                        headers={"Content-Disposition": disposition})


def mount_web_file_routes(app) -> None:  # noqa: ANN001
    """`/api/files/{tree,read,raw}` for WEB deployments (no-op on desktop).

    The web shell's Files panel + viewers. Workspace resolved through the
    SAME tenant path the agent (and /api/uploads) uses — auth principal
    wins; in no-auth dev the ids ride as query params. Local-fs only: web
    workspaces live under ADK_CC_WORKSPACE_ROOT on the server host (docker
    bind-mounts the same dir), which is exactly what the tenancy layer
    `os.makedirs`'s. Same guards and caps as the desktop routes.
    """
    from .. import deployment

    if deployment.is_desktop():
        return

    def _web_root_and_target(request: Request) -> tuple[Path, Path, str]:
        from .tenancy import resolve_default_tenant

        q = request.query_params
        session_id = q.get("session_id") or ""
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id required")
        auth = getattr(request.state, "adk_cc_auth", None)
        user_id = (getattr(auth, "user_id", None) if auth is not None
                   else q.get("user_id")) or "local"
        try:
            ws = resolve_default_tenant(user_id).workspace(session_id)
        except ValueError as e:  # unsafe ids
            raise HTTPException(status_code=400, detail=str(e))
        root = Path(ws.abs_path).resolve()
        rel = q.get("path") or ""
        target = (root / rel).resolve() if rel else root
        if target != root and root not in target.parents:
            raise HTTPException(status_code=403, detail="path escapes workspace")
        return root, target, rel

    @app.get("/api/files/tree", include_in_schema=False)
    async def web_files_tree(request: Request):  # noqa: ANN202
        _, target, rel = _web_root_and_target(request)
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="not a directory")
        return _dir_listing(target, rel)

    @app.get("/api/files/read", include_in_schema=False)
    async def web_files_read(request: Request):  # noqa: ANN202
        _, target, rel = _web_root_and_target(request)
        if not rel or not target.is_file():
            raise HTTPException(status_code=404, detail="not a file")
        return _read_payload(target, rel)

    @app.get("/api/files/raw", include_in_schema=False)
    async def web_files_raw(request: Request):  # noqa: ANN202
        _, target, rel = _web_root_and_target(request)
        if not rel:
            raise HTTPException(status_code=404, detail="not a file")
        as_download = (request.query_params.get("download") or "") in ("1", "true")
        return _raw_response(target, rel.rsplit("/", 1)[-1], as_download)


def mount_desktop_files_routes(app) -> None:  # noqa: ANN001
    """Mount /desktop/files/* when ADK_CC_DESKTOP=1; otherwise a no-op."""
    from .desktop_routes import desktop_enabled

    if not desktop_enabled():
        return

    @app.get("/desktop/files/tree", include_in_schema=False)
    async def files_tree(request: Request):  # noqa: ANN202
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        rel = q.get("path") or ""
        if not project_id or not session_id:
            raise HTTPException(status_code=400, detail="project_id and session_id required")

        rc = _remote_ctx_checked(project_id, session_id)
        if rc:
            return await _remote_tree(rc[0], rc[1], rel)

        target = _resolve_within(project_id, session_id, rel)
        if target is None:
            # No project repo bound yet — empty state, not an error.
            return {"root_exists": False, "path": rel, "entries": [], "truncated": False}
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="not a directory")
        return _dir_listing(target, rel)

    @app.get("/desktop/files/status", include_in_schema=False)
    async def files_status(request: Request):  # noqa: ANN202
        """Whole-workspace git working-tree status → change markers in the
        file panel. One call per reload/turn (git status is a repo-wide op);
        the client looks each tree entry up in the returned map. Empty +
        ``is_repo=false`` when the workspace root isn't a git work tree."""
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        if not project_id or not session_id:
            raise HTTPException(status_code=400, detail="project_id and session_id required")

        rc = _remote_ctx_checked(project_id, session_id)
        if rc:
            return await _remote_status(rc[0], rc[1])

        root = _resolve_within(project_id, session_id, "")
        if root is None or not root.is_dir():
            return {"is_repo": False, "statuses": {}}
        is_repo, statuses = _git_working_status(root)
        return {"is_repo": is_repo, "statuses": statuses}

    @app.get("/desktop/files/read", include_in_schema=False)
    async def files_read(request: Request):  # noqa: ANN202
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        rel = q.get("path") or ""
        if not project_id or not session_id or not rel:
            raise HTTPException(status_code=400, detail="project_id, session_id, path required")

        rc = _remote_ctx_checked(project_id, session_id)
        if rc:
            return await _remote_read(rc[0], rc[1], rel)

        target = _resolve_within(project_id, session_id, rel)
        if target is None:
            raise HTTPException(status_code=404, detail="workspace not initialized")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="not a file")
        return _read_payload(target, rel)

    # Raw bytes with a real Content-Type — what the panel's image/PDF/media
    # viewers and the Download button consume (the JSON read route above can
    # only say "binary — not shown"). Same guards as /read; desktop is
    # no-auth loopback, so a plain <img src>/<iframe src> URL works.
    @app.get("/desktop/files/raw", include_in_schema=False)
    async def files_raw(request: Request):  # noqa: ANN202
        from urllib.parse import quote as _urlquote

        from fastapi.responses import FileResponse, Response

        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        rel = q.get("path") or ""
        if not project_id or not session_id or not rel:
            raise HTTPException(status_code=400,
                                detail="project_id, session_id, path required")
        as_download = (q.get("download") or "") in ("1", "true")
        name = rel.rsplit("/", 1)[-1]
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        disposition = (
            f"{'attachment' if as_download else 'inline'}; "
            f"filename*=UTF-8''{_urlquote(name)}"
        )

        rc = _remote_ctx_checked(project_id, session_id)
        if rc:
            from ..sandbox.ssh_transport import SshConnectionError

            target = _remote_target(rc[1], rel)
            try:
                raw = await rc[0].read_file(target, max_bytes=_MAX_RAW + 1)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="not a file")
            except SshConnectionError as e:
                raise HTTPException(status_code=502, detail=str(e))
            if len(raw) > _MAX_RAW:
                raise HTTPException(status_code=413,
                                    detail=f"file exceeds the "
                                           f"{_MAX_RAW // (1024 * 1024)}MB view limit")
            return Response(raw, media_type=mime,
                            headers={"Content-Disposition": disposition})

        target = _resolve_within(project_id, session_id, rel)
        if target is None:
            raise HTTPException(status_code=404, detail="workspace not initialized")
        return _raw_response(target, name, as_download)


# --- datasets (W5 ingestion) ------------------------------------------------

def mount_desktop_upload_routes(app) -> None:  # noqa: ANN001
    """`PUT /desktop/uploads/{name}` — general file upload into the bound
    project's workspace (ADK_CC_DESKTOP=1 only; no-op otherwise).

    Unlike the dataset routes this does NOT refuse remote/containerized
    workspaces: delivery goes through the SAME (resolver, backend) pair the
    TenancyPlugin gives the agent, so an SSH project's file lands on the
    remote host and a container backend's inside the sandbox
    (analysis/file-upload-plan.md)."""
    from .desktop_routes import desktop_enabled

    if not desktop_enabled():
        return

    from .uploads import UploadError, deliver_upload, read_capped_body

    @app.put("/desktop/uploads/{name}", include_in_schema=False)
    async def upload_file(name: str, request: Request):  # noqa: ANN202
        from .desktop_routes import load_projects
        from .desktop_workspace import (
            desktop_backend_factory,
            desktop_tenant_resolver,
        )

        q = request.query_params
        project_id = _safe(q.get("project_id") or "", "project_id")
        session_id = _safe(q.get("session_id") or "", "session_id")
        if not any(p.get("id") == project_id for p in load_projects()):
            raise HTTPException(status_code=404,
                                detail=f"unknown project: {project_id}")

        ctx = desktop_tenant_resolver(project_id)
        ws = ctx.workspace(session_id)
        backend = desktop_backend_factory(ctx, session_id)

        blob = await read_capped_body(request)
        overwrite = (q.get("overwrite") or "").lower() in ("1", "true")
        try:
            row = await deliver_upload(ws, backend, name, blob,
                                       overwrite=overwrite)
        except UploadError as e:
            raise HTTPException(status_code=e.status, detail=str(e))
        return {"status": "ok", "upload": row}


def mount_desktop_dataset_routes(app) -> None:  # noqa: ANN001
    """Mount /desktop/datasets/* when ADK_CC_DESKTOP=1; otherwise a no-op.

    Lives beside the file routes because it shares their workspace resolution:
    a dataset must land where the AGENT reads, which on desktop is the bound
    project root, not a server-side upload area.
    """
    from .desktop_routes import desktop_enabled

    if not desktop_enabled():
        return

    from . import datasets as ds

    # (path, mtime_ns, size) -> profile. Profiling costs a sandbox round trip
    # and the first one may provision the env; re-listing a panel must not.
    _PROFILE_CACHE: dict = {}

    def _workspace_is_local(request: Request) -> Optional[str]:
        """Why this workspace is NOT on the server's filesystem, or None.

        The datasets/profile/env routes all read the workspace with plain
        `pathlib`, which is right for desktop's in-place local project and
        WRONG the moment the workspace lives elsewhere: an SSH project's files
        are on another machine, and a container backend's are inside the
        sandbox. Reading the host path then reports "no datasets" / "env not
        built" with total confidence — the worst kind of wrong. Say so instead.
        """
        pid = request.query_params.get("project_id") or ""
        try:
            if pid and _remote_ctx(_safe(pid, "project_id")) is not None:
                return "this project runs over SSH; its files are on the remote host"
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — detection must not break the route
            pass
        try:
            from ..sandbox import _get_default_backend, is_noop_backend

            backend = _get_default_backend()
            if not is_noop_backend(backend):
                name = getattr(backend, "name", type(backend).__name__)
                return f"the workspace lives inside the {name} sandbox"
        except Exception:  # noqa: BLE001
            pass
        return None

    def _root(request: Request) -> Path:
        q = request.query_params
        project_id = q.get("project_id") or ""
        session_id = q.get("session_id") or ""
        if not project_id or not session_id:
            raise HTTPException(status_code=400,
                                detail="project_id and session_id required")
        root = _resolve_within(project_id, session_id, "")
        if root is None:
            raise HTTPException(status_code=409, detail="no workspace bound to this project")
        return root

    @app.get("/desktop/datasets", include_in_schema=False)
    async def list_datasets(request: Request):  # noqa: ANN202
        remote = _workspace_is_local(request)
        if remote:
            # An empty list here would read as "no datasets", which is a
            # different and much more misleading statement.
            return {"datasets": [], "location": ds.DATA_DIR,
                    "supported": list(ds.supported()), "max_bytes": ds.max_bytes(),
                    "unavailable": remote}
        root = _root(request)
        return {
            "datasets": ds.listing(root),
            "location": ds.DATA_DIR,
            "supported": list(ds.supported()),
            "max_bytes": ds.max_bytes(),
        }

    @app.post("/desktop/datasets/from-path", include_in_schema=False)
    async def add_dataset_from_path(request: Request):  # noqa: ANN202
        """Ingest a LOCAL file the user picked (desktop is single-user loopback,
        so the server may read the chosen path — same trust model as adding a
        project folder)."""
        remote = _workspace_is_local(request)
        if remote:
            raise HTTPException(status_code=409,
                                detail=f"cannot place a dataset from here: {remote}")
        root = _root(request)
        body = await request.json() or {}
        try:
            row = ds.ingest_local_path(root, str(body.get("path") or ""),
                                       name=str(body.get("name") or ""))
        except ds.DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "ok", "dataset": row}

    @app.put("/desktop/datasets/{name}", include_in_schema=False)
    async def upload_dataset(name: str, request: Request):  # noqa: ANN202
        remote = _workspace_is_local(request)
        if remote:
            raise HTTPException(status_code=409,
                                detail=f"cannot place a dataset from here: {remote}")
        root = _root(request)
        blob = await request.body()
        if not blob:
            raise HTTPException(status_code=400, detail="empty body")
        try:
            row = ds.write_bytes(root, name, blob)
        except ds.DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "ok", "dataset": row}

    @app.get("/desktop/analysis-env", include_in_schema=False)
    async def analysis_env_status(request: Request):  # noqa: ANN202
        """State of the uv-managed analysis runtime for this workspace.

        Read-only by construction — polling must never kick off a 60s install.
        """
        from ..sandbox.analysis_env import status as env_status

        remote = _workspace_is_local(request)
        if remote:
            # "absent" would claim the env is not built; we simply cannot see it.
            return {"state": "unknown", "detail": remote}
        return env_status(str(_root(request)))

    @app.get("/desktop/datasets/{name}/profile", include_in_schema=False)
    async def profile_dataset(name: str, request: Request):  # noqa: ANN202
        """Shape, dtypes, null counts and head — what an analyst checks first.

        Runs in the SAME uv-managed analysis env the agent uses (W1), so a
        profile that works here works in the turn. Bounded: parquet reads
        metadata, text formats read a sample and count newlines. Cached on
        (path, mtime, size) because the first call may provision the env.
        """
        remote = _workspace_is_local(request)
        if remote:
            raise HTTPException(status_code=409,
                                detail=f"cannot profile from here: {remote}")
        root = _root(request)
        try:
            dest = ds.target_path(root, name)
        except ds.DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not dest.is_file():
            raise HTTPException(status_code=404, detail=f"no such dataset: {name}")

        st = dest.stat()
        key = (str(dest), st.st_mtime_ns, st.st_size)
        hit = _PROFILE_CACHE.get(key)
        if hit is not None:
            return {"status": "ok", "profile": hit, "cached": True}

        from ..sandbox import _get_default_backend
        from ..sandbox.config import NetworkConfig
        from ..sandbox.workspace import WorkspaceRoot
        from ..sandbox.analysis_env import ensure_env

        backend = _get_default_backend()
        ws = WorkspaceRoot(tenant_id="local",
                           session_id=request.query_params.get("session_id") or "profile",
                           abs_path=str(root))
        try:
            env = await ensure_env(backend, ws, tiers=("core",))
        except Exception as e:  # noqa: BLE001 — provisioning is the likely failure
            raise HTTPException(status_code=503,
                                detail=f"analysis runtime unavailable: {e}")
        cmd = ds.profile_command(f"{ds.DATA_DIR}/{dest.name}", python=env.python)
        try:
            res = await backend.exec(cmd, fs_write=ws.fs_write_config(),
                                     network=NetworkConfig(), timeout_s=240,
                                     cwd=str(root))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"profiling failed: {e}")
        prof = ds.parse_profile(getattr(res, "stdout", "") or "")
        if prof is None:
            # Never report a half-run profiler as a profile.
            raise HTTPException(
                status_code=500,
                detail=(getattr(res, "stderr", "") or "profiler produced no output")[-400:])
        if len(_PROFILE_CACHE) > 64:
            _PROFILE_CACHE.clear()
        _PROFILE_CACHE[key] = prof
        return {"status": "ok", "profile": prof, "cached": False}

    @app.delete("/desktop/datasets/{name}", include_in_schema=False)
    async def delete_dataset(name: str, request: Request):  # noqa: ANN202
        remote = _workspace_is_local(request)
        if remote:
            raise HTTPException(status_code=409,
                                detail=f"cannot delete from here: {remote}")
        root = _root(request)
        try:
            gone = ds.remove(root, name)
        except ds.DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"status": "deleted" if gone else "not_found", "name": name}
