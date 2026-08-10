"""File uploads: a user file becomes `uploads/<name>` in the session workspace.

Design (analysis/file-upload-plan.md): one delivery core, every mode. The
file lands where the agent's fs tools AND sandbox code both see it, via the
same per-backend write primitives the agent itself uses — host write for
noop/local_container (bind mount makes it visible inside), ssh transport for
remote projects, daemon/proxy APIs for docker/daytona/sandbox_service. Blobs
never enter session events; the client tells the model about the file with a
plain-text line in the next message.

Safety model: the destination is FIXED (`uploads/` directly under the
workspace root) and the name is a single validated path component — no
extension allow-list (this deliberately IS the general upload endpoint,
unlike datasets), because a sanitized single component under a fixed subdir
cannot touch workspace config, dotfiles, or anything outside the root.

Ordering: `ensure_workspace` runs here, NOT lazily — an upload can arrive
before the session's first turn, and docker/daytona raise until the
workspace exists (tenancy normally brings it up at the first tool call).
"""
from __future__ import annotations

import logging
import os
import shlex
from typing import Any

from fastapi import HTTPException, Request

_log = logging.getLogger(__name__)

UPLOADS_DIR = "uploads"

class UploadError(Exception):
    """Delivery failure with an HTTP-shaped status for the routes."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def upload_max_bytes() -> int:
    try:
        mb = float(os.environ.get("ADK_CC_UPLOAD_MAX_MB", "100"))
    except ValueError:
        mb = 100.0
    return int(mb * 1024 * 1024)


def check_upload_name(name: str) -> str:
    """Validate an upload filename. Returns the bare name (never a path).

    STRUCTURAL guards only, no charset allow-list: real files are called
    "report (1).csv" and "売上データ.xlsx", and every consumer of the name
    quotes or encodes it (shlex for exec probes, tar member names, URL
    encoding for the daytona/sandbox_service proxies). What stays banned:
    path separators, leading dot (workspace dotfiles) and dash (argv-flag
    lookalike, defense in depth), control characters, absurd length.
    """
    name = (name or "").strip()
    if not name:
        raise UploadError(400, "a filename is required")
    if "/" in name or "\\" in name:
        raise UploadError(400, f"unsafe filename: {name!r} — no path separators")
    if name.startswith(".") or name.startswith("-"):
        raise UploadError(
            400, f"unsafe filename: {name!r} — must not start with '.' or '-'")
    if any(ord(c) < 32 or ord(c) == 0x7F for c in name):
        raise UploadError(
            400, f"unsafe filename: {name!r} — control characters not allowed")
    if len(name) > 150:
        raise UploadError(400, f"filename too long ({len(name)} chars, max 150)")
    return name


async def _exists(ws, backend, rel_path: str, dest: str) -> bool:  # noqa: ANN001
    """Does the upload target already exist, as seen by THIS backend?

    Host check when file IO is host-direct (NoopBackend family — includes
    local_container, whose mount mirrors the host dir). Otherwise one exec
    probe inside the sandbox — with the workspace-RELATIVE path, because the
    backend translates only the cwd, and the host spelling of `dest` does
    not exist inside a container namespace (the same reason the code
    executor hands the sandbox relative paths). Probe failures count as
    "absent" — the write itself still enforces every real boundary, and
    blocking an upload on a broken probe helps nobody.
    """
    from ..sandbox.backends.noop_backend import NoopBackend

    if isinstance(backend, NoopBackend):
        return os.path.exists(dest)
    try:
        from ..sandbox.config import NetworkConfig

        res = await backend.exec(
            f"test -e {shlex.quote(rel_path)}",
            fs_write=ws.fs_write_config(),
            network=NetworkConfig(),
            timeout_s=30,
            cwd=ws.abs_path,
        )
        return getattr(res, "exit_code", 1) == 0
    except Exception as e:  # noqa: BLE001 — probe only; the write still gates
        _log.debug("upload existence probe failed (%s); assuming absent", e)
        return False


async def deliver_upload(
    ws,  # noqa: ANN001 — WorkspaceRoot
    backend,  # noqa: ANN001 — SandboxBackend
    name: str,
    data: bytes,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Land `data` at `uploads/<name>` in `ws` through `backend`.

    Returns {name, rel_path, bytes}. Raises UploadError with an HTTP-shaped
    status (400 bad name/empty, 409 exists, 413 too large).
    """
    name = check_upload_name(name)
    if not data:
        raise UploadError(400, "empty body")
    cap = upload_max_bytes()
    if len(data) > cap:
        raise UploadError(
            413,
            f"upload is {len(data) / 1024 / 1024:.1f}MB, over the "
            f"{cap / 1024 / 1024:.0f}MB limit "
            f"(raise ADK_CC_UPLOAD_MAX_MB to allow more)",
        )

    # Idempotent; normally lazy at the first tool call, but an upload may
    # be the very first thing a session does.
    await backend.ensure_workspace(ws)

    root = (ws.abs_path or "").rstrip("/")
    rel_path = f"{UPLOADS_DIR}/{name}"
    dest = f"{root}/{rel_path}"

    if not overwrite and await _exists(ws, backend, rel_path, dest):
        raise UploadError(
            409,
            f"{rel_path} already exists — re-send with overwrite=1 to "
            "replace it, or pick another name",
        )

    await backend.write_bytes(dest, data, fs_write=ws.fs_write_config())
    _log.info("upload delivered: %s (%d bytes) via %s",
              rel_path, len(data), getattr(backend, "name", type(backend).__name__))
    return {"name": name, "rel_path": rel_path, "bytes": len(data)}


async def read_capped_body(request: Request) -> bytes:
    """Read the request body, 413ing from the Content-Length header BEFORE
    buffering when the client declares a too-large payload (the skills
    routes' check-after-buffer is a known wart)."""
    cap = upload_max_bytes()
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) > cap:
            raise UploadError(
                413,
                f"upload exceeds the {cap / 1024 / 1024:.0f}MB limit "
                f"(raise ADK_CC_UPLOAD_MAX_MB to allow more)",
            )
    except ValueError:
        pass
    except UploadError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return await request.body()


def mount_upload_routes(app) -> None:  # noqa: ANN001
    """`PUT /api/uploads/{name}` for WEB deployments (desktop mounts its own
    under /desktop/uploads, beside the other project-bound routes).

    Auth: the route is NOT in the exempt lists, so the auth middleware gates
    it in real web deployments; the principal supplies tenant/user. In
    no-auth dev runs the ids come from query params, mirroring how ADK's
    own dev routes carry them in the path.
    """
    from .. import deployment

    if deployment.is_desktop():
        return

    @app.put("/api/uploads/{name}", include_in_schema=False)
    async def upload_file(name: str, request: Request):  # noqa: ANN202
        from ..sandbox import make_default_backend
        from .tenancy import resolve_default_tenant

        q = request.query_params
        session_id = q.get("session_id") or ""
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id required")
        auth = getattr(request.state, "adk_cc_auth", None)
        user_id = (getattr(auth, "user_id", None) if auth is not None
                   else q.get("user_id")) or "local"

        # The EXACT resolution the TenancyPlugin performs — same workspace,
        # same backend family, so the file lands where the agent looks.
        ctx = resolve_default_tenant(user_id)
        try:
            ws = ctx.workspace(session_id)
        except ValueError as e:  # unsafe ids
            raise HTTPException(status_code=400, detail=str(e))
        backend = make_default_backend(
            session_id=session_id, tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )

        blob = await read_capped_body(request)
        overwrite = (q.get("overwrite") or "").lower() in ("1", "true")
        try:
            row = await deliver_upload(ws, backend, name, blob,
                                       overwrite=overwrite)
        except UploadError as e:
            raise HTTPException(status_code=e.status, detail=str(e))
        return {"status": "ok", "upload": row}
