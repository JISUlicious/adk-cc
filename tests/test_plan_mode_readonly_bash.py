"""Integration test: read-only `run_bash` is allowed in PLAN mode.

Exercises the permission engine end-to-end — `decide()` → the PLAN-mode block
→ `_plan_mode_bash_ok` → the read-only classifier. In plan mode a read-only
command (`ls -la`) must NOT be blocked, while a mutating command (`touch x`)
must still be denied "blocked in plan mode". Outside plan mode the plan gate is
inert (the tool's own destructive fallback governs).

Run: `.venv/bin/python tests/test_plan_mode_readonly_bash.py`
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from pydantic import BaseModel

from adk_cc.permissions.engine import decide
from adk_cc.permissions.modes import PermissionMode
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.tools.base import AdkCcTool, ToolMeta


class _Args(BaseModel):
    command: str = ""


class _FakeBashTool(AdkCcTool):
    """Mirrors the real BashTool meta (is_read_only=False, destructive)."""

    meta: ClassVar[ToolMeta] = ToolMeta(
        name="run_bash",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=True,
    )
    input_model: ClassVar[type[BaseModel]] = _Args
    description: ClassVar[str] = "fake bash"

    async def _execute(self, args: BaseModel, ctx: Any) -> dict:
        return {"status": "ran"}


def _decide(mode: PermissionMode, command: str):
    return decide(
        tool=_FakeBashTool(),
        args={"command": command},
        mode=mode,
        settings=SettingsHierarchy(),
    )


def test_plan_allows_read_only_bash() -> None:
    d = _decide(PermissionMode.PLAN, "ls -la")
    # Not blocked by the plan gate. (It still falls through to the destructive
    # fallback → "ask" — the point is it is NOT a plan-mode deny.)
    assert d.behavior != "deny" or "plan mode" not in d.reason, d
    assert "blocked in plan mode" not in d.reason, d
    print(f"OK test_plan_allows_read_only_bash (behavior={d.behavior})")


def test_plan_allows_git_log() -> None:
    d = _decide(PermissionMode.PLAN, "git log --oneline -20")
    assert "blocked in plan mode" not in d.reason, d
    print(f"OK test_plan_allows_git_log (behavior={d.behavior})")


def test_plan_blocks_mutating_bash() -> None:
    d = _decide(PermissionMode.PLAN, "touch newfile")
    assert d.behavior == "deny", d
    assert "blocked in plan mode" in d.reason, d
    print("OK test_plan_blocks_mutating_bash")


def test_plan_blocks_chained_command() -> None:
    # `ls; rm y` has a shell metachar → classifier rejects → plan-mode deny.
    d = _decide(PermissionMode.PLAN, "ls; rm y")
    assert d.behavior == "deny" and "blocked in plan mode" in d.reason, d
    print("OK test_plan_blocks_chained_command")


def test_default_mode_plan_gate_inert() -> None:
    # Outside plan mode the plan block never fires; the destructive fallback
    # governs (→ "ask") regardless of whether the command is read-only.
    d = _decide(PermissionMode.DEFAULT, "touch newfile")
    assert "blocked in plan mode" not in d.reason, d
    print(f"OK test_default_mode_plan_gate_inert (behavior={d.behavior})")


def main() -> None:
    test_plan_allows_read_only_bash()
    test_plan_allows_git_log()
    test_plan_blocks_mutating_bash()
    test_plan_blocks_chained_command()
    test_default_mode_plan_gate_inert()
    print("\nall plan-mode read-only bash tests passed")


if __name__ == "__main__":
    main()
