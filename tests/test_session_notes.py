"""Session notes (#127 P0): tool semantics + injection + compaction survival.

The load-bearing A/B: a decision recorded ONLY in session notes must still
reach the model's request AFTER event compaction folded the turn that
recorded it — injected blocks are re-materialized from state each turn,
immune by position. The same scenario without the NotesPlugin fails.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_session_notes.py
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
os.environ.pop("ADK_CC_SESSION_NOTES_BUDGET", None)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def tool_units() -> None:
    from adk_cc.tools.session_notes import (
        STATE_KEY, UpdateSessionNotesArgs, UpdateSessionNotesTool,
    )

    class Ctx:
        def __init__(self):
            self.state = {}

    t = UpdateSessionNotesTool()
    ctx = Ctx()
    r = asyncio.run(t._execute(
        UpdateSessionNotesArgs(content="DECISION: approach B"), ctx))
    check("append into empty", ctx.state[STATE_KEY] == "DECISION: approach B"
          and r["status"] == "ok")
    asyncio.run(t._execute(
        UpdateSessionNotesArgs(content="GOTCHA: API paginates at 100"), ctx))
    check("append stacks", "approach B" in ctx.state[STATE_KEY]
          and "paginates" in ctx.state[STATE_KEY])
    asyncio.run(t._execute(
        UpdateSessionNotesArgs(content="fresh", mode="replace"), ctx))
    check("replace rewrites", ctx.state[STATE_KEY] == "fresh")

    os.environ["ADK_CC_SESSION_NOTES_BUDGET"] = "200"  # -> 800 chars
    try:
        ctx2 = Ctx()
        for i in range(30):
            asyncio.run(t._execute(
                UpdateSessionNotesArgs(content=f"line {i}: " + "x" * 60), ctx2))
        s = ctx2.state[STATE_KEY]
        check("cap trims oldest, keeps newest",
              len(s) <= 800 and "line 29" in s and "line 0:" not in s,
              (len(s), s[:40]))
    finally:
        os.environ.pop("ADK_CC_SESSION_NOTES_BUDGET", None)


async def _runner_scenario(with_plugin: bool) -> dict:
    """Turn 1 records a note via the REAL tool; compaction folds turn 1;
    turn 2's request is captured — does it still carry the note?"""
    import e2e_confirmation_flow as T
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.apps import compaction as C
    from google.adk.apps.app import EventsCompactionConfig
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from adk_cc.plugins.session_notes import NotesPlugin
    from adk_cc.tools.session_notes import UpdateSessionNotesTool
    from test_compaction_pin import _make_stub_summarizer

    seen_instructions: list = []

    class _SpyLlm(T._ScriptedLlm):
        async def generate_content_async(self, llm_request, stream=False):  # noqa: ANN001
            seen_instructions.append(
                str(getattr(llm_request.config, "system_instruction", "") or ""))
            async for r in super().generate_content_async(llm_request, stream):
                yield r

    call = LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(
            id="N-1", name="update_session_notes",
            args={"content": "DECISION: use approach B (xylophone-77)",
                  "mode": "append"}))]))
    agent = LlmAgent(
        name="test_agent",
        model=_SpyLlm(responses=[call, T._text_response("noted." + "pad " * 300),
                                 T._text_response("second turn reply"),
                                 T._text_response("spare")]),
        tools=[UpdateSessionNotesTool()])
    app = App(name="notes_ab", root_agent=agent,
              plugins=[NotesPlugin()] if with_plugin else [],
              events_compaction_config=EventsCompactionConfig(
                  token_threshold=1, event_retention_size=0,
                  compaction_interval=10, overlap_size=2,
                  summarizer=_make_stub_summarizer()))
    runner = InMemoryRunner(app=app)
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id="u", session_id="s")

    async def run(text):
        async for _ in runner.run_async(
                user_id="u", session_id="s",
                new_message=types.Content(role="user",
                                          parts=[types.Part(text=text)])):
            pass

    await run("please record the decision " + "filler " * 200)
    # Force compaction over turn 1 if the runner didn't already.
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id="s")
    if not any(e.actions and e.actions.compaction for e in sess.events):
        await C._run_compaction_for_token_threshold_config(
            config=app.events_compaction_config, session=sess,
            session_service=runner.session_service, agent=agent)
    sess = await runner.session_service.get_session(
        app_name=runner.app_name, user_id="u", session_id="s")
    compacted = any(e.actions and e.actions.compaction for e in sess.events)
    await run("so which approach did we pick?")
    return {"compacted": compacted,
            "final_instruction": seen_instructions[-1] if seen_instructions else ""}


def promote_and_routing_units() -> None:
    import tempfile
    from adk_cc.tools.session_notes import (
        UpdateSessionNotesArgs, UpdateSessionNotesTool,
    )

    class TC:  # tenant context stub
        tenant_id, user_id = "local", "kim"

    class Ctx:
        def __init__(self):
            self.state = {"temp:tenant_context": TC()}

    t = UpdateSessionNotesTool()
    # promote without memory enabled → clear error
    os.environ.pop("ADK_CC_MEMORY", None)
    r = asyncio.run(t._execute(UpdateSessionNotesArgs(
        content="uses pandas 2.x", mode="promote"), Ctx()))
    check("promote without ADK_CC_MEMORY errors clearly",
          r["status"] == "error" and "ADK_CC_MEMORY" in r["error"])
    # promote with memory on → lands in the user's episodic store
    root = tempfile.mkdtemp(prefix="notes-promote-")
    os.environ.update({"ADK_CC_MEMORY": "1", "ADK_CC_MEMORY_ROOT": root})
    try:
        r = asyncio.run(t._execute(UpdateSessionNotesArgs(
            content="uses pandas 2.x", mode="promote"), Ctx()))
        check("promote writes episodic memory", r.get("promoted") is True, r)
        import glob
        hits = glob.glob(os.path.join(root, "local", "users", "kim",
                                      "episodic", "*.md"))
        check("promoted item on disk under the USER", len(hits) == 1, hits)
    finally:
        os.environ.pop("ADK_CC_MEMORY", None)
        os.environ.pop("ADK_CC_MEMORY_ROOT", None)

    # P1 prompt routing: flag off → no SESSION instruction; on → present (web)
    from adk_cc.plugins.memory import _capture_prompt
    os.environ.pop("ADK_CC_SESSION_NOTES_AUTOCAPTURE", None)
    off = _capture_prompt("T")
    os.environ["ADK_CC_SESSION_NOTES_AUTOCAPTURE"] = "1"
    try:
        on = _capture_prompt("T")
    finally:
        os.environ.pop("ADK_CC_SESSION_NOTES_AUTOCAPTURE", None)
    check("P1 routing line only under the flag",
          "SESSION:" not in off and "SESSION:" in on)


def main() -> int:
    tool_units()
    promote_and_routing_units()

    r = asyncio.run(_runner_scenario(with_plugin=True))
    check("A/B with plugin: compaction ran", r["compacted"])
    check("A/B with plugin: note survives INTO the post-compaction request",
          "xylophone-77" in r["final_instruction"],
          r["final_instruction"][-200:])

    r2 = asyncio.run(_runner_scenario(with_plugin=False))
    check("A/B without plugin: note is ABSENT from the request "
          "(proves injection, not the transcript, carries it)",
          "xylophone-77" not in r2["final_instruction"])

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
