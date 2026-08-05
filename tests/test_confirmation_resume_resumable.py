"""Confirmation answers must survive ADK resumability (#114 root cause).

The durable-runs work set `ResumabilityConfig(is_resumable=True)` (agent.py),
and that flag broke EVERY confirmation resume in production: ADK's
`_resolve_invocation_id` maps a function-response message back to the GATED
invocation, which already ended (the gate's dict return closes the call, the
turn completes parked on the user), so `run_async` hits its
`end_of_agents -> return` short-circuit and yields ZERO events — tools never
re-run, the model is never called, and the broker's auto-continue turned the
silence into a misleading "Continue.". Looked skills-specific live only
because skills gate every time while bash accumulates allow_always rules and
rarely exercises resume.

Reproduced 2026-08-05 by one-flag A/B toggle; fixed by routing
confirmation-only messages into a NEW invocation
(`install_confirmation_resume_fix`), the path proven correct without
resumability. This test pins the whole matrix, using the REAL PermissionPlugin
skill gate + REAL ConfirmationFormUiPlugin + REAL ADK resume — only the LLM
and the tool body are fakes.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_confirmation_resume_resumable.py
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

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


async def _scenario(*, resumable: bool, with_fix: bool) -> dict:
    """Two gated run_skill_script calls; allow A, then allow B. Returns what ran."""
    import e2e_confirmation_flow as T
    from google.adk.agents import LlmAgent
    from google.adk.apps import App, ResumabilityConfig
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

    two = LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(
            id="A-1", name="run_skill_script",
            args={"skill_name": "g", "file_path": "scripts/a.py"})),
        types.Part(function_call=types.FunctionCall(
            id="B-2", name="run_skill_script",
            args={"skill_name": "g", "file_path": "scripts/b.py"})),
    ]))
    agent = LlmAgent(
        name="test_agent",
        model=T._ScriptedLlm(responses=[two, T._text_response("done after tools"),
                                        T._text_response("spare")]),
        tools=[FunctionTool(run_skill_script)])
    plugins = [PermissionPlugin(settings=T.SettingsHierarchy()),
               ConfirmationFormUiPlugin()]
    if resumable:
        app = App(name="conf_resume", root_agent=agent, plugins=plugins,
                  resumability_config=ResumabilityConfig(is_resumable=True))
        runner = InMemoryRunner(app=app)
    else:
        runner = InMemoryRunner(agent=agent, plugins=plugins,
                                app_name="conf_resume")
    if with_fix:
        install_confirmation_resume_fix(runner)

    await runner.session_service.create_session(
        app_name=runner.app_name, user_id="u", session_id="s")

    async def run(msg):
        out = []
        async for ev in runner.run_async(user_id="u", session_id="s",
                                         new_message=msg):
            out.append(ev)
        return out

    ev1 = await run(types.Content(role="user", parts=[types.Part(text="go")]))
    wraps = [fc.id for e in ev1 for fc in e.get_function_calls()
             if fc.name in ("adk_request_confirmation", "adk_cc_confirmation_form")]
    assert len(wraps) == 2, f"expected 2 wraps, got {len(wraps)}"

    def answer(wid):
        return types.Content(role="user", parts=[types.Part(
            function_response=types.FunctionResponse(
                id=wid, name="adk_cc_confirmation_form",
                response={"chose_id": "allow_once"}))])

    await run(answer(wraps[0]))
    ev3 = await run(answer(wraps[1]))
    texts = [p.text for e in ev3 for p in (e.content.parts if e.content else [])
             if getattr(p, "text", None)]
    return {"ran": list(ran), "model_text": texts}


def main() -> int:
    r = asyncio.run(_scenario(resumable=False, with_fix=False))
    check("baseline (non-resumable): both scripts run after allow A+B",
          len(r["ran"]) == 2, r)

    r = asyncio.run(_scenario(resumable=True, with_fix=False))
    check("the BUG is real: resumable app WITHOUT the fix resumes nothing "
          "(if this fails, ADK fixed it upstream — retire the fix)",
          len(r["ran"]) == 0, r)

    r = asyncio.run(_scenario(resumable=True, with_fix=True))
    check("resumable app WITH the fix: both scripts run", len(r["ran"]) == 2, r)
    check("…and the model actually replies afterwards",
          any("done after tools" in t for t in r["model_text"]), r)

    # The fix must not reroute anything that is not a confirmation answer.
    from google.genai import types
    from adk_cc.service.turns import _is_confirmation_answer
    check("a plain text message is not rerouted",
          not _is_confirmation_answer(types.Content(role="user", parts=[
              types.Part(text="hello")])))
    check("an ask_user_question answer is not rerouted",
          not _is_confirmation_answer(types.Content(role="user", parts=[
              types.Part(function_response=types.FunctionResponse(
                  id="q1", name="ask_user_question", response={"a": 1}))])))
    check("a confirmation answer IS rerouted",
          _is_confirmation_answer(types.Content(role="user", parts=[
              types.Part(function_response=types.FunctionResponse(
                  id="c1", name="adk_cc_confirmation_form",
                  response={"chose_id": "allow"}))])))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
