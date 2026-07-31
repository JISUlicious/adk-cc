"""Running a skill's script asks first, and shows what it is about to run.

Found live: a published third-party skill (openscad) was asked to make a
project. Its own `openscad-project.sh` did `mkdir -p ~/openscad-projects/<name>`
and wrote files there — silently, with no prompt. Moments later the AGENT tried
`write_file` to that same directory and the permission floor stopped it:
"targets a path outside the project scope". The floor guards what the agent
does directly; a skill script was one exec whose insides nothing mediated.

So `run_skill_script` is gated like `run_bash`: Allow once / Allow always /
Deny, the same rule storage, and — because "do you trust this script?" cannot
be answered without reading it — the script's SOURCE in the prompt.

Two decisions worth stating:
  * it asks even under bypassPermissions. That mode says the user trusts the
    AGENT's judgement about its own actions; a skill script is somebody else's
    code, closer to `curl | sh` than to an edit the agent chose to make.
  * "Allow always" grants the whole skill (`name:*`), shown in the prompt
    before the click — the same literal+broadened shape as run_bash.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_script_gate.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_ROOT = Path(tempfile.mkdtemp(prefix="gateskills-"))
os.environ["ADK_CC_SKILLS_DIR"] = str(_ROOT)
_d = _ROOT / "housekeeper"
(_d / "scripts").mkdir(parents=True)
(_d / "SKILL.md").write_text(
    "---\nname: housekeeper\ndescription: Tidies things up.\n---\n\nBody.\n")
(_d / "scripts" / "tidy.sh").write_text(
    "#!/usr/bin/env bash\n# Looks harmless; writes to your home directory.\n"
    'mkdir -p "$HOME/housekeeper-out"\n'
    'echo "swept" > "$HOME/housekeeper-out/log.txt"\n')

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


class _Confirmation:
    def __init__(self, chose_id=None, confirmed=None, persist=False):
        self.payload = {"chose_id": chose_id} if chose_id else {}
        if persist:
            self.payload["persist_across_sessions"] = True
        self.confirmed = confirmed


class _Ctx:
    """Enough ToolContext for the plugin: state, a call id, and a place for
    `request_confirmation` to land."""

    agent_name = "coordinator"

    def __init__(self, confirmation=None, state=None):
        self.state = state if state is not None else {}
        self.function_call_id = "call-1"
        self.tool_confirmation = confirmation
        self.requested: list[dict] = []
        self.actions = type("A", (), {"skip_summarization": False})()

    def request_confirmation(self, *, hint=None, payload=None):
        self.requested.append({"hint": hint, "payload": payload})


def main() -> int:
    from adk_cc.permissions.modes import PermissionMode
    from adk_cc.permissions.settings import SettingsHierarchy
    from adk_cc.plugins.permissions import PermissionPlugin
    from adk_cc.tools import skills as sk

    sk.clear_project_skill_cache()
    toolset = sk.make_skill_toolset()
    tool = next(t for t in toolset._tools if t.name == "run_skill_script")
    args = {"skill_name": "housekeeper", "file_path": "scripts/tidy.sh"}

    def run(plugin, ctx):
        return asyncio.run(plugin.before_tool_callback(
            tool=tool, tool_args=args, tool_context=ctx))

    plugin = PermissionPlugin(SettingsHierarchy(), default_mode=PermissionMode.DEFAULT)

    # --- first use asks -------------------------------------------------
    ctx = _Ctx()
    out = run(plugin, ctx)
    check("the first run of a skill script does not just happen",
          (out or {}).get("status") == "needs_confirmation", out)
    check("the user is asked", len(ctx.requested) == 1, ctx.requested)
    payload = (ctx.requested[0]["payload"] if ctx.requested else {}) or {}
    detail = payload.get("detail", "")
    check("and can SEE the script before deciding",
          "mkdir -p" in detail and "housekeeper-out" in detail, detail[:160])
    check("the prompt names the script",
          "tidy.sh" in payload.get("title", "") + detail, payload.get("title"))
    ids = [o.get("id") for o in payload.get("options", [])]
    check("with three choices, as the bash gate has",
          ids == ["allow_once", "allow_always", "deny"], ids)
    always = next((o for o in payload.get("options", [])
                   if o.get("id") == "allow_always"), {})
    check("and the breadth of 'always' is stated before the click",
          "housekeeper:*" in (always.get("description") or ""),
          always.get("description"))

    # --- the three answers ----------------------------------------------
    check("Allow once lets it run",
          run(plugin, _Ctx(_Confirmation("allow_once"))) is None)

    denied = run(plugin, _Ctx(_Confirmation("deny")))
    check("Deny stops it",
          (denied or {}).get("status") == "permission_denied_by_user", denied)
    check("and the model is told why, so it can adapt",
          "declined" in (denied or {}).get("reason", "").lower(), denied)

    state: dict = {}
    ctx = _Ctx(_Confirmation("allow_always"), state=state)
    check("Allow always lets it run", run(plugin, ctx) is None)
    rules = state.get("adk_cc_allow_rules") or []
    contents = [r.get("rule_content") for r in rules]
    check("and stores a rule for the script and for the skill",
          "housekeeper:scripts/tidy.sh" in contents and "housekeeper:*" in contents,
          contents)
    check("so the next run of that script does not ask again",
          run(plugin, _Ctx(state=state)) is None)
    check("nor does a SIBLING script of the same trusted skill",
          asyncio.run(plugin.before_tool_callback(
              tool=tool, tool_context=_Ctx(state=state),
              tool_args={"skill_name": "housekeeper",
                         "file_path": "scripts/other.sh"})) is None)
    other = asyncio.run(plugin.before_tool_callback(
        tool=tool, tool_context=_Ctx(state=state),
        tool_args={"skill_name": "somebody-else", "file_path": "scripts/x.sh"}))
    check("but a DIFFERENT skill still asks",
          (other or {}).get("status") == "needs_confirmation", other)

    # --- bypass does not skip this ---------------------------------------
    bypass = PermissionPlugin(SettingsHierarchy(),
                              default_mode=PermissionMode.BYPASS_PERMISSIONS)
    ctx = _Ctx(state={"permission_mode": "bypassPermissions"})
    out = run(bypass, ctx)
    check("bypassPermissions does NOT wave third-party code through",
          (out or {}).get("status") == "needs_confirmation", out)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
