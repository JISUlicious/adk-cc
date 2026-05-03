from __future__ import annotations

import re
import shlex
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..sandbox import get_backend, get_workspace
from ..sandbox.config import NetworkConfig
from .base import AdkCcTool, ToolMeta
from .schemas import GrepArgs

_MAX_HITS = 200


class GrepTool(AdkCcTool):
    """Regex search across files under a workspace-anchored path.

    Routes through the sandbox backend (`backend.exec("grep -rn ...")`)
    so results reflect the workspace as the sandbox sees it. With
    NoopBackend this is host execution; with DockerBackend it runs
    `grep` inside the per-session container against the bind-mounted
    workspace.
    """

    meta = ToolMeta(
        name="grep",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = GrepArgs
    description = (
        "Search for an extended regex (POSIX ERE) across files under "
        "`path`. Returns up to 200 hits as `{file, line, text}`; sets "
        "`truncated=true` when more exist. Optional `glob` filters by "
        "basename (e.g. '**/*.py' restricts to `.py` files)."
    )

    async def _execute(self, args: GrepArgs, ctx: ToolContext) -> dict[str, Any]:
        # Validate the regex client-side too — gives a clean error before
        # we burn a backend call on a bad pattern.
        try:
            re.compile(args.pattern)
        except re.error as e:
            return {"status": "error", "error": f"bad regex: {e}"}

        ws = get_workspace(ctx)
        backend = get_backend(ctx)

        path = args.path or "."
        if not path.startswith("/"):
            path = f"{ws.abs_path.rstrip('/')}/{path}".rstrip("/") or ws.abs_path

        # Use grep -rn -E on the path, filtering by --include for the
        # glob (basename match — we don't translate the full glob).
        include_flag = ""
        if args.glob and args.glob != "**/*":
            # Take the last component of the glob as a basename pattern.
            tail = args.glob.split("/")[-1]
            if tail and tail != "**":
                include_flag = f"--include={shlex.quote(tail)}"

        cmd = (
            f"grep -rn -E -I {include_flag} -- {shlex.quote(args.pattern)} "
            f"{shlex.quote(path)} 2>/dev/null | head -n {_MAX_HITS + 1}"
        )

        result = await backend.exec(
            cmd,
            fs_write=ws.fs_write_config(),
            network=NetworkConfig(),
            timeout_s=30,
            cwd=ws.abs_path,
        )
        # grep exit 0 = matches, 1 = no matches, 2 = error.
        if result.exit_code == 1:
            return {"status": "ok", "hits": [], "truncated": False}
        if result.exit_code not in (0, 124):
            return {
                "status": "error",
                "error": f"grep exit {result.exit_code}: {result.stderr.strip()[:300]}",
            }

        hits: list[dict] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            # Format: "<file>:<line>:<text>"
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            try:
                lineno = int(parts[1])
            except ValueError:
                continue
            hits.append({"file": parts[0], "line": lineno, "text": parts[2][:300]})

        truncated = len(hits) > _MAX_HITS
        return {"status": "ok", "hits": hits[:_MAX_HITS], "truncated": truncated}
