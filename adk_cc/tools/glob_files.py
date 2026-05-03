from __future__ import annotations

import shlex
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import get_backend, get_workspace
from ..sandbox.config import NetworkConfig
from .base import AdkCcTool, ToolMeta
from .schemas import GlobFilesArgs

_MAX_MATCHES = 200


class GlobFilesTool(AdkCcTool):
    """Search for files by glob pattern, anchored under the workspace root.

    Routes through the sandbox backend (`backend.exec("find ...")`) so the
    results reflect the workspace as the sandbox sees it, not the agent
    pod's host. With NoopBackend this is host execution; with
    DockerBackend it runs `find` inside the per-session container against
    the bind-mounted workspace.
    """

    meta = ToolMeta(
        name="glob_files",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = GlobFilesArgs
    description = (
        "Find files matching a glob pattern under `root`. Supports basename "
        "patterns ('*.py'), recursive patterns ('**/*.py'), and prefixed "
        "patterns ('src/**/*.ts'). Returns up to 200 matches; sets "
        "`truncated=true` when more exist."
    )

    async def _execute(self, args: GlobFilesArgs, ctx: ToolContext) -> dict[str, Any]:
        ws = get_workspace(ctx)
        backend = get_backend(ctx)

        # Anchor relative roots under the workspace; absolute roots
        # are passed through (the sandbox's fs_read still gates them).
        root = args.root or "."
        if not root.startswith("/"):
            root = f"{ws.abs_path.rstrip('/')}/{root}".rstrip("/") or ws.abs_path

        # `find -path` for glob support; quote everything; cap output.
        # We use -name for simple basename patterns and -path for ones
        # containing /. This isn't a full glob → find translation but
        # covers the common cases (`**/*.py`, `*.csv`, `src/**/*.ts`).
        pattern = args.pattern
        if "/" in pattern or "**" in pattern:
            # Strip leading **/ so the pattern works as a -path suffix.
            stripped = pattern.removeprefix("**/")
            test = f"-path {shlex.quote('*/' + stripped)} -o -path {shlex.quote(stripped)}"
        else:
            test = f"-name {shlex.quote(pattern)}"

        cmd = (
            f"find {shlex.quote(root)} -type f \\( {test} \\) "
            f"2>/dev/null | sort | head -n {_MAX_MATCHES + 1}"
        )

        result = await backend.exec(
            cmd,
            fs_write=ws.fs_write_config(),
            network=NetworkConfig(),
            timeout_s=30,
            cwd=ws.abs_path,
        )
        if result.exit_code != 0 and result.exit_code != 124:
            return {
                "status": "error",
                "error": f"find exit {result.exit_code}: {result.stderr.strip()[:300]}",
            }
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        truncated = len(lines) > _MAX_MATCHES
        return {
            "status": "ok",
            "matches": lines[:_MAX_MATCHES],
            "truncated": truncated,
        }
