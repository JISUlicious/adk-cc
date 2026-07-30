#!/usr/bin/env python3
"""Entrypoint for the page smoke check — run this, not the .mjs directly.

Why a Python wrapper around a Node runner:

  * `run_skill_script` only executes .py/.sh/.bash (ADK's `_build_wrapper_code`
    returns None for anything else), so a bare `.mjs` cannot be launched
    through the skill tool at all. A live run proved it: the agent loaded this
    skill, tried `run_skill_script(... smoke_page.mjs)` and got
    "Unsupported script type '.mjs'".
  * Its fallback — `run_bash "node scripts/smoke_page.mjs …"` — exited 1,
    because a skill's files are NOT in the workspace: they are served through
    the skill tools, so that relative path does not exist where commands run.

This file sits next to the runner, so it can find it by `__file__` no matter
where it is invoked from, and hands off to node.

Usage (through run_skill_script, args as a list):
    ["<page.html>", "<check.mjs>", "--json"]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_RUNNER = Path(__file__).resolve().parent / "smoke_page.mjs"


def _fail(msg: str, code: int = 3) -> int:
    print(msg, file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return _fail("usage: smoke_page.py <page.html> <check.mjs> [--json]")
    if not shutil.which("node"):
        return _fail(
            "node is not installed, so a page cannot be driven here. Report the "
            "behaviour as unverified rather than substituting a syntax check.",
            2,
        )
    if not _RUNNER.is_file():  # pragma: no cover — packaging guard
        return _fail(f"runner missing next to this script: {_RUNNER}")

    page, check = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    for label, p in (("page", page), ("check", check)):
        if not p.is_file():
            # Skill scripts execute in a TEMP cwd, not the workspace, so a
            # relative arg resolves against the wrong directory. A live run hit
            # exactly this and spent an extra round trip on `pwd && realpath`
            # to work it out — so say it here instead of only reporting the
            # path that was not found.
            return _fail(
                f"{label} not found: {p}\n"
                "Skill scripts run in a temporary directory, so RELATIVE paths "
                "do not resolve against your workspace. Pass absolute paths — "
                "`run_bash: realpath index.html check.mjs` gives them.")

    proc = subprocess.run(
        ["node", str(_RUNNER), str(page), str(check), *argv[2:]],
        capture_output=True, text=True,
        # Inherit the caller's env plus a hint for the shared runtime cache, so
        # a jsdom installed once is found from any workspace.
        env={**os.environ,
             "ADK_CC_WEB_RUNTIME_DIR": os.environ.get(
                 "ADK_CC_WEB_RUNTIME_DIR",
                 str(Path.home() / ".adk-cc" / "web-runtime"))},
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 2:
        # No DOM runtime. Say the one thing that fixes it, in the terms the
        # caller can act on — installing it writes outside the project, so it
        # needs the user's confirmation.
        print(json.dumps({
            "hint": "no DOM runtime installed. Ask the user to approve: "
                    "mkdir -p ~/.adk-cc/web-runtime && cd ~/.adk-cc/web-runtime "
                    "&& npm i jsdom  — it writes outside the project, so it "
                    "needs a permission answer. Until then the page behaviour "
                    "is UNVERIFIED; do not substitute a syntax check.",
        }), file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
