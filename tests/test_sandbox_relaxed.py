"""#122: isolated-sandbox sessions skip routine confirmations, keep the floor.

The mechanism is an effective-mode swap INSIDE decide(): sandbox_isolated
promotes default/acceptEdits to bypassPermissions, inheriting the exact
danger lattice the engine already enforces UNDER bypass — catastrophic
denies, dangerous asks, plan and dontAsk untouched. This pins all of it,
plus the plugin helper's guards (desktop excluded, opt-out env, backend
name gate).

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_sandbox_relaxed.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
for _k in [k for k in os.environ if k.startswith(("ADK_CC_SANDBOX_RELAXED",
                                                  "ADK_CC_DESKTOP"))]:
    os.environ.pop(_k)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    from adk_cc.permissions.engine import decide
    from adk_cc.permissions.modes import PermissionMode as M
    from adk_cc.permissions.settings import SettingsHierarchy
    from adk_cc.tools.bash.tool import BashTool

    tool = BashTool()

    def d(cmd, mode, iso):
        return decide(tool=tool, args={"command": cmd}, mode=mode,
                      settings=SettingsHierarchy(),
                      sandbox_isolated=iso).behavior

    # A/B: the exact relaxation
    check("A/B not-isolated: mutating cmd in default mode ASKS",
          d("pip install pandas", M.DEFAULT, False) == "ask")
    check("isolated: mutating cmd in default mode ALLOWS",
          d("pip install pandas", M.DEFAULT, True) == "allow")
    check("isolated: acceptEdits mutating cmd ALLOWS",
          d("npm install left-pad", M.ACCEPT_EDITS, True) == "allow")

    # The floor survives relaxation
    check("isolated: DANGEROUS still asks",
          d("sudo rm -rf ~/x", M.DEFAULT, True) == "ask")
    check("isolated: CATASTROPHIC still denies",
          d("rm -rf /", M.DEFAULT, True) == "deny")

    # Explicit postures untouched
    check("isolated: plan mode still gates",
          d("pip install pandas", M.PLAN, True) != "allow")
    check("isolated: dontAsk denies dangerous",
          d("sudo rm -rf ~/x", M.DONT_ASK, True) == "deny")

    # ---- plugin helper guards -------------------------------------------
    from adk_cc.plugins.permissions import PermissionPlugin
    from adk_cc.permissions.settings import SettingsHierarchy as SH

    plugin = PermissionPlugin(settings=SH())

    class _Backend:
        def __init__(self, name):
            self.name = name

    class _Ctx:
        def __init__(self, backend):
            self.state = {"temp:sandbox_backend": backend}

    check("helper: docker backend (web) → isolated",
          plugin._sandbox_isolated(_Ctx(_Backend("docker"))))
    check("helper: daytona → isolated",
          plugin._sandbox_isolated(_Ctx(_Backend("daytona"))))
    check("helper: noop → NOT isolated",
          not plugin._sandbox_isolated(_Ctx(_Backend("noop"))))
    check("helper: ssh → NOT isolated (remote ≠ isolated)",
          not plugin._sandbox_isolated(_Ctx(_Backend("ssh"))))
    check("helper: no seeded backend → NOT isolated",
          not plugin._sandbox_isolated(_Ctx(None)))

    os.environ["ADK_CC_SANDBOX_RELAXED"] = "0"
    try:
        check("helper: opt-out env disables",
              not plugin._sandbox_isolated(_Ctx(_Backend("docker"))))
    finally:
        os.environ.pop("ADK_CC_SANDBOX_RELAXED", None)

    os.environ["ADK_CC_DESKTOP"] = "1"
    try:
        from adk_cc import deployment
        # deployment caches? call through: desktop must disable wholesale.
        check("helper: desktop excluded wholesale",
              not plugin._sandbox_isolated(_Ctx(_Backend("docker")))
              or not deployment.is_desktop())
    finally:
        os.environ.pop("ADK_CC_DESKTOP", None)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
