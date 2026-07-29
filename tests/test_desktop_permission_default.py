"""What mode a fresh desktop chat starts in, and whether it says so.

Two defaults were invisible. `ADK_CC_PERMISSION_MODE` fell back to
`bypassPermissions` everywhere, and the UI wrote no mode when creating a
session — so a packaged desktop app ran in the most permissive mode with
nothing in session state recording it.

Bypass is narrower than it sounds: deny rules, secret material, catastrophic
commands, dangerous commands and bash writing outside the project all still
fire ahead of the bypass short-circuit. What it uniquely gives up is Step 2c,
protected shell/tool config — and Step 1f covers run_bash only, so `write_file`
to `~/.zshrc` lands silently while `echo >> ~/.zshrc` still prompts. That gap
is the one this changes.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_desktop_permission_default.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")


def _mode_in(env: dict) -> str:
    """Import the agent in a clean process — PERMISSION_MODE is module-level."""
    code = "from adk_cc.agent import PERMISSION_MODE; print(PERMISSION_MODE.value)"
    full = dict(os.environ)
    full.update({"ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
                 "ADK_CC_API_KEY": "sk-dummy-for-tests",
                 "PYTHONPATH": str(REPO / "agents")})
    full.update(env)
    for k, v in list(full.items()):
        if v is None:
            full.pop(k)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=full, cwd=str(REPO))
    assert out.returncode == 0, out.stderr[-800:]
    return out.stdout.strip().splitlines()[-1]


def test_desktop_defaults_to_accept_edits() -> None:
    """The decision: a packaged app should still ask before writing a shell rc."""
    got = _mode_in({"ADK_CC_DESKTOP": "1"})
    assert got == "acceptEdits", got
    print("OK desktop_defaults_to_accept_edits")


def test_service_deployment_is_unchanged() -> None:
    """Not desktop → the dev/service default stays exactly as it was."""
    got = _mode_in({"ADK_CC_DESKTOP": None})
    assert got == "bypassPermissions", got
    print("OK service_deployment_is_unchanged")


def test_env_still_wins_everywhere() -> None:
    """An operator setting the mode explicitly must not be second-guessed."""
    got = _mode_in({"ADK_CC_DESKTOP": "1",
                    "ADK_CC_PERMISSION_MODE": "bypassPermissions"})
    assert got == "bypassPermissions", got
    print("OK env_still_wins_everywhere")


def test_a_new_session_records_its_mode() -> None:
    """The composer cannot show a mode that nothing wrote down."""
    from adk_cc.service.file_session_service import FileSessionService

    with tempfile.TemporaryDirectory() as d:
        svc = FileSessionService(d)
        s = asyncio.run(svc.create_session(app_name="adk_cc", user_id="u1"))
        assert s.state.get("permission_mode"), s.state
        print("OK a_new_session_records_its_mode")


def test_an_explicit_mode_is_not_overwritten() -> None:
    """A caller that asked for plan mode meant it."""
    from adk_cc.service.file_session_service import FileSessionService

    with tempfile.TemporaryDirectory() as d:
        svc = FileSessionService(d)
        s = asyncio.run(svc.create_session(
            app_name="adk_cc", user_id="u1", state={"permission_mode": "plan"}))
        assert s.state["permission_mode"] == "plan", s.state
        print("OK an_explicit_mode_is_not_overwritten")


def main() -> None:
    test_desktop_defaults_to_accept_edits()
    test_service_deployment_is_unchanged()
    test_env_still_wins_everywhere()
    test_a_new_session_records_its_mode()
    test_an_explicit_mode_is_not_overwritten()
    print("\nall desktop permission-default tests passed")


if __name__ == "__main__":
    main()
