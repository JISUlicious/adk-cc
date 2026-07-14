"""EVIDENCE for review finding #1 (container lifecycle = per-turn vs per-session).

Drives the REAL ADK runtime (InMemoryRunner + TenancyPlugin + the real BashTool)
with the container backend enabled, across TWO turns of the SAME session, and
observes the container directly:

  turn 1: write an EPHEMERAL marker to /tmp (a container tmpfs, NOT the bind
          mount) and print the container's hostname (= its id).
  turn 2: read the marker back and print the hostname again.

If the backend is per-SESSION, turn 2 sees the same hostname and the marker is
still there. If it is per-TURN (close() fires after each run and the backend is
rebuilt), turn 2 sees a different hostname and the marker is gone.

Prints a clear VERDICT. SKIPS when no container runtime is present.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_container_lifecycle.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from typing import Any, AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

_TMP = tempfile.mkdtemp(prefix="adk-cc-ctr-life-")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP
os.environ["ADK_CC_SANDBOX_MODE"] = "container"   # opt in
os.environ["ADK_CC_SANDBOX_NETWORK"] = "0"        # not needed; faster

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents"))

try:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.events.event import Event
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from pydantic import Field
except Exception as e:  # pragma: no cover
    print(f"[SKIP] google-adk not importable: {e}")
    sys.exit(0)

from adk_cc.sandbox.backends.container_runtime import detect_runtime
from adk_cc.sandbox.backends.local_container_backend import sweep_orphans
from adk_cc.service.desktop_routes import save_projects
from adk_cc.service.desktop_workspace import desktop_tenant_resolver
from adk_cc.service.tenancy import TenancyPlugin
from adk_cc.tools import BashTool


class _ScriptedLlm(BaseLlm):
    model: str = "fake/scripted-life"
    responses: list = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake/scripted-life"]

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> AsyncGenerator:
        if not self.responses:
            raise AssertionError("scripted queue empty")
        yield self.responses.pop(0)


def _tool_call(cid: str, cmd: str) -> "LlmResponse":
    return LlmResponse(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(id=cid, name="run_bash", args={"command": cmd}))]),
        partial=False)


def _text(t: str) -> "LlmResponse":
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]), partial=False)


def _bash_stdout(events: list) -> str:
    for ev in events:
        for part in getattr(getattr(ev, "content", None), "parts", None) or []:
            fr = getattr(part, "function_response", None)
            if fr is not None and fr.name == "run_bash":
                return (fr.response or {}).get("stdout") or ""
    return ""


async def _turn(runner, pid, sid, cmd) -> str:
    llm: Any = runner.agent.model
    llm.responses = [_tool_call("c", cmd), _text("done")]
    events: list = []
    async for ev in runner.run_async(
        user_id=pid, session_id=sid,
        new_message=types.Content(role="user", parts=[types.Part(text="go")]),
    ):
        events.append(ev)
    return _bash_stdout(events)


async def run() -> int:
    proj = os.path.join(_TMP, "proj")
    os.makedirs(proj)
    pid, sid = "projLife", "sessLife"
    save_projects([{"id": pid, "name": "proj", "repo_path": proj}])

    agent = LlmAgent(name="life_agent", model=_ScriptedLlm(), instruction="t", tools=[BashTool()])
    runner = InMemoryRunner(agent=agent, plugins=[TenancyPlugin(tenant_resolver=desktop_tenant_resolver)],
                            app_name="e2e-life")
    await runner.session_service.create_session(app_name=runner.app_name, user_id=pid, session_id=sid)

    # turn 1: create ephemeral state + report which container we're in
    out1 = await _turn(runner, pid, sid,
                       "echo TURN1-MARKER > /tmp/ephemeral_marker; cat /etc/hostname")
    host1 = out1.strip().splitlines()[-1] if out1.strip() else ""
    # turn 2: is the ephemeral marker still there, and same container?
    out2 = await _turn(runner, pid, sid,
                       "cat /tmp/ephemeral_marker 2>/dev/null || echo MARKER-GONE; cat /etc/hostname")
    lines2 = out2.strip().splitlines()
    marker2 = lines2[0] if lines2 else ""
    host2 = lines2[-1] if lines2 else ""

    print(f"  turn1: container={host1!r}")
    print(f"  turn2: marker={marker2!r} container={host2!r}")

    same_container = host1 and host2 and host1 == host2
    marker_survived = "TURN1-MARKER" in marker2
    print()
    if same_container and marker_survived:
        print("  VERDICT: per-SESSION — container + ephemeral state survive across turns")
        print("  PASS: finding #1 (per-turn churn) is fixed")
        return 0
    print("  VERDICT: per-TURN — the container is recreated every message")
    print(f"    · container id changed between turns: {not same_container}")
    print(f"    · ephemeral /tmp state lost: {not marker_survived}")
    print("  FAIL: finding #1 (per-turn churn) REGRESSED — a bg process / install /")
    print("    non-project state started in one turn is gone the next.")
    return 1


def main() -> int:
    if detect_runtime() is None:
        print("[SKIP] no local container runtime detected")
        return 0
    print("container lifecycle evidence (real runner, 2 turns):")
    try:
        return asyncio.run(run())
    finally:
        sweep_orphans()


if __name__ == "__main__":
    sys.exit(main())
