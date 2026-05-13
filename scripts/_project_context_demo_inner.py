"""Inner script: drives a one-turn InMemoryRunner so the
ProjectContextPlugin fires in the real plugin chain."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import AsyncGenerator

from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

# Importing adk_cc.agent runs configure_logging() with our env vars
# AND registers the full plugin chain (including ProjectContextPlugin).
from adk_cc import agent  # noqa: F401
from adk_cc.plugins import (
    AuditPlugin,
    ContextGuardPlugin,
    ModelIOTracePlugin,
    PermissionPlugin,
    PlanModeReminderPlugin,
    ProjectContextPlugin,
    TaskReminderPlugin,
    ToolCallValidatorPlugin,
)
from adk_cc.permissions import SettingsHierarchy


class _ScriptedLlm(BaseLlm):
    model: str = "fake/scripted-context-demo"
    responses: list[LlmResponse] = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake/.*"]

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not self.responses:
            raise RuntimeError("_ScriptedLlm queue empty")
        yield self.responses.pop(0)


def _text(t: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]))


async def run() -> int:
    llm = _ScriptedLlm(
        responses=[
            _text("OK — I'll use uv and put tests under tests/."),
        ]
    )
    agent_inst = LlmAgent(
        name="ctx_demo_agent",
        model=llm,
        instruction="You are a demo agent.",
    )
    app = App(
        name="ctx_demo",
        root_agent=agent_inst,
        # Same plugin order as agent.py (the relevant ones for this demo).
        plugins=[
            AuditPlugin(),
            PermissionPlugin(SettingsHierarchy.empty()),
            ProjectContextPlugin(),
            PlanModeReminderPlugin(default_mode="default"),
            TaskReminderPlugin(default_mode="default"),
            ToolCallValidatorPlugin(default_mode="default"),
            ContextGuardPlugin(),
            ModelIOTracePlugin(),  # last — captures final state
        ],
    )
    runner = InMemoryRunner(app=app)
    user_id = "alice"
    session_id = f"demo-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )

    print(f"[demo] session_id={session_id}")
    print(f"[demo] cwd={os.getcwd()}")
    print(f"[demo] expected: CLAUDE.md picked up, project_context_loaded audit event,")
    print(f"[demo]           and the model_request audit event shows the prepended block")
    print(f"[demo] --- turn 1 ---")
    async for _ in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part(text="what's the project convention?")]
        ),
    ):
        pass

    print("[demo] turn complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
