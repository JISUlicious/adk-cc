from __future__ import annotations

import subprocess
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..base import AdkCcTool, ToolMeta
from ..schemas import RunBashArgs
from .prompt import DESCRIPTION


class BashTool(AdkCcTool):
    meta = ToolMeta(
        name="run_bash",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
        needs_sandbox=True,
    )
    input_model = RunBashArgs
    description = DESCRIPTION

    async def _execute(self, args: RunBashArgs, ctx: ToolContext) -> dict[str, Any]:
        try:
            result = subprocess.run(
                args.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            return {
                "status": "timeout",
                "command": args.command,
                "stdout": e.stdout or "",
                "stderr": e.stderr or "",
            }
        return {
            "status": "ok",
            "command": args.command,
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-2000:],
        }
