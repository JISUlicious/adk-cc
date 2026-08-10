"""Compaction must not eat a parked confirmation's functionCall (#119).

Reported live: tool call gated -> user deciding -> ADK event compaction ran
-> answering the card raised "No function call event found for function
responses ids: {...}".

ADK's own guard (`compaction._pending_function_call_ids`) counts ANY
function_response as an answer. adk-cc's confirmation protocol produces
responses that are NOT answers:

  - the gate closes the ORIGINAL call with an interim
    {"status": "needs_confirmation"} dict (permissions.py), and
  - a stashed first click answers the WRAP id under the sentinel name
    `adk_cc_pending_confirmation`, which ADK deliberately ignores.

Both make a parked call look answered, so ADK compacts the call event; the
post-approval real response then orphans and contents assembly raises.

`install_compaction_pin` (context/compaction_pin.py) swaps in a name/payload
aware pending set. This test drives the REAL ADK selection + contents code
with REAL Event objects: it must reproduce the live error stock, and require
the pin to prevent it (A/B — if both sides ever pass, ADK fixed it upstream
and the pin should be retired).

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_compaction_pin.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "tests"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
# Seal the flags under test: a shell-leaked ADK_CC_COMPACTION_* would make
# agent.py auto-install the pin at import and turn the stock A/B vacuous.
for _k in [k for k in os.environ if k.startswith("ADK_CC_COMPACTION")]:
    os.environ.pop(_k)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _ev(ts, *, calls=(), resps=(), text=None, author="model", long_running=None):
    """Real ADK Event. calls: (name, id); resps: (name, id, payload)."""
    from google.adk.events.event import Event
    from google.genai import types

    parts = []
    for name, cid in calls:
        parts.append(types.Part(function_call=types.FunctionCall(
            id=cid, name=name, args={})))
    for name, cid, payload in resps:
        parts.append(types.Part(function_response=types.FunctionResponse(
            id=cid, name=name, response=payload)))
    if text is not None:
        parts.append(types.Part(text=text))
    e = Event(
        author=author,
        content=types.Content(
            role="model" if author != "user" else "user", parts=parts),
        invocation_id=f"inv{int(ts)}",
    )
    e.timestamp = float(ts)
    if long_running:
        e.long_running_tool_ids = set(long_running)
    return e


_INTERIM = {"status": "needs_confirmation", "reason": "gated",
            "is_pause_not_denial": True}
_FILLER = "x" * 400  # keep char/4 estimates comfortably above any threshold


def _parked_history(*, stash_click=False):
    """The live shape: gated call c1, wrap w1, interim response, newer noise."""
    evs = [
        _ev(1, text=_FILLER, author="user"),
        _ev(2, text=_FILLER),
        _ev(3, text=_FILLER, author="user"),
        _ev(4, text=_FILLER),
        _ev(5, text=_FILLER, author="user"),
        _ev(6, text=_FILLER),
        _ev(7, calls=[("run_skill_script", "c1")]),                    # gated call
        _ev(8, calls=[("adk_request_confirmation", "w1")],
            long_running=["w1"]),                                      # wrap
        _ev(9, resps=[("run_skill_script", "c1", dict(_INTERIM))],
            author="user"),                                            # interim
        _ev(10, text=_FILLER),                                         # title write etc.
        _ev(11, text=_FILLER),
        _ev(12, text=_FILLER),
    ]
    if stash_click:
        evs.append(_ev(13, resps=[(
            "adk_cc_pending_confirmation", "w1",
            {"chose_id": "allow"})], author="user"))
    return evs


def _resolved_history():
    """Same flow after the user allowed: everything answered for real."""
    return [
        _ev(1, text=_FILLER, author="user"),
        _ev(2, text=_FILLER),
        _ev(7, calls=[("run_skill_script", "c1")]),
        _ev(8, calls=[("adk_request_confirmation", "w1")],
            long_running=["w1"]),
        _ev(9, resps=[("run_skill_script", "c1", dict(_INTERIM))],
            author="user"),
        _ev(10, resps=[("adk_request_confirmation", "w1",
                        {"confirmed": True})], author="user"),
        _ev(11, resps=[("run_skill_script", "c1", {"status": "ok"})],
            author="user"),
        _ev(12, text=_FILLER),
        _ev(13, text=_FILLER, author="user"),
        _ev(14, text=_FILLER),
        _ev(15, text=_FILLER, author="user"),
        _ev(16, text=_FILLER),
    ]


def _select(events, retention=2):
    """Run ADK's real token-threshold selection; return compacted timestamps."""
    from google.adk.apps import compaction as C
    picked = C._events_to_compact_for_token_threshold(
        events=events, event_retention_size=retention)
    return {e.timestamp for e in picked}


def _make_stub_summarizer():
    """Real-shaped compaction events, no LLM; valid for the config field."""
    from google.adk.apps.base_events_summarizer import BaseEventsSummarizer

    class _Stub(BaseEventsSummarizer):
        async def maybe_summarize_events(self, *, events):
            from google.adk.events.event import Event
            from google.adk.events.event_actions import (
                EventActions, EventCompaction)
            from google.genai import types
            if not events:
                return None
            return Event(
                author="model",
                invocation_id="compaction",
                actions=EventActions(compaction=EventCompaction(
                    start_timestamp=events[0].timestamp,
                    end_timestamp=events[-1].timestamp,
                    compacted_content=types.Content(
                        role="model", parts=[types.Part(text="[summary]")]),
                )),
            )

    return _Stub()


def _deliver_after_compaction(events):
    """Compact the parked history, then deliver the real answer + tool result.

    Returns "ok" or the ValueError message — the live incident reproduced.
    """
    from google.adk.apps import compaction as C
    from google.adk.flows.llm_flows import contents as CO

    picked = C._events_to_compact_for_token_threshold(
        events=events, event_retention_size=2)
    comp = asyncio.run(
        _make_stub_summarizer().maybe_summarize_events(events=picked))
    history = list(events)
    if comp is not None:
        comp.timestamp = events[-1].timestamp + 1
        history.append(comp)
    # The resume: user answers the wrap, the tool re-runs and responds.
    history.append(_ev(90, resps=[("adk_request_confirmation", "w1",
                                   {"confirmed": True})], author="user"))
    history.append(_ev(91, resps=[("run_skill_script", "c1",
                                   {"status": "ok"})], author="user"))
    try:
        CO._get_contents(current_branch=None, events=history, agent_name="")
        return "ok"
    except ValueError as e:
        return str(e)


async def _runner_scenario():
    """The live incident through the REAL ADK runner + REAL plugins.

    One gated run_skill_script -> parked -> compaction (production function,
    tiny threshold) -> user allows. Uses whatever pending-call function is
    currently installed in google.adk.apps.compaction, so callers A/B it.
    Returns {"compacted", "ran", "error", "texts"}.
    """
    import e2e_confirmation_flow as T
    from google.adk.agents import LlmAgent
    from google.adk.apps import App, ResumabilityConfig
    from google.adk.apps import compaction as C
    from google.adk.apps.app import EventsCompactionConfig
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.adk.tools import FunctionTool
    from google.genai import types

    from adk_cc.plugins.confirmation_form_ui import ConfirmationFormUiPlugin
    from adk_cc.plugins.permissions import PermissionPlugin
    from adk_cc.service.turns import install_confirmation_resume_fix

    ran: list = []

    def run_skill_script(skill_name: str, file_path: str) -> dict:
        ran.append((skill_name, file_path))
        return {"status": "ok", "stdout": f"ran: {skill_name}/{file_path}"}

    one = LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(
            id="A-1", name="run_skill_script",
            args={"skill_name": "g", "file_path": "scripts/a.py"}))]))
    agent = LlmAgent(
        name="test_agent",
        model=T._ScriptedLlm(responses=[one, T._text_response("done after tools"),
                                        T._text_response("spare")]),
        tools=[FunctionTool(run_skill_script)])
    config = EventsCompactionConfig(
        token_threshold=1, event_retention_size=0,
        compaction_interval=10, overlap_size=2,
        summarizer=_make_stub_summarizer())
    app = App(name="conf_compact", root_agent=agent,
              plugins=[PermissionPlugin(settings=T.SettingsHierarchy()),
                       ConfirmationFormUiPlugin()],
              resumability_config=ResumabilityConfig(is_resumable=True),
              events_compaction_config=config)
    runner = InMemoryRunner(app=app)
    install_confirmation_resume_fix(runner)
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id="u", session_id="s")

    async def run(msg):
        out = []
        async for ev in runner.run_async(user_id="u", session_id="s",
                                         new_message=msg):
            out.append(ev)
        return out

    async def session():
        return await runner.session_service.get_session(
            app_name=runner.app_name, user_id="u", session_id="s")

    def has_compaction(s):
        return any(e.actions and e.actions.compaction for e in s.events)

    # Enough text that the char/4 estimate crosses the tiny threshold.
    ev1 = await run(types.Content(role="user", parts=[
        types.Part(text="please run the gated skill script now. " * 40)]))
    wraps = [fc.id for e in ev1 for fc in e.get_function_calls()
             if fc.name in ("adk_request_confirmation",
                            "adk_cc_confirmation_form")]
    assert len(wraps) == 1, f"expected 1 wrap, got {wraps}"

    # "While waiting for confirmation, compaction happened." The runner may
    # have run it post-invocation already; if not, invoke the same
    # production function the runner uses.
    sess = await session()
    if not has_compaction(sess):
        await C._run_compaction_for_token_threshold_config(
            config=config, session=sess,
            session_service=runner.session_service, agent=agent)
        sess = await session()

    answer = types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(
            id=wraps[0], name="adk_cc_confirmation_form",
            response={"chose_id": "allow_once"}))])
    try:
        ev2 = await run(answer)
        texts = [p.text for e in ev2
                 for p in (e.content.parts if e.content else [])
                 if getattr(p, "text", None)]
        return {"compacted": has_compaction(sess), "ran": list(ran),
                "error": "", "texts": texts}
    except ValueError as e:
        return {"compacted": has_compaction(sess), "ran": list(ran),
                "error": str(e), "texts": []}


def main() -> int:
    import inspect

    from google.adk.apps import compaction as C

    from adk_cc.context import compaction_pin as PIN

    _stock_pending = C._pending_function_call_ids
    check("pin not pre-installed (env sealed)",
          not getattr(C, "_adk_cc_pin", False))

    # ---- contract: the ADK internals the pin patches still exist --------
    check("patch point exists",
          callable(getattr(C, "_pending_function_call_ids", None)))
    for fn in ("_events_to_compact_for_token_threshold",
               "_run_compaction_for_sliding_window"):
        src = inspect.getsource(getattr(C, fn))
        check(f"{fn} still routes through the patch point",
              "_pending_function_call_ids(" in src
              and "_truncate_events_before_pending_function_call(" in src)
    from adk_cc.plugins import confirmation_form_ui as CF
    from adk_cc.service import turns as TU
    check("pin literals match broker + plugin names",
          set(PIN._UNDELIVERED_CONFIRMATION_NAMES)
          == set(TU._UNDELIVERED_CONFIRMATION_NAMES)
          == {CF.PENDING_CONFIRMATION_NAME,
              CF.CONFIRMATION_FORM_FUNCTION_CALL_NAME})

    # ---- A: stock ADK is vulnerable (else retire the pin) ---------------
    stock = _select(_parked_history())
    check("A/B stock: interim response defeats ADK's guard "
          "(gated call c1 selected for compaction)", 7.0 in stock, stock)
    # Token-threshold selection happens to shield the stash shape via its
    # pair-keeping split (the stashed response drags its wrap call — and
    # transitively the gated call — out of range). Document that accident:
    stock_stash = _select(_parked_history(stash_click=True))
    check("stock token-threshold path shields the stash shape incidentally",
          7.0 not in stock_stash and 8.0 not in stock_stash, stock_stash)
    # The SLIDING-WINDOW path has no such split — it applies exactly this
    # guard sequence (compaction.py), which the stash defeats stock:
    sw = _parked_history(stash_click=True)
    sw_kept = C._truncate_events_before_pending_function_call(
        sw, C._pending_function_call_ids(sw))
    check("A/B stock: stashed click defeats the sliding-window guard "
          "(gated + wrap calls left compactable)",
          {e.timestamp for e in sw_kept} >= {7.0, 8.0, 13.0},
          {e.timestamp for e in sw_kept})
    err = _deliver_after_compaction(_parked_history())
    check("A/B stock: delivering the answer reproduces the LIVE error",
          "No function call event found" in err, err)

    # ---- B: with the pin installed ---------------------------------------
    check("install_compaction_pin succeeds", PIN.install_compaction_pin())
    check("install is idempotent", PIN.install_compaction_pin())

    pinned = _select(_parked_history())
    check("pinned: gated call survives", 7.0 not in pinned, pinned)
    check("pinned: wrap call survives", 8.0 not in pinned, pinned)
    check("pinned: interim response survives", 9.0 not in pinned, pinned)
    check("pinned: older prefix still compacts",
          pinned == {1.0, 2.0, 3.0, 4.0, 5.0, 6.0}, pinned)

    pinned_stash = _select(_parked_history(stash_click=True))
    check("pinned: stashed click event survives",
          13.0 not in pinned_stash and 7.0 not in pinned_stash
          and 8.0 not in pinned_stash, pinned_stash)
    sw2 = _parked_history(stash_click=True)
    sw2_kept = C._truncate_events_before_pending_function_call(
        sw2, C._pending_function_call_ids(sw2))
    check("pinned: sliding-window guard now cuts before the gated call",
          {e.timestamp for e in sw2_kept} == {1.0, 2.0, 3.0, 4.0, 5.0, 6.0},
          {e.timestamp for e in sw2_kept})

    check("pinned: delivering the answer works",
          _deliver_after_compaction(_parked_history()) == "ok")

    # ---- no over-pinning: resolved flows compact normally ----------------
    resolved = _select(_resolved_history())
    check("resolved flow: fully answered call+wrap DO compact",
          {7.0, 8.0, 9.0, 10.0, 11.0} <= resolved, resolved)

    # ---- the whole incident through the real runner (A/B) ----------------
    # Stock: restore ADK's own pending computation for the A side.
    C._pending_function_call_ids = _stock_pending
    C._adk_cc_pin = False
    r = asyncio.run(_runner_scenario())
    check("runner A/B stock: compaction ran while parked", r["compacted"], r)
    check("runner A/B stock: allowing the card hits the LIVE error "
          "(if this fails, ADK fixed it upstream — retire the pin)",
          "No function call event found" in r["error"], r)

    check("re-install for the B side", PIN.install_compaction_pin())
    r = asyncio.run(_runner_scenario())
    check("runner pinned: compaction still ran while parked",
          r["compacted"], r)
    check("runner pinned: no error on allow", r["error"] == "", r)
    check("runner pinned: the gated script actually ran",
          len(r["ran"]) == 1, r)
    check("runner pinned: the model replies afterwards",
          any("done after tools" in t for t in r["texts"]), r)

    # ---- wiring: agent.py installs the pin when compaction is enabled ----
    # Subprocess: THIS process installed the pin by hand above, so only a
    # fresh interpreter can prove the _make_compaction_config() hook fires.
    import subprocess
    wiring_env = {
        **os.environ,
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_API_KEY": "stub", "PYTHONPATH": str(REPO / "agents"),
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "100000",
        "ADK_CC_COMPACTION_EVENT_RETENTION": "8",
    }
    probe = ("import adk_cc.agent; from google.adk.apps import compaction; "
             "print('pin=%s' % getattr(compaction, '_adk_cc_pin', False))")
    r = subprocess.run([sys.executable, "-c", probe], env=wiring_env,
                       capture_output=True, text=True, timeout=120)
    check("agent.py wires the pin when compaction is enabled",
          "pin=True" in r.stdout, (r.stdout[-200:], r.stderr[-300:]))

    # ---- unit: the pending-set semantics ---------------------------------
    p = C._pending_function_call_ids(_parked_history(stash_click=True))
    check("pending set = {c1, w1} while parked", p == {"c1", "w1"}, p)
    done = C._pending_function_call_ids(_resolved_history())
    check("pending set empty once truly answered", done == set(), done)
    ordinary = C._pending_function_call_ids([
        _ev(1, calls=[("run_bash", "t1")]),
        _ev(2, resps=[("run_bash", "t1", {"stdout": "hi"})], author="user"),
    ])
    check("ordinary tool responses still count as answers",
          ordinary == set(), ordinary)
    gated_tool = C._pending_function_call_ids([
        _ev(1, calls=[("exit_plan_mode", "p1")]),
        _ev(2, resps=[("exit_plan_mode", "p1",
                       {"status": "awaiting_user_confirmation"})],
            author="user"),
    ])
    check("approval-gated tools' interim close is not an answer either",
          gated_tool == {"p1"}, gated_tool)

    # The interim statuses the pin recognizes must be the ones the gates
    # actually emit (permissions.py / tools/base.py).
    import adk_cc.plugins.permissions as P
    import adk_cc.tools.base as TB
    psrc, tbsrc = inspect.getsource(P), inspect.getsource(TB)
    check("permissions gate still emits needs_confirmation",
          '"status": "needs_confirmation"' in psrc
          and "needs_confirmation" in PIN._PARKED_STATUSES)
    check("tool approval gate still emits awaiting_user_confirmation",
          '"status": "awaiting_user_confirmation"' in tbsrc
          and "awaiting_user_confirmation" in PIN._PARKED_STATUSES)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
