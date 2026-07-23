"""P3 experiment + regression: ADK resumability vs the F3 rooting bug.

F3: a confirmation answered inside a sub-agent starts a NEW invocation rooted
at that sub-agent (`_find_agent_to_run`), so when the specialist finishes and
emits `_handback_to_coordinator`, there is no parent flow — the run ends with
the marker dangling and the coordinator never replies.

ADK's `ResumabilityConfig(is_resumable=True)` changes the resume path: a
function response resolves to its ORIGINAL invocation
(`_resolve_invocation_id`), per-agent states are restored, and the root agent
stays in charge when it participated — so after the specialist finishes, the
coordinator's flow continues naturally.

CAVEAT found by this experiment: ADK ends a resumed parent right after its
sub-agent completes (`llm_agent._run_async_impl`: "run it and then end the
current agent") — so even resumable, the coordinator's REPLY still comes from
the Turn Broker's bounded auto-continue. What resumability buys: a clean
pause (no marker pollution), correct rooting of the resumed run, and no
re-execution of completed tools.

This file proves all of it on the REAL adk_cc App (scripted model, no
network):

  - baseline (not resumable): the dangling-handback repro — documents WHY the
    broker mitigation exists;
  - resumable: the original invocation resumes (specialist completes, root
    preserved) and the BROKER supplies the coordinator's reply;
  - at-least-once probe: tools completed BEFORE the pause do NOT re-execute
    on resume (the risk that gated this to P3);
  - resumable everyday paths: plain turn and coordinator-level confirmation
    behave exactly as before.

Run: `uv run python tests/test_resumability_f3.py`
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
_TMP = tempfile.mkdtemp(prefix="adk-cc-resumability-")
os.environ.setdefault("ADK_CC_DESKTOP", "1")
os.environ.setdefault("ADK_CC_DESKTOP_DATA", _TMP)
os.environ.setdefault("ADK_CC_WORKSPACE_ROOT", _TMP)

from google.adk.apps.app import ResumabilityConfig  # noqa: E402
from google.adk.flows.llm_flows.functions import (  # noqa: E402
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
)
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.tools.tool_confirmation import ToolConfirmation  # noqa: E402
from google.genai import types  # noqa: E402
from pydantic import Field  # noqa: E402

from adk_cc.service.turns import _is_dangling_handback  # noqa: E402


def _text_resp(t: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=t)]),
        partial=False,
    )


def _fc_resp(call_id: str, name: str, args: dict) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(id=call_id, name=name, args=args))]),
        partial=False,
    )


class _ScriptedLlm(BaseLlm):
    """Sequential scripted model shared by coordinator + specialists (they all
    use the same MODEL object, so one queue serves the whole tree in call
    order). Popping an exhausted queue fails loudly — a misaligned resume
    (e.g. an unexpected extra model call) is a test failure, not a hang."""

    model: str = "fake/resumability"
    responses: list = Field(default_factory=list)
    calls: int = 0

    async def generate_content_async(self, req, stream: bool = False):
        self.calls += 1
        if not self.responses:
            raise AssertionError(
                f"scripted queue exhausted at model call #{self.calls}")
        yield self.responses.pop(0)


def _msg(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


# In the real App the ConfirmationFormUiPlugin REWRITES the pause event's
# call name to its form widget; the UI answers under that name and the plugin
# swaps it back on the way in. Resumability's invocation resolution matches by
# call ID, so the rewrite is transparent to it — but the finder must accept
# both names.
_CONFIRMATION_NAMES = {
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
    "adk_cc_confirmation_form",
}


def _confirmation_answer(call_id: str, name: str) -> types.Content:
    """The allow_once submission exactly as the web UI sends it (the form
    plugin accepts the legacy `{chose_id}` shape and reshapes it)."""
    resp = (ToolConfirmation(payload={"chose_id": "allow_once"}).model_dump()
            if name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
            else {"chose_id": "allow_once"})
    return types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(
            id=call_id, name=name, response=resp))])


def _find_confirmation_call(events) -> tuple[str, str]:
    for ev in events:
        for fc in ev.get_function_calls():
            if fc.name in _CONFIRMATION_NAMES:
                return fc.id, fc.name
    raise AssertionError(
        "no confirmation pause happened; authors="
        f"{[getattr(e, 'author', '?') for e in events]}")


def _tool_response_count(events, tool_name: str) -> int:
    """EXECUTIONS of `tool_name`: its function_response events, excluding the
    `needs_confirmation` placeholder a gated (not-yet-run) call emits."""
    n = 0
    for ev in events:
        for fr in ev.get_function_responses():
            if fr.name != tool_name:
                continue
            resp = fr.response if isinstance(fr.response, dict) else {}
            if resp.get("status") == "needs_confirmation":
                continue
            n += 1
    return n


def _coordinator_texts(events) -> list[str]:
    out = []
    for ev in events:
        if getattr(ev, "author", "") != "coordinator":
            continue
        for p in (getattr(ev.content, "parts", None) or []):
            if getattr(p, "text", None) and not getattr(p, "thought", False):
                out.append(p.text)
    return out


async def _run(runner, sid: str, msg: types.Content) -> list:
    events = []
    async for ev in runner.run_async(user_id="u1", session_id=sid,
                                     new_message=msg):
        events.append(ev)
    return events


def _fresh(responses, *, resumable: bool):
    """Real App + scripted model; resumability toggled BEFORE the Runner is
    built (Runner reads app.resumability_config at construction)."""
    import adk_cc.agent as A

    llm = _ScriptedLlm(responses=list(responses))
    A.MODEL._resolve_delegate = lambda: llm
    A.app.resumability_config = (
        ResumabilityConfig(is_resumable=True) if resumable else None)
    svc = InMemorySessionService()
    runner = Runner(app=A.app, session_service=svc,
                    artifact_service=None, memory_service=None)
    return runner, svc, llm


# The gate: reading a protected path (`.git/config`) hits the protected-path
# floor → ask, in ANY mode — the same shape as the live F3 repro (a read-only
# specialist tripping a protected-path confirmation). Benign `run_bash` does
# NOT gate (the danger classifier auto-allows it), so bash can't be the gate.
_F3_SCRIPT = [
    # turn 1: coordinator → Explore; one COMPLETED tool; then the gate
    _fc_resp("fc-transfer", "transfer_to_agent",
             {"agent_name": "Explore"}),
    _fc_resp("fc-glob", "glob_files", {"pattern": "*.md"}),
    _fc_resp("fc-read", "read_file", {"path": ".git/config"}),
    # turn 2 (after allow_once): the specialist's final report. Later model
    # calls differ per test — each appends what it expects to consume.
    _text_resp("explore report: config read ok"),
]


async def _f3_turn1(*, resumable: bool):
    """Run the shared turn 1 to its confirmation pause; hand everything the
    tests need for their own turn 2."""
    import adk_cc.agent as A

    # a real (dummy) protected file so the approved read succeeds harmlessly
    gitdir = os.path.join(_TMP, ".git")
    os.makedirs(gitdir, exist_ok=True)
    with open(os.path.join(gitdir, "config"), "w") as f:
        f.write("[core]\n\tbare = false\n")

    runner, svc, llm = _fresh(_F3_SCRIPT, resumable=resumable)
    s = await svc.create_session(app_name=A.app.name, user_id="u1",
                                 state={"permission_mode": "default"})
    ev1 = await asyncio.wait_for(_run(runner, s.id, _msg("verify the probe")),
                                 60)
    call_id, call_name = _find_confirmation_call(ev1)
    # gated: the protected read must NOT have run pre-approval
    assert _tool_response_count(ev1, "read_file") == 0
    # the pause is CLEAN in both modes: no handback marker mid-pause (the
    # after-agent callback stays silent while a confirmation is outstanding)
    assert not any(_is_dangling_handback(e) for e in ev1), (
        "handback marker leaked into a pausing turn")
    return runner, svc, llm, s.id, call_id, call_name


async def _session_events(svc, sid):
    import adk_cc.agent as A

    return (await svc.get_session(app_name=A.app.name, user_id="u1",
                                  session_id=sid)).events


def test_baseline_reproduces_f3() -> None:
    """Without resumability the answered confirmation starts a NEW run rooted
    at the specialist: it ends on the dangling handback and the coordinator
    never speaks. (Next PLAIN turns root correctly in both modes — pinned
    below — the damage is confined to the resumed turn itself.)"""
    async def main():
        runner, svc, llm, sid, cid, cname = await _f3_turn1(resumable=False)
        ev2 = await asyncio.wait_for(
            _run(runner, sid, _confirmation_answer(cid, cname)), 60)
        assert ev2, "resumed run yielded nothing"
        assert _is_dangling_handback(ev2[-1]), (
            "expected the F3 dangling handback; last event author="
            f"{getattr(ev2[-1], 'author', '?')}")
        assert _coordinator_texts(ev2) == [], _coordinator_texts(ev2)
        assert llm.calls == 4, llm.calls  # coordinator never called again
        full = await _session_events(svc, sid)
        assert _tool_response_count(full, "read_file") == 1  # ran exactly once
        # the next plain turn still roots at the coordinator (damage confined
        # to the resumed turn)
        llm.responses.append(_text_resp("who am I"))
        ev3 = await asyncio.wait_for(_run(runner, sid, _msg("and now?")), 60)
        authors = {getattr(e, "author", "?") for e in ev3} - {"user"}
        assert authors == {"coordinator"}, authors
    asyncio.run(main())
    print("OK test_baseline_reproduces_f3")


def test_resumable_resumes_original_invocation() -> None:
    """With is_resumable the answer RESUMES the original invocation: the
    specialist completes with the root preserved, completed tools do not
    re-run, and the next plain turn roots at the coordinator.

    NOTE ADK ends a resumed parent right after its sub-agent completes
    (llm_agent._run_async_impl) — the coordinator's REPLY is the Turn
    Broker's job (next test), not ADK's."""
    async def main():
        runner, svc, llm, sid, cid, cname = await _f3_turn1(resumable=True)
        ev2 = await asyncio.wait_for(
            _run(runner, sid, _confirmation_answer(cid, cname)), 60)
        # specialist finished its work in the ORIGINAL invocation
        assert any(
            "explore report" in (getattr(p, "text", "") or "")
            for e in ev2
            for p in (getattr(getattr(e, "content", None), "parts", None) or [])
        ), [getattr(e, "author", "?") for e in ev2]
        assert llm.calls == 4, llm.calls
        full = await _session_events(svc, sid)
        # at-least-once probe: nothing completed pre-pause re-executed
        assert _tool_response_count(full, "glob_files") == 1, (
            f"glob_files ran {_tool_response_count(full, 'glob_files')}x — "
            "resume re-executed a completed tool")
        assert _tool_response_count(full, "read_file") == 1
        # rooting is REPAIRED: the next plain turn belongs to the coordinator
        llm.responses.append(_text_resp("back at the top"))
        ev3 = await asyncio.wait_for(_run(runner, sid, _msg("and now?")), 60)
        assert any("back at the top" in t for t in _coordinator_texts(ev3)), (
            [getattr(e, "author", "?") for e in ev3])
    asyncio.run(main())
    print("OK test_resumable_resumes_original_invocation")


def test_resumable_broker_completes_the_turn() -> None:
    """End-to-end P3 property: the confirmation answer driven through the
    Turn Broker — the resumed run ends without a coordinator reply (ADK
    semantics), the broker detects the unanswered handback (which is NOT the
    last event in the resumable shape) and auto-continues, and the coordinator
    finally speaks. One turn, one status, done."""
    from adk_cc.service.turns import TurnBroker

    async def main():
        runner, svc, llm, sid, cid, cname = await _f3_turn1(resumable=True)
        llm.responses.append(_text_resp("COORDINATOR FINAL: probe verified."))

        async def get_runner(app_name):
            return runner

        broker = TurnBroker(get_runner=get_runner, session_service=svc)
        turn = broker.start(app_name="adk_cc", user_id="u1", session_id=sid,
                            new_message=_confirmation_answer(cid, cname))
        await asyncio.wait_for(turn.task, timeout=60)
        assert turn.status == "done", turn.snapshot()
        assert llm.calls == 5, llm.calls  # 4 scripted + 1 auto-continue reply
        full = await _session_events(svc, sid)
        texts = _coordinator_texts(full)
        assert any("COORDINATOR FINAL" in t for t in texts), texts
    asyncio.run(main())
    print("OK test_resumable_broker_completes_the_turn")


def test_resumable_plain_turn_unchanged() -> None:
    """Everyday path: a plain text turn with resumability ON is unaffected."""
    async def main():
        import adk_cc.agent as A

        runner, svc, llm = _fresh([_text_resp("plain reply")], resumable=True)
        s = await svc.create_session(app_name=A.app.name, user_id="u1")
        ev = await asyncio.wait_for(_run(runner, s.id, _msg("hello")), 60)
        assert any("plain reply" in t for t in _coordinator_texts(ev)), (
            [getattr(e, "author", "?") for e in ev])
        assert llm.calls == 1
    asyncio.run(main())
    print("OK test_resumable_plain_turn_unchanged")


def test_resumable_coordinator_level_confirmation() -> None:
    """Everyday path: a confirmation raised by the COORDINATOR itself (the
    common desktop case) still pauses and, on allow_once, continues to a
    normal final reply with resumability ON."""
    async def main():
        import adk_cc.agent as A

        runner, svc, llm = _fresh([
            _fc_resp("fc-read-c", "read_file", {"path": ".git/config"}),
            _text_resp("read it directly"),
        ], resumable=True)
        s = await svc.create_session(app_name=A.app.name, user_id="u1",
                                     state={"permission_mode": "default"})
        ev1 = await asyncio.wait_for(_run(runner, s.id, _msg("read cfg")), 60)
        call_id, call_name = _find_confirmation_call(ev1)
        ev2 = await asyncio.wait_for(
            _run(runner, s.id, _confirmation_answer(call_id, call_name)), 60)
        assert any("read it directly" in t for t in _coordinator_texts(ev2)), (
            [getattr(e, "author", "?") for e in ev2])
        full = (await svc.get_session(app_name=A.app.name, user_id="u1",
                                      session_id=s.id)).events
        assert _tool_response_count(full, "read_file") == 1
        assert llm.calls == 2
    asyncio.run(main())
    print("OK test_resumable_coordinator_level_confirmation")


def main() -> None:
    import adk_cc.agent as A

    prior = A.app.resumability_config
    try:
        test_baseline_reproduces_f3()
        test_resumable_resumes_original_invocation()
        test_resumable_broker_completes_the_turn()
        test_resumable_plain_turn_unchanged()
        test_resumable_coordinator_level_confirmation()
    finally:
        A.app.resumability_config = prior
    print("\nall resumability tests passed")


if __name__ == "__main__":
    main()
