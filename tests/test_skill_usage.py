"""Offered vs used, and not paying twice for the same skill.

Two things the Agent Skills implementer guide asks a client to do, and one
complaint it does not answer:

  * "Consider tracking which skills have been activated in the current session.
     If the model attempts to load a skill that's already in context, you can
     skip the re-injection." — a second load pays the whole token cost to say
     nothing new.
  * The spec makes the DESCRIPTION responsible for triggering, and nothing
     tells an author when theirs is not working. adk-cc holds both halves —
     what was offered on a turn, and what was loaded — so the ratio is a fact
     rather than a guess.

Counted per TURN, not per model request: a turn makes several requests, and
counting those would read as heavy use of a skill nobody touched.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_usage.py
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
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="usagedata-")
os.environ["ADK_CC_DESKTOP"] = "1"

_ROOT = Path(tempfile.mkdtemp(prefix="usageskills-"))
os.environ["ADK_CC_SKILLS_DIR"] = str(_ROOT)
for _n in ("alpha", "beta"):
    _d = _ROOT / _n
    _d.mkdir(parents=True)
    (_d / "SKILL.md").write_text(
        f"---\nname: {_n}\ndescription: The {_n} skill.\n---\n\n{_n} instructions.\n")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


class _Req:
    def __init__(self):
        self.instructions: list[str] = []

    def append_instructions(self, items):
        self.instructions.extend(items)


def _ctx(invocation_id: str):
    class _Inv:
        pass

    inv = _Inv()
    inv.invocation_id = invocation_id

    class _Ctx:
        agent_name = "coordinator"

        def __init__(self):
            self.state: dict = {}
            self._invocation_context = inv

    return _Ctx()


def main() -> int:
    from adk_cc.tools import skill_usage, skills as sk

    sk.clear_project_skill_cache()
    toolset = sk.make_skill_toolset()
    tools = {t.name: t for t in toolset._tools}

    # --- offers are counted once per TURN --------------------------------
    ctx = _ctx("turn-1")
    for _ in range(3):          # one turn, three model requests
        asyncio.run(toolset.process_llm_request(tool_context=ctx, llm_request=_Req()))
    counts = skill_usage.usage()
    check("a skill in the catalogue is counted as offered",
          counts.get("alpha", {}).get("offered") == 1,
          f"alpha={counts.get('alpha')}")
    check("three requests in one turn count once, not three times",
          counts.get("beta", {}).get("offered") == 1, f"beta={counts.get('beta')}")

    asyncio.run(toolset.process_llm_request(tool_context=_ctx("turn-2"),
                                            llm_request=_Req()))
    counts = skill_usage.usage()
    check("a second turn counts again",
          counts.get("alpha", {}).get("offered") == 2, f"alpha={counts.get('alpha')}")
    check("nothing is counted as USED until it is loaded",
          counts.get("alpha", {}).get("used", 0) == 0, f"alpha={counts.get('alpha')}")

    # --- loading counts, and the second load is free ---------------------
    load = tools["load_skill"]
    session = _ctx("turn-3")
    first = asyncio.run(load.run_async(args={"skill_name": "alpha"},
                                       tool_context=session))
    check("loading a skill returns its instructions",
          "alpha instructions" in str(first), str(first)[:120])
    check("and is counted as a use",
          skill_usage.usage().get("alpha", {}).get("used") == 1,
          skill_usage.usage().get("alpha"))

    second = asyncio.run(load.run_async(args={"skill_name": "alpha"},
                                        tool_context=session))
    check("a repeat load in the SAME session does not re-send the body",
          "alpha instructions" not in str(second), str(second)[:160])
    check("but says so plainly instead of failing",
          (second or {}).get("already_loaded") is True
          and "already loaded" in str(second), str(second)[:160])
    check("and is not double-counted as a second use",
          skill_usage.usage().get("alpha", {}).get("used") == 1,
          skill_usage.usage().get("alpha"))

    fresh = _ctx("turn-4")
    third = asyncio.run(load.run_async(args={"skill_name": "alpha"},
                                       tool_context=fresh))
    check("a DIFFERENT session gets the full instructions",
          "alpha instructions" in str(third), str(third)[:120])

    # --- what the panel shows --------------------------------------------
    from adk_cc.tools import skill_enablement

    rows = {r["name"]: r for r in skill_enablement.catalog()}
    check("the catalogue row carries the counts",
          rows.get("alpha", {}).get("usage", {}).get("used", 0) >= 1,
          rows.get("alpha"))
    check("a skill offered but never used shows exactly that",
          rows.get("beta", {}).get("usage") == {"offered": 2, "used": 0},
          rows.get("beta", {}).get("usage"))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
