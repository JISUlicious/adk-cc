"""Unit tests for the protected-path floor (Phase 4b) and its precedence over
grants / bypass, via classify_path and the decide() engine.

Run: `.venv/bin/python tests/test_scope_and_protected.py`
"""

from __future__ import annotations

import os
import tempfile

os.environ["ADK_CC_DESKTOP"] = "1"
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
_TMP_DATA = tempfile.mkdtemp(prefix="adkcc-prot-")
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP_DATA

from adk_cc.permissions.engine import decide
from adk_cc.permissions.modes import PermissionMode as M
from adk_cc.permissions.protected import classify_path
from adk_cc.permissions.rules import PermissionRule, RuleBehavior, RuleSource
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.tools.read_file import ReadFileTool
from adk_cc.tools.write_file import WriteFileTool

_ROOT = os.path.realpath("/tmp/proj")


def _d(tool, path, mode, rules=()):
    return decide(tool=tool, args={"path": path}, mode=mode,
                  settings=SettingsHierarchy(list(rules)), workspace_root=_ROOT).behavior


def test_classify_deny_and_ask() -> None:
    secret = os.path.join(_TMP_DATA, "secrets", "k")
    assert classify_path(secret) == "deny", secret
    assert classify_path(os.path.expanduser("~/.ssh/id_rsa")) == "deny"
    assert classify_path(os.path.expanduser("~/.gitconfig")) == "ask"
    assert classify_path(f"{_ROOT}/.git/config") == "ask"
    assert classify_path(f"{_ROOT}/src/a.ts") is None
    print("OK test_classify_deny_and_ask")


def test_deny_beats_grant_and_bypass() -> None:
    home_grant = PermissionRule(
        source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
        tool_name="read_file", rule_content=os.path.expanduser("~") + "/*")
    # secret store: deny even with a covering ~/* grant AND bypass mode
    assert _d(ReadFileTool(), os.path.join(_TMP_DATA, "secrets", "k"),
              M.BYPASS_PERMISSIONS, [home_grant]) == "deny"
    assert _d(ReadFileTool(), "~/.ssh/id_rsa", M.DEFAULT) == "deny"
    print("OK test_deny_beats_grant_and_bypass")


def test_ask_never_auto_approved_but_yields_to_bypass() -> None:
    home_grant_w = PermissionRule(
        source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
        tool_name="write_file", rule_content=os.path.expanduser("~") + "/*")
    # ~/.gitconfig: ask even with a covering grant (never auto-approved)...
    assert _d(WriteFileTool(), "~/.gitconfig", M.DEFAULT, [home_grant_w]) == "ask"
    # ...and even in acceptEdits (protected beats the edit auto-approve)...
    assert _d(WriteFileTool(), f"{_ROOT}/.git/config", M.ACCEPT_EDITS) == "ask"
    # ...but yields to bypassPermissions (matches Claude Code).
    assert _d(WriteFileTool(), "~/.gitconfig", M.BYPASS_PERMISSIONS) == "allow"
    print("OK test_ask_never_auto_approved_but_yields_to_bypass")


def test_normal_paths_unaffected() -> None:
    assert _d(WriteFileTool(), "src/a.ts", M.DEFAULT) == "ask"        # destructive
    assert _d(WriteFileTool(), "src/a.ts", M.BYPASS_PERMISSIONS) == "allow"
    assert _d(ReadFileTool(), "src/a.ts", M.DEFAULT) == "allow"       # read-only
    print("OK test_normal_paths_unaffected")


def test_env_override_extends_deny() -> None:
    os.environ["ADK_CC_PROTECTED_DENY"] = "~/secretz/**"
    try:
        assert classify_path(os.path.expanduser("~/secretz/x")) == "deny"
    finally:
        del os.environ["ADK_CC_PROTECTED_DENY"]
    print("OK test_env_override_extends_deny")


def test_web_mode_no_protection() -> None:
    os.environ["ADK_CC_DESKTOP"] = "0"
    try:
        assert classify_path(os.path.expanduser("~/.ssh/id_rsa")) is None
    finally:
        os.environ["ADK_CC_DESKTOP"] = "1"
    print("OK test_web_mode_no_protection")


def main() -> None:
    test_classify_deny_and_ask()
    test_deny_beats_grant_and_bypass()
    test_ask_never_auto_approved_but_yields_to_bypass()
    test_normal_paths_unaffected()
    test_env_override_extends_deny()
    test_web_mode_no_protection()
    print("\nall scope-and-protected tests passed")


if __name__ == "__main__":
    main()
