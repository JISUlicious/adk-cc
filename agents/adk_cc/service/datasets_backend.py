"""Serve the dataset panel THROUGH the sandbox backend (#75).

The dataset/profile/env routes read the workspace with plain pathlib — right
for desktop's in-place local project, impossible for an SSH-bound project or
a container/daytona backend, where the files live elsewhere. Since fd5d379
those cases were DETECTED and refused honestly; this module serves them
instead, over the SAME (workspace, backend) pair the TenancyPlugin hands the
agent — so what the panel shows is by construction what the agent sees.

Contract notes (mirrors `service/uploads.py`):
* exec commands use workspace-RELATIVE paths with `cwd=ws.abs_path` — the
  backend translates only the cwd, and the host spelling of a path does not
  exist inside a container namespace;
* writes use the host spelling (`<root>/data/<name>`) via `backend.write_bytes`,
  which every backend translates and which creates parent dirs (P0-proven);
* a backend that cannot answer raises `WorkspaceUnreachable` — callers must
  report that, never an empty listing.
"""
from __future__ import annotations

import logging
import shlex
import time
from typing import Any, Optional

from . import datasets as ds

_log = logging.getLogger(__name__)


class WorkspaceUnreachable(Exception):
    """The backend could not answer. Say so — an empty list would read as
    "no datasets", which is a different and much more misleading statement."""


async def _exec(ws, backend, cmd: str, *, timeout_s: int = 60):  # noqa: ANN001
    from ..sandbox.config import NetworkConfig

    return await backend.exec(
        cmd, fs_write=ws.fs_write_config(), network=NetworkConfig(),
        timeout_s=timeout_s, cwd=ws.abs_path)


async def listing_via(ws, backend) -> list[dict[str, Any]]:  # noqa: ANN001
    try:
        res = await _exec(ws, backend, ds.rows_command())
    except Exception as e:  # noqa: BLE001 — every backend fails differently
        raise WorkspaceUnreachable(str(e)) from e
    rows = ds.parse_rows(getattr(res, "stdout", "") or "")
    if rows is None:
        tail = (getattr(res, "stderr", "") or "listing produced no output")
        raise WorkspaceUnreachable(tail[-300:])
    return rows


async def stat_via(ws, backend, name: str) -> Optional[dict[str, Any]]:  # noqa: ANN001
    """One dataset's listing row, or None if it does not exist there."""
    name = ds.check_name(name)
    try:
        res = await _exec(ws, backend, ds.rows_command(name))
    except Exception as e:  # noqa: BLE001
        raise WorkspaceUnreachable(str(e)) from e
    rows = ds.parse_rows(getattr(res, "stdout", "") or "")
    if rows is None:
        tail = (getattr(res, "stderr", "") or "stat produced no output")
        raise WorkspaceUnreachable(tail[-300:])
    return rows[0] if rows else None


async def put_via(ws, backend, name: str, blob: bytes) -> dict[str, Any]:  # noqa: ANN001
    """Land dataset bytes at `data/<name>` through the backend.

    Same validation as the local route, same overwrite-by-name semantics.
    """
    name = ds.check_name(name)
    if not blob:
        raise ds.DatasetError("empty body")
    ds.check_size(len(blob))
    root = (ws.abs_path or "").rstrip("/")
    try:
        # An upload may precede the session's first turn (same as uploads.py).
        await backend.ensure_workspace(ws)
        await backend.write_bytes(f"{root}/{ds.DATA_DIR}/{name}", blob,
                                  fs_write=ws.fs_write_config())
    except Exception as e:  # noqa: BLE001
        raise WorkspaceUnreachable(str(e)) from e
    _log.info("dataset delivered via %s: %s (%d bytes)",
              getattr(backend, "name", type(backend).__name__), name, len(blob))
    try:
        row = await stat_via(ws, backend, name)
    except WorkspaceUnreachable:
        row = None
    # The write succeeded; a failed follow-up stat must not fail the upload.
    return row or {
        "name": name, "path": f"{ds.DATA_DIR}/{name}", "bytes": len(blob),
        "modified": int(time.time()),
        "format": (ds.lower_ext(name) or "").lstrip("."),
    }


async def remove_via(ws, backend, name: str) -> bool:  # noqa: ANN001
    name = ds.check_name(name)
    rel = shlex.quote(f"{ds.DATA_DIR}/{name}")
    cmd = (f"if [ -e {rel} ]; then rm -f -- {rel} && echo __ADKCC_DS_GONE__; "
           f"else echo __ADKCC_DS_ABSENT__; fi")
    try:
        res = await _exec(ws, backend, cmd, timeout_s=30)
    except Exception as e:  # noqa: BLE001
        raise WorkspaceUnreachable(str(e)) from e
    out = getattr(res, "stdout", "") or ""
    if "__ADKCC_DS_GONE__" in out:
        return True
    if "__ADKCC_DS_ABSENT__" in out:
        return False
    tail = (getattr(res, "stderr", "") or "delete probe produced no output")
    raise WorkspaceUnreachable(tail[-300:])
