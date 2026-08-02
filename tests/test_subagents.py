"""Spawn/collect sub-agents: wait-all default, early resume, no strays, no HITL.

Shape decided 2026-08-01: one merged read-only explorer; the coordinator waits
for the whole batch by default but may resume once part of it suffices; the
one-call-per-explorer AgentTool shape cannot express that (ADK gathers ALL
tool calls before the model sees anything), hence spawn/collect. A sub-agent
has no human, so anything that would ASK must deny instead — the alternative
is a fan-out hung on a confirmation card nobody can see.

`_run_child` is monkeypatched to controllable coroutines here: the nested-
Runner internals are exercised by the live e2e, while these tests pin the
semantics that make the feature safe — registry, waiting modes, cancellation,
and the deny.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_subagents.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


class _Session:
    def __init__(self):
        self.app_name, self.user_id, self.id = "adk_cc", "u1", "s1"
        self.state: dict = {}


class _Inv:
    def __init__(self):
        self.session = _Session()
        self.invocation_id = "inv-1"
        self.app_name = "adk_cc"
        self.plugin_manager = type("PM", (), {"plugins": []})()


class _Ctx:
    def __init__(self):
        self._invocation_context = _Inv()
        self.state: dict = {}


async def _scenario():
    from adk_cc.tools import subagents as sa

    sa._REGISTRY.clear()
    sa._reset_gate_for_test(8)

    # Controllable children: finish when told, so waiting modes are testable
    # without timing races.
    finish: dict[str, asyncio.Event] = {}

    async def fake_child(agent, task_text, ctx, child_id):
        ev = finish.setdefault(task_text, asyncio.Event())
        await ev.wait()
        return sa.enrich_result(f"report for {task_text}", id=child_id,
                                task=task_text, agent="explorer",
                                elapsed_s=0.1, tool_calls=2)

    # The real _run_child labels a zero-tool report; assert via the real
    # envelope path (measured live: an "explorer" answered from model memory
    # in 24s, zero tool calls, and was presented as research).
    lazy = sa.enrich_result("from memory", id="x", task="t", agent="explorer",
                            tool_calls=0)
    # enrich_result itself stays pure — the label is _run_child's; emulate its
    # condition here to pin the contract:
    assert "note" not in lazy

    real = sa._run_child
    sa._run_child = fake_child
    try:
        spawn = sa.SpawnExplorersTool(agent=object())
        collect = sa.CollectExplorersTool()
        ctx = _Ctx()

        # Events pre-created: spawn's create_task does not start the child
        # coroutine until the next await, so a lazily-created event would not
        # exist when the test tries to set it.
        for t in ("auth flow", "sandbox backends", "css pipeline",
                  "q1", "q2", "stray", "a", "b", "slowpoke"):
            finish[t] = asyncio.Event()

        # --- spawn returns immediately, children keep running -------------
        out = await spawn.run_async(
            args={"tasks": ["auth flow", "sandbox backends", "css pipeline"]},
            tool_context=ctx)
        check("spawn returns ids immediately, without waiting",
              len(out.get("spawned", [])) == 3 and out.get("running") == 3, out)
        ids = {s["task"]: s["id"] for s in out["spawned"]}
        skey = sa._session_key(ctx)
        check("the registry sees three running children",
              len(sa.running_children(skey)) == 3)

        # --- first_done: early resume --------------------------------------
        finish["auth flow"].set()
        got = await collect.run_async(args={"wait": "first_done"},
                                      tool_context=ctx)
        check("first_done returns as soon as one report exists",
              len(got["done"]) == 1 and got["done"][0]["task"] == "auth flow",
              got)
        check("the rest are reported as still running, with elapsed",
              len(got["running"]) == 2
              and all("elapsed_s" in r for r in got["running"]), got)
        check("a collected child leaves the registry",
              len(sa.running_children(skey)) == 2)

        # --- default: wait for all -----------------------------------------
        async def finish_later():
            await asyncio.sleep(0.05)
            finish["sandbox backends"].set()
            finish["css pipeline"].set()

        asyncio.create_task(finish_later())
        got = await collect.run_async(args={}, tool_context=ctx)
        check("default collect waits for ALL outstanding",
              sorted(d["task"] for d in got["done"])
              == ["css pipeline", "sandbox backends"], got)
        check("attribution survives unordered completion",
              all(d["report"] == f"report for {d['task']}" for d in got["done"]))
        check("nothing is left running afterwards",
              sa.running_children(skey) == [] and skey not in sa._REGISTRY)

        # --- an empty timed-out collect explains itself ----------------------
        # Measured live: first_done+timeout_s=20 returned nothing, the model
        # cancelled everything, re-spawned, repeated the pattern — six
        # explorers, zero reports — then answered from memory and the NEXT
        # turn avoided the feature entirely. The empty result must teach.
        out = await spawn.run_async(args={"tasks": ["slowpoke"]}, tool_context=ctx)
        got = await collect.run_async(
            args={"wait": "first_done", "timeout_s": 0.05}, tool_context=ctx)
        check("an empty timed-out collect says the explorers just need time",
              got["done"] == [] and "30-120s" in got.get("note", ""), got)
        check("and nothing was killed by it",
              len(sa.running_children(skey)) == 1)
        finish["slowpoke"].set()
        got = await collect.run_async(args={}, tool_context=ctx)
        check("collecting again then simply works",
              len(got["done"]) == 1, got)

        # --- enough already: cancel the rest --------------------------------
        out = await spawn.run_async(args={"tasks": ["q1", "q2"]},
                                    tool_context=ctx)
        finish["q1"].set()
        got = await collect.run_async(
            args={"wait": "first_done", "cancel_remaining": True},
            tool_context=ctx)
        check("cancel_remaining kills what is still running",
              len(got["done"]) == 1 and got.get("running") == []
              and "DISCARDED" in got.get("note", ""), got)
        # Measured in real use: a timeout+cancel collect destroyed a report and
        # the note did not say WHICH question was lost — the model then covered
        # that topic from memory, indistinguishable from research. The
        # cancelled list names them.
        check("what was cancelled is NAMED, task and all",
              got.get("cancelled") == [{"id": [s["id"] for s in out["spawned"]
                                              if s["task"] == "q2"][0],
                                        "task": "q2"}], got.get("cancelled"))
        check("and the registry is clean",
              skey not in sa._REGISTRY)

        # --- a stuck child self-terminates at the deadline -------------------
        # Measured live: one stuck explorer ground on for a whole 776s turn,
        # survived four collects, and had to be stray-cancelled at turn end.
        # The deadline turns that into a timely error envelope.
        os.environ["ADK_CC_SUBAGENT_DEADLINE_S"] = "0.1"
        try:
            out = await spawn.run_async(args={"tasks": ["never finishes"]},
                                        tool_context=ctx)
            got = await collect.run_async(args={}, tool_context=ctx)
            d = got["done"][0]
            check("a child that never finishes hits the deadline as a RESULT",
                  len(got["done"]) == 1 and not d["ok"]
                  and "deadline" in (d.get("error") or ""), got)
            check("the deadline leaves the registry clean",
                  skey not in sa._REGISTRY)
        finally:
            os.environ.pop("ADK_CC_SUBAGENT_DEADLINE_S", None)

        # --- invocation end cancels strays ----------------------------------
        out = await spawn.run_async(args={"tasks": ["stray"]}, tool_context=ctx)
        n = sa.cancel_children(skey, invocation_id="inv-1")
        check("invocation-end cleanup cancels uncollected children",
              n == 1 and skey not in sa._REGISTRY, n)

        # --- abort kills the whole session's tree ---------------------------
        await spawn.run_async(args={"tasks": ["a", "b"]}, tool_context=ctx)
        n = sa.cancel_children(skey)
        check("session abort cancels every child regardless of turn",
              n == 2 and skey not in sa._REGISTRY, n)
    finally:
        sa._run_child = real


def _deny_checks():
    from adk_cc.permissions.modes import PermissionMode
    from adk_cc.permissions.settings import SettingsHierarchy
    from adk_cc.plugins.permissions import PermissionPlugin
    from adk_cc.tools.bash.tool import BashTool

    class _TCtx:
        agent_name = "explorer"

        def __init__(self, subagent=True):
            self.state = {"subagent": True} if subagent else {}
            self.function_call_id = "c1"
            self.tool_confirmation = None
            self.requested: list = []
            self.actions = type("A", (), {"skip_summarization": False})()

        def request_confirmation(self, *, hint=None, payload=None):
            self.requested.append(payload)

    plugin = PermissionPlugin(SettingsHierarchy(),
                              default_mode=PermissionMode.DEFAULT)
    ctx = _TCtx()
    # run_bash in DEFAULT mode would ASK; inside a sub-agent it must deny.
    out = asyncio.run(plugin.before_tool_callback(
        tool=BashTool(), tool_args={"command": "touch x"}, tool_context=ctx))
    check("a would-ask inside a sub-agent becomes a structured deny",
          (out or {}).get("status") == "permission_denied", out)
    check("with a reason the coordinator can act on",
          "coordinator" in (out or {}).get("error", ""), out)
    check("and NO confirmation card is raised",
          ctx.requested == [], ctx.requested)
    parent = _TCtx(subagent=False)
    out = asyncio.run(plugin.before_tool_callback(
        tool=BashTool(), tool_args={"command": "touch x"}, tool_context=parent))
    check("the same call at the coordinator still asks",
          (out or {}).get("status") == "needs_confirmation"
          and len(parent.requested) == 1, out)


def main() -> int:
    asyncio.run(_scenario())
    _deny_checks()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
