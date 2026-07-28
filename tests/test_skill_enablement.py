"""W8 — skills can be turned off without uninstalling them.

Covers the two things that make a toggle real rather than cosmetic:

1. A disabled skill leaves the catalog — BOTH catalogs. `list_skills` is the
   obvious one; the load-bearing one is `SkillToolset.process_llm_request`,
   which injects every skill's name + description into the system instruction
   of EVERY request. Filtering only the tool would leave the skill costing
   tokens and visible to the model on every turn.
2. A disabled skill is refused at every entry point. A model that saw the name
   earlier in the conversation must not be able to route around the toggle.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")

_TMP = tempfile.mkdtemp(prefix="skill-enable-")
os.environ["ADK_CC_SKILL_ENABLEMENT_FILE"] = os.path.join(_TMP, "enablement.json")

from adk_cc.tools import skill_enablement as E  # noqa: E402
from adk_cc.tools.skills import make_skill_toolset  # noqa: E402


SKILL_MD = """---
name: {name}
description: {desc}
---

# {name}

Body for {name}.
"""


def _skills_dir() -> Path:
    base = Path(_TMP) / "skills"
    for name, desc in (("alpha", "The alpha skill."), ("beta", "The beta skill.")):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(SKILL_MD.format(name=name, desc=desc))
    return base


def _reset_store() -> None:
    path = Path(os.environ["ADK_CC_SKILL_ENABLEMENT_FILE"])
    if path.exists():
        path.unlink()


class _FakeCtx:
    """Minimal ToolContext stand-in: skill tools only need `.state` from it."""

    agent_name = "coordinator"

    def __init__(self, state=None):
        self.state = state if state is not None else {}


class _Tenant:
    """Stand-in for the tenant context adk-cc puts in session state."""

    def __init__(self, tenant_id, user_id):
        self.tenant_id, self.user_id = tenant_id, user_id


class _FakeRequest:
    def __init__(self):
        self.instructions = []

    def append_instructions(self, items):
        self.instructions.extend(items)


def test_deny_list_roundtrip() -> None:
    _reset_store()
    assert E.disabled_for("local", "u1") == (frozenset(), frozenset())

    E.set_enabled("alpha", False, tenant_id="local")
    E.set_enabled("beta", False, tenant_id="local", user_id="u1")
    org, user = E.disabled_for("local", "u1")
    assert org == {"alpha"} and user == {"beta"}, (org, user)
    # a different user does not inherit u1's personal choice
    assert E.disabled_for("local", "u2") == (frozenset({"alpha"}), frozenset())

    E.set_enabled("alpha", True, tenant_id="local")
    assert E.disabled_for("local", "u1")[0] == frozenset()
    # idempotent: disabling twice does not duplicate the entry
    E.set_enabled("beta", False, tenant_id="local")
    E.set_enabled("beta", False, tenant_id="local")
    data = json.loads(Path(os.environ["ADK_CC_SKILL_ENABLEMENT_FILE"]).read_text())
    assert data["tenants"]["local"]["disabled"] == ["beta"], data
    print("OK test_deny_list_roundtrip")


def test_session_override_beats_persisted_scopes() -> None:
    """'Not in this chat' — and its inverse, which is why the session layer is
    a {name: bool} map and not a third deny-list.

    On DESKTOP the tenant-scope list is the single user's own global setting, so
    a per-chat override may lift it.
    """
    _reset_store()
    os.environ["ADK_CC_DESKTOP"] = "1"
    try:
        E.set_enabled("alpha", False, tenant_id="local")
        desktop = {"temp:tenant_context": _Tenant("local", "project-1")}
        assert E.disabled_names(desktop) == {"alpha"}
        assert E.disabled_names(
            dict(desktop, **{E.STATE_OVERRIDES: {"alpha": True}})) == frozenset(), (
            "desktop's global list is the user's own — a per-chat override lifts it")
        assert E.disabled_names(
            dict(desktop, **{E.STATE_OVERRIDES: {"beta": False}})) == {"alpha", "beta"}
    finally:
        os.environ.pop("ADK_CC_DESKTOP", None)
    print("OK test_session_override_beats_persisted_scopes")


def test_org_disable_is_a_floor_in_web() -> None:
    """An admin's org-wide 'off' cannot be lifted per user or per chat; the
    user's OWN 'off' can. Same shape as the protected-path permission floor."""
    _reset_store()
    os.environ.pop("ADK_CC_DESKTOP", None)
    E.set_enabled("alpha", False, tenant_id="acme")             # admin, org-wide
    E.set_enabled("beta", False, tenant_id="acme", user_id="u1")  # the user's own
    state = {"temp:tenant_context": _Tenant("acme", "u1")}
    assert E.disabled_names(state) == {"alpha", "beta"}

    lifted = dict(state, **{E.STATE_OVERRIDES: {"alpha": True, "beta": True}})
    assert E.disabled_names(lifted) == {"alpha"}, (
        "org-wide disable must survive a session override")
    print("OK test_org_disable_is_a_floor_in_web")


def test_unknown_name_and_corrupt_file_are_harmless() -> None:
    _reset_store()
    E.set_enabled("does-not-exist", False, tenant_id="local")  # allowed on purpose
    assert "does-not-exist" in E.disabled_names({})
    Path(os.environ["ADK_CC_SKILL_ENABLEMENT_FILE"]).write_text("{not json")
    assert E.disabled_names({}) == frozenset(), "corrupt file must fail OPEN"
    print("OK test_unknown_name_and_corrupt_file_are_harmless")


def test_disabled_skill_leaves_both_catalogs() -> None:
    _reset_store()
    toolset = make_skill_toolset(skills_dir=_skills_dir())
    assert toolset is not None
    lst = next(t for t in toolset._tools if t.name == "list_skills")

    async def catalogs(state):
        ctx = _FakeCtx(state)
        xml = await lst.run_async(args={}, tool_context=ctx)
        req = _FakeRequest()
        await toolset.process_llm_request(tool_context=ctx, llm_request=req)
        return xml, "\n".join(req.instructions)

    xml, sysinstr = asyncio.run(catalogs({}))
    assert "alpha" in xml and "beta" in xml, xml
    assert "alpha" in sysinstr and "beta" in sysinstr

    E.set_enabled("alpha", False, tenant_id="local")
    xml, sysinstr = asyncio.run(catalogs({}))
    assert "alpha" not in xml and "beta" in xml, xml
    assert "alpha" not in sysinstr, "disabled skill still injected into the prompt"
    assert "beta" in sysinstr

    # re-enabled for this chat only
    xml, sysinstr = asyncio.run(catalogs({E.STATE_OVERRIDES: {"alpha": True}}))
    assert "alpha" in xml and "alpha" in sysinstr
    print("OK test_disabled_skill_leaves_both_catalogs")


def test_every_entry_point_refuses_a_disabled_skill() -> None:
    _reset_store()
    toolset = make_skill_toolset(skills_dir=_skills_dir())
    E.set_enabled("alpha", False, tenant_id="local")
    by_name = {t.name: t for t in toolset._tools}

    async def call(tool, args):
        return await by_name[tool].run_async(args=args, tool_context=_FakeCtx({}))

    checks = {
        "load_skill": {"skill_name": "alpha"},
        "load_skill_resource": {"skill_name": "alpha", "file_path": "SKILL.md"},
        "search_skill_resource": {"skill_name": "alpha", "query": "body"},
        "run_skill_script": {"skill_name": "alpha", "script_name": "x.py"},
    }
    for tool, args in checks.items():
        if tool not in by_name:
            raise AssertionError(f"{tool} missing from the toolset: {list(by_name)}")
        res = asyncio.run(call(tool, args))
        assert isinstance(res, dict) and res.get("error_code") == "SKILL_DISABLED", (
            f"{tool} did not refuse a disabled skill: {res}")
    # the enabled one still works through the same tools
    res = asyncio.run(call("load_skill", {"skill_name": "beta"}))
    assert isinstance(res, dict) and "instructions" in res, res
    print("OK test_every_entry_point_refuses_a_disabled_skill")


def test_catalog_reports_source_and_reason() -> None:
    _reset_store()
    os.environ["ADK_CC_SKILLS_DIR"] = str(_skills_dir())
    try:
        E.set_enabled("alpha", False, tenant_id="local")
        E.set_enabled("beta", False, tenant_id="local", user_id="u1")
        rows = {r["name"]: r for r in E.catalog(tenant_id="local", user_id="u1")}
        assert rows["alpha"]["enabled"] is False
        assert rows["alpha"]["disabled_by"] == "org"
        assert rows["beta"]["disabled_by"] == "user"
        assert rows["alpha"]["source"] == "configured", rows["alpha"]
        # built-ins are discovered too, and are on by default
        assert any(r["source"] == "built-in" for r in E.catalog()), "no built-ins"
    finally:
        os.environ.pop("ADK_CC_SKILLS_DIR", None)
    print("OK test_catalog_reports_source_and_reason")


def main() -> None:
    test_deny_list_roundtrip()
    test_session_override_beats_persisted_scopes()
    test_org_disable_is_a_floor_in_web()
    test_unknown_name_and_corrupt_file_are_harmless()
    test_disabled_skill_leaves_both_catalogs()
    test_every_entry_point_refuses_a_disabled_skill()
    test_catalog_reports_source_and_reason()
    print("\nall skill-enablement tests passed")


if __name__ == "__main__":
    main()
