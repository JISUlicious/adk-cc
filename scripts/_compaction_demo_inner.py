"""Inner script for compaction_demo.py — runs inside a subprocess so
the agent's `configure_logging()` at import sees our ADK_CC_LOG_LEVEL.

Builds an InMemoryRunner with a scripted LLM (so no model server is
needed), plugs our `_LazyAdkCcSummarizer` into `EventsCompactionConfig`
with a low threshold, and drives 3 invocations to cross the threshold.

The inner `LlmEventSummarizer.maybe_summarize_events` is patched to
return a fake compacted event — that part is the only mock, and only
because the demo isn't trying to test the summarizer LLM call (PR A's
scope is the audit/log wrapping, not the summarizer itself).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import AsyncGenerator
from unittest.mock import patch

from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

# Importing the agent runs configure_logging() with our env vars.
from adk_cc import agent  # noqa: F401  (side effects)
from adk_cc.agent import _make_lazy_summarizer_class
from adk_cc.plugins.audit import AuditPlugin


# --- Scripted LLM (no model server needed) -------------------------


class _ScriptedLlm(BaseLlm):
    """Yields the next LlmResponse from `responses` per call."""

    model: str = "fake/scripted-compaction-demo"
    responses: list[LlmResponse] = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake/.*"]

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not self.responses:
            raise RuntimeError(
                "_ScriptedLlm queue empty — under-queued for this demo."
            )
        # Yield real lengthy text so prompt-token estimate grows.
        yield self.responses.pop(0)


def _text(content: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=content)])
    )


# --- Stub summarizer (no LLM call inside) --------------------------


async def _fake_inner_summarize(self, *, events):  # noqa: ANN001
    """Replaces LlmEventSummarizer.maybe_summarize_events. Returns a
    canned real Event with EventCompaction action so ADK's session
    service accepts it. No LLM call inside — keeps the demo
    model-server-free."""
    from google.adk.events.event import Event
    from google.adk.events.event_actions import EventActions, EventCompaction
    n = len(events) if events else 0
    last_ts = getattr(events[-1], "timestamp", 0.0) if events else 0.0
    first_ts = getattr(events[0], "timestamp", 0.0) if events else 0.0
    return Event(
        invocation_id="demo-inv",
        author="demo_agent",
        actions=EventActions(
            compaction=EventCompaction(
                start_timestamp=first_ts,
                end_timestamp=last_ts,
                compacted_content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"[demo compaction summary of {n} events]")],
                ),
            ),
        ),
    )


# --- Drive the runner ---------------------------------------------


async def run() -> int:
    # Long-ish prompts so prompt_token_count (chars/4 fallback) grows
    # fast enough to cross the 200-token threshold within a few turns.
    long_text = (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        "Sed do eiusmod tempor incididunt ut labore et dolore magna "
        "aliqua. " * 8
    )

    llm = _ScriptedLlm(
        responses=[
            _text(f"first turn response. {long_text}"),
            _text(f"second turn response. {long_text}"),
            _text(f"third turn response. {long_text}"),
        ]
    )

    agent_inst = LlmAgent(
        name="demo_agent",
        model=llm,
        instruction="You are a demo agent for compaction observability.",
    )

    # Plug in our wrapper around the (mocked) inner summarizer.
    cls = _make_lazy_summarizer_class()
    summarizer = cls(
        model_id="fake/scripted-compaction-demo",
        api_base=None,
        api_key="dummy",
    )
    compaction_config = EventsCompactionConfig(
        token_threshold=200,
        event_retention_size=2,
        compaction_interval=10,
        overlap_size=2,
        summarizer=summarizer,
    )

    # Wire AuditPlugin so audit events go to ADK_CC_AUDIT_LOG (read
    # by the plugin's __init__).
    app = App(
        name="compaction_demo",
        root_agent=agent_inst,
        plugins=[AuditPlugin()],
        events_compaction_config=compaction_config,
    )
    runner = InMemoryRunner(app=app)
    user_id = "alice"
    session_id = f"demo-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )

    print(f"[demo] session_id={session_id}")
    print(f"[demo] threshold=200 tokens, retention=2 events")

    # Patch the inner LlmEventSummarizer so the wrapper runs cleanly
    # without an LLM call. The OUTER wrapper (our _LazyAdkCcSummarizer)
    # is NOT patched — it's exercised end-to-end.
    with patch(
        "google.adk.apps.llm_event_summarizer.LlmEventSummarizer.maybe_summarize_events",
        new=_fake_inner_summarize,
    ):
        for turn in (1, 2, 3):
            print(f"[demo] --- turn {turn} ---")
            async for ev in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=types.Content(
                    role="user",
                    parts=[types.Part(text=f"turn {turn} prompt. {long_text}")],
                ),
            ):
                pass

    # Inspect the final session to confirm a compaction event landed.
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    n_events = len(session.events)
    n_compaction = sum(
        1
        for e in session.events
        if getattr(e.actions, "compaction", None) is not None
    )
    print(f"[demo] session has {n_events} events, of which {n_compaction} are compactions")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
