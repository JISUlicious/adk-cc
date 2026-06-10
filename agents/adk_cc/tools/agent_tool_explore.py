"""Parallel, attributable subagent exploration via an enriched AgentTool.

Why this exists
---------------
adk-cc's `Explore` specialist is a *transfer* sub-agent: delegation is
sequential (one active agent at a time). When you want to fan OUT — spawn
several explorers at once for independent questions ("find the auth flow",
"map the sandbox backends", "research X on the web") — you want the model to
pick the count and tasks at runtime AND have them run concurrently.

ADK already gives that when an agent is exposed as an `AgentTool`: the model
can emit several tool calls in ONE response and ADK dispatches them
concurrently (`flows/llm_flows/functions.py` — `asyncio.create_task` per call
+ `asyncio.gather`). Each `AgentTool` call runs the sub-agent in its own
isolated session (its own context budget — ideal for exploration) and returns
its report.

The gap this fills
------------------
Vanilla `AgentTool` returns ONLY the sub-agent's final text. When N explorers
fire in parallel their results come back unordered and unlabeled — a bare
string is hard to attribute to the question that produced it, and you get no
visibility into how much work each explorer did. `EnrichedAgentTool` wraps the
result in a structured envelope built from the child run's event stream:

    {
      "task": "<the request the explorer was given>",
      "agent": "code_explore",
      "ok": true,
      "elapsed_s": 4.12,
      "tool_calls": 7,
      "tools_used": ["grep", "read_file"],
      "events": 15,
      "report": "<the explorer's findings>",
    }

So the coordinator can tell which finding answers which question, see
per-explorer cost, and notice failures (`ok=false` + `error`) instead of a
silent empty string.

The `run_async` override mirrors `google.adk.tools.AgentTool.run_async`
(nested Runner + isolated InMemory session + state/artifact forwarding) and
instruments the event loop to collect the envelope fields.

Opt-in: wired into the coordinator only when `ADK_CC_AGENT_TOOL_EXPLORE=1`
(see agent.py), so the default tool surface is unchanged.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.tools._forwarding_artifact_service import ForwardingArtifactService
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.tool_context import ToolContext
from google.adk.utils.context_utils import Aclosing
from google.genai import types


# --- concurrency cap ------------------------------------------------------
# At most N explorers run their nested execution AT ONCE — across all explore
# tools, regardless of how many the model spawns in a single turn. Excess
# calls queue on this semaphore (their `queued_s` reflects the wait) and run
# as slots free up. Process-global: one resource guard for the model endpoint
# / host. The model still picks the COUNT freely; this only bounds how many
# run concurrently. Tune with ADK_CC_AGENT_TOOL_EXPLORE_MAX (default 8).
_DEFAULT_MAX = 8
_gate: Optional[asyncio.Semaphore] = None


def explore_concurrency_limit() -> int:
    try:
        return max(1, int(os.environ.get("ADK_CC_AGENT_TOOL_EXPLORE_MAX", _DEFAULT_MAX)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX


def _explore_gate() -> asyncio.Semaphore:
    # Lazily created so the env var is read at first use, not import time.
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(explore_concurrency_limit())
    return _gate


def _reset_gate_for_test(n: int) -> None:
    """Test hook: rebuild the gate with a specific concurrency limit."""
    global _gate
    _gate = asyncio.Semaphore(n)


def enrich_result(
    report: str,
    *,
    task: str,
    agent: str,
    ok: bool = True,
    error: Optional[str] = None,
    elapsed_s: float = 0.0,
    queued_s: float = 0.0,
    tool_calls: int = 0,
    tools_used: Optional[list[str]] = None,
    events: int = 0,
) -> dict[str, Any]:
    """Build the structured envelope returned to the coordinator.

    Pure + side-effect free so it can be unit-tested without spinning up a
    nested runner. `task` is echoed back so parallel results — which return
    unordered — are attributable to the question that produced them.
    `queued_s` is how long this call waited for a concurrency slot (>0 when
    the model spawned more than the cap allows to run at once).
    """
    env: dict[str, Any] = {
        "task": task,
        "agent": agent,
        "ok": ok,
        "elapsed_s": round(elapsed_s, 3),
        "queued_s": round(queued_s, 3),
        "tool_calls": tool_calls,
        "tools_used": tools_used or [],
        "events": events,
        "report": report,
    }
    if error:
        env["error"] = error
    return env


class EnrichedAgentTool(AgentTool):
    """`AgentTool` whose result is the `enrich_result` envelope.

    Behaves exactly like `AgentTool` for declaration/dispatch (so the model
    can spawn several in one turn and ADK runs them in parallel), but returns
    a structured, attributable dict instead of a bare string.
    """

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        # Lazy import: keeps this module importable in tests that don't need
        # the runner, and mirrors AgentTool's own lazy Runner import.
        from google.adk.runners import Runner
        from google.adk.sessions.in_memory_session_service import (
            InMemorySessionService,
        )

        task = args.get("request", "") if isinstance(args, dict) else ""
        if self.skip_summarization:
            tool_context.actions.skip_summarization = True

        # Input: mirror AgentTool — structured input_schema if the agent
        # declares one, else the default `request` string becomes the message.
        from google.adk.tools.agent_tool import _get_input_schema

        input_schema = _get_input_schema(self.agent)
        if input_schema:
            input_value = input_schema.model_validate(args)
            content = types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=input_value.model_dump_json(exclude_none=True)
                    )
                ],
            )
        else:
            content = types.Content(
                role="user",
                parts=[types.Part.from_text(text=args.get("request", ""))],
            )

        invocation_context = tool_context._invocation_context
        parent_app_name = (
            invocation_context.app_name if invocation_context else None
        )
        child_app_name = parent_app_name or self.agent.name
        plugins = (
            invocation_context.plugin_manager.plugins
            if self.include_plugins
            else None
        )
        tool_calls = 0
        tools_used: list[str] = []
        n_events = 0
        last_content = None
        ok = True
        error: Optional[str] = None

        # Concurrency cap: wait for a slot. Excess explorers (model spawned
        # more than the cap) queue here; queued_s captures the wait. Only the
        # nested run is gated — declaration/dispatch stay parallel, so ADK
        # still fires the whole batch; the cap just bounds how many RUN at once.
        t_start = time.perf_counter()
        async with _explore_gate():
            queued_s = time.perf_counter() - t_start
            runner = Runner(
                app_name=child_app_name,
                agent=self.agent,
                artifact_service=ForwardingArtifactService(tool_context),
                session_service=InMemorySessionService(),
                memory_service=InMemoryMemoryService(),
                credential_service=invocation_context.credential_service,
                plugins=plugins,
            )
            state_dict = {
                k: v
                for k, v in tool_context.state.to_dict().items()
                if not k.startswith("_adk")
            }
            session = await runner.session_service.create_session(
                app_name=child_app_name,
                user_id=invocation_context.user_id,
                state=state_dict,
            )

            # --- instrumented run loop (the enrichment) ---
            t0 = time.perf_counter()
            try:
                async with Aclosing(
                    runner.run_async(
                        user_id=session.user_id,
                        session_id=session.id,
                        new_message=content,
                    )
                ) as agen:
                    async for event in agen:
                        n_events += 1
                        if event.actions and event.actions.state_delta:
                            tool_context.state.update(event.actions.state_delta)
                        for fc in event.get_function_calls() or []:
                            tool_calls += 1
                            if fc.name and fc.name not in tools_used:
                                tools_used.append(fc.name)
                        if event.content:
                            last_content = event.content
            except Exception as e:  # one explorer failing must not poison the batch
                ok = False
                error = f"{type(e).__name__}: {e}"
            finally:
                await runner.close()
            elapsed = time.perf_counter() - t0
        report = ""
        if last_content is not None and last_content.parts:
            report = "\n".join(
                p.text for p in last_content.parts if p.text and not p.thought
            )
        return enrich_result(
            report,
            task=task,
            agent=self.agent.name,
            ok=ok,
            error=error,
            elapsed_s=elapsed,
            queued_s=queued_s,
            tool_calls=tool_calls,
            tools_used=tools_used,
            events=n_events,
        )
