"""Tests for the enriched, parallel-capable explore AgentTool
(tools/agent_tool_explore.py).

Covers:
  1. enrich_result envelope (pure, deterministic).
  2. EnrichedAgentTool returns the envelope from a real nested run.
  3. PARALLELISM: several EnrichedAgentTools, when gathered (exactly how ADK's
     function executor dispatches multiple tool calls in one response —
     flows/llm_flows/functions.py uses asyncio.create_task + asyncio.gather),
     run CONCURRENTLY: wall-clock ≈ one delay, not the sum.

Uses a sleepy stub BaseAgent so it's model-free and deterministic.
Hand-rolled (no pytest).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.auth.credential_service.in_memory_credential_service import (
    InMemoryCredentialService,
)
from google.adk.events.event import Event
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from adk_cc.tools.agent_tool_explore import EnrichedAgentTool, enrich_result


# --- 1. pure envelope ----------------------------------------------------

def test_enrich_result_envelope():
    env = enrich_result(
        "found 3 backends",
        task="map sandbox backends",
        agent="code_explore",
        elapsed_s=4.1239,
        tool_calls=7,
        tools_used=["grep", "read_file"],
        events=15,
    )
    assert env == {
        "task": "map sandbox backends",
        "agent": "code_explore",
        "ok": True,
        "elapsed_s": 4.124,  # rounded to 3dp
        "tool_calls": 7,
        "tools_used": ["grep", "read_file"],
        "events": 15,
        "report": "found 3 backends",
    }, env
    print("OK enrich_result_envelope")


def test_enrich_result_error():
    env = enrich_result(
        "", task="t", agent="code_explore", ok=False, error="Boom: nope"
    )
    assert env["ok"] is False and env["error"] == "Boom: nope", env
    print("OK enrich_result_error")


# --- sleepy stub agent (model-free) --------------------------------------

class _SleepyAgent(BaseAgent):
    """Sleeps `delay`s then emits one text event. No model involved."""

    delay: float = 0.5

    async def _run_async_impl(
        self, ctx
    ) -> AsyncGenerator[Event, None]:
        await asyncio.sleep(self.delay)
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"done:{self.name}")],
            ),
        )


async def _make_tool_context() -> ToolContext:
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="t", user_id="u", state={})
    ic = InvocationContext(
        session_service=svc,
        invocation_id="inv-test",
        agent=_SleepyAgent(name="parent"),
        session=session,
        credential_service=InMemoryCredentialService(),
    )
    return ToolContext(invocation_context=ic)


# --- 2. envelope from a real nested run ----------------------------------

async def test_enriched_tool_returns_envelope():
    tc = await _make_tool_context()
    tool = EnrichedAgentTool(_SleepyAgent(name="code_explore", delay=0.1),
                             skip_summarization=True)
    out = await tool.run_async(args={"request": "find X"}, tool_context=tc)
    assert out["task"] == "find X", out
    assert out["agent"] == "code_explore", out
    assert out["ok"] is True, out
    assert "done:code_explore" in out["report"], out
    assert out["events"] >= 1, out
    assert out["elapsed_s"] >= 0.0, out
    print("OK enriched_tool_returns_envelope")


# --- 3. PARALLELISM ------------------------------------------------------

async def test_several_tools_fire_in_parallel():
    """Gather N EnrichedAgentTool.run_async (the same dispatch ADK uses for
    multiple tool calls in one response) → they run concurrently, so total
    wall-clock ≈ one delay, NOT N×delay."""
    tc = await _make_tool_context()
    n, delay = 3, 0.5
    tools = [
        EnrichedAgentTool(_SleepyAgent(name=f"explore{i}", delay=delay),
                          skip_summarization=True)
        for i in range(n)
    ]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        t.run_async(args={"request": f"task{i}"}, tool_context=tc)
        for i, t in enumerate(tools)
    ])
    elapsed = time.perf_counter() - t0

    # Sequential would be ~n*delay = 1.5s; parallel ~delay = 0.5s (+overhead).
    seq = n * delay
    print(f"  elapsed={elapsed:.3f}s  (sequential would be ~{seq:.1f}s)")
    assert elapsed >= delay * 0.8, f"too fast — did the delay run? {elapsed}"
    assert elapsed < seq * 0.7, (
        f"NOT parallel: {elapsed:.3f}s ≈ sequential {seq:.1f}s"
    )
    # Each result is its own attributable envelope.
    for i, r in enumerate(results):
        assert r["ok"] and r["task"] == f"task{i}", r
        assert f"done:explore{i}" in r["report"], r
    print("OK several_tools_fire_in_parallel")


def main():
    test_enrich_result_envelope()
    test_enrich_result_error()
    asyncio.run(test_enriched_tool_returns_envelope())
    asyncio.run(test_several_tools_fire_in_parallel())
    print("\nall agent-tool-explore tests passed")


if __name__ == "__main__":
    main()
