"""Live A/B: is a mid-turn message still honored when the agent can only act
on it SEVERAL tool calls later?

The real adk_cc App, a real model, a real in-process Turn Broker. The task is
three sequential shell steps; a message queued during step 1 asks for one
more command at the end. It is delivered after step 1 and can only be acted
on after step 3 — exactly where the old request-only framing had already
vanished from the model's input.

Arms: `fixed` (persistent framing + coordinator policy) vs `old` (framing on
the delivery request only, raw text persisted). Reports per-arm counts of
rounds where the late ask was carried out.

MEASURED 2026-10-14 (gpt-5.5 via chatgpt-codex), and the result is NULL:
    fixed  4/4      old  4/4
The model honored the late ask in both arms at a 3-tool-call distance. Both
arms also carry the new coordinator-prompt policy, so this isolates only the
persistent framing. Kept as a REGRESSION guard (the fixed arm must stay N/N);
the framing's evidence is deterministic — test_midturn_messages proves the
reminder is present three steps later, and absent under the old scheme.

Run: ADK_CC_LIVE=1 [ROUNDS=3] [ARM=fixed|old] .venv/bin/python tests/e2e_midturn_steering_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "agents"))
os.environ["ADK_CC_SKIP_DOTENV"] = "1"
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.pop("ADK_CC_MEMORY", None)
_TMP = tempfile.mkdtemp(prefix="midturn-live-")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP
os.environ["ADK_CC_WORKSPACE_ROOT"] = _TMP
MODEL = os.environ.get("MIDTURN_MODEL", "chatgpt-codex/gpt-5.5")
ROUNDS = int(os.environ.get("ROUNDS", "3"))

_reg = json.load(open(os.path.expanduser(
    "~/.adk-cc-desktop/admin-data/model-endpoints.json")))
for _ep in _reg.get("endpoints", []):
    if str(_ep.get("model", "")).startswith("chatgpt-codex/"):
        _ep["model"] = MODEL
_reg["active"] = next((e["name"] for e in _reg["endpoints"]
                       if str(e.get("model", "")).startswith("chatgpt-codex/")),
                      _reg.get("active"))
_reg_path = os.path.join(_TMP, "model-endpoints.json")
json.dump(_reg, open(_reg_path, "w"))
os.environ["ADK_CC_MODEL_REGISTRY_FILE"] = _reg_path

TASK = ("Run these three shell commands with run_bash, ONE AT A TIME, in "
        "order, each as its own call: `sleep 6 && echo step-1`, then "
        "`sleep 6 && echo step-2`, then `sleep 6 && echo step-3`. Report "
        "their outputs.")
LATE = ("Also, once those are done, run `echo late-ask-done` with run_bash "
        "and include its output.")


async def _round(arm: str) -> tuple[bool, str]:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    import adk_cc.agent as A
    from adk_cc.plugins import midturn_messages as M
    from adk_cc.service.turns import TurnBroker

    if arm == "old":
        async def old_deliver(self, ictx, turn, llm_request, item):  # noqa: ANN001
            from google.adk.events.event import Event
            ev = Event(invocation_id=ictx.invocation_id, author="user",
                       content=types.Content(role="user", parts=[types.Part(text=item["text"])]),
                       custom_metadata={"adk_cc_midturn": item["id"]})
            await ictx.session_service.append_event(ictx.session, ev)
            llm_request.contents.append(types.Content(role="user", parts=[types.Part(
                text="[The user sent this message while you were working. Read it now "
                     "and adjust what you are doing accordingly — it may change or "
                     "cancel the current plan.]\n\n" + item["text"])]))
        M.MidTurnMessagesPlugin._deliver = old_deliver

    svc = InMemorySessionService()
    runner = Runner(app=A.app, session_service=svc,
                    artifact_service=None, memory_service=None)

    async def get_runner(_):
        return runner

    broker = TurnBroker(get_runner=get_runner, session_service=svc)
    s = await svc.create_session(app_name=A.app.name, user_id="u1", state={
        "session_title": "live", "permission_mode": "bypassPermissions"})
    turn = broker.start(app_name="adk_cc", user_id="u1", session_id=s.id,
                        new_message=types.Content(role="user", parts=[types.Part(text=TASK)]))
    for _ in range(300):                      # wait until step 1 is running
        if turn.current_tool == "run_bash" or turn.status != "running":
            break
        await asyncio.sleep(0.2)
    await broker.enqueue(app_name="adk_cc", user_id="u1", session_id=s.id, text=LATE)
    await asyncio.wait_for(turn.task, 300)
    for t in [turn] + ([broker.get(turn.followup_id)] if turn.followup_id else []):
        if t and t.task and not t.task.done():
            await asyncio.wait_for(t.task, 300)
    sess = await svc.get_session(app_name="adk_cc", user_id="u1", session_id=s.id)
    blob = json.dumps([e.model_dump(mode="json", exclude_none=True) for e in sess.events])
    ran = "late-ask-done" in blob.replace("echo late-ask-done", "")
    steps = all(f"step-{i}" in blob for i in (1, 2, 3))
    return ran and steps, ("ran" if ran else "IGNORED") + ("" if steps else " (steps incomplete)")


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    arm = os.environ.get("ARM", "fixed")
    ok = 0
    for i in range(ROUNDS):
        good, note = asyncio.run(_round(arm))
        ok += good
        print(f"  [{arm}] round {i + 1}: {note}")
    print(f"\n  {arm}: late ask carried out in {ok}/{ROUNDS}")
    return 0 if (arm != "fixed" or ok == ROUNDS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
