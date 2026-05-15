"""Bare LlmAgent + adk-cc plugins, NO tools wired.

Demonstrates the "platform features without tools" shape from the
structural review: project context auto-load, audit JSONL, context
guardrail, model-IO trace, optional permission rules — all available
against a bare `LlmAgent(tools=[])`.

What this proves
----------------

  - `adk_cc.plugins.*` modules import cleanly without anything from
    `adk_cc.tools` being present in the runtime plugin chain — the
    PermissionPlugin lazy-import fix from this PR enforces the
    invariant.
  - The bare agent boots through `InMemoryRunner`, `before_model_callback`
    fires, project context loads, model-IO trace records the request,
    audit events land on disk.

Run
---

`.venv/bin/python examples/bare_agent.py`

Seeds a temp project with `CLAUDE.md`, chdirs into it, drives one
scripted-LLM turn, then prints the captured audit JSONL. No external
model server needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import AsyncGenerator

# Set env BEFORE importing adk_cc so configure_logging() and the
# agent-module-level AuditPlugin (constructed during `from adk_cc
# import agent` side-effect) pick up the same audit log path.
_TMP = Path(tempfile.mkdtemp(prefix="bare_agent_demo_"))
_AUDIT_PATH = _TMP / "audit.jsonl"
_PROJECT_DIR = _TMP / "project"
_PROJECT_DIR.mkdir()
(_PROJECT_DIR / "CLAUDE.md").write_text(
    "# Project conventions\n\n"
    "- This is the bare-agent demo project.\n"
    "- Use `uv` not `pip` for dependency management.\n"
    "- Tests live under `tests/`.\n"
)

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-demo")
os.environ["ADK_CC_LOG_LEVEL"] = "INFO"
os.environ["ADK_CC_AUDIT_LOG"] = str(_AUDIT_PATH)
# Turn ModelIOTracePlugin on so we get model_request / model_response
# audit events in the demo output.
os.environ["ADK_CC_LOG_MODEL_IO"] = "1"

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

# Apply env-driven logging (idempotent).
from adk_cc.logging_setup import configure_logging
configure_logging()

# Plugins that DO NOT require any tools. Each subclass of ADK's
# BasePlugin operates on model / event / message hooks only.
from adk_cc.plugins import (
    AuditPlugin,
    ContextGuardPlugin,
    ModelIOTracePlugin,
    PermissionPlugin,
    ProjectContextPlugin,
)
from adk_cc.permissions import SettingsHierarchy


class _ScriptedLlm(BaseLlm):
    """Zero-cost stand-in for a real LLM — yields the queued response
    so the demo runs without a model server."""

    model: str = "fake/bare-agent-demo"
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


def _make_agent() -> LlmAgent:
    return LlmAgent(
        name="bare_agent",
        model=_ScriptedLlm(
            responses=[
                LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text="hello from the bare agent")],
                    )
                )
            ]
        ),
        instruction="You are a bare assistant. No tools available.",
        # Critically: no tools wired. The agent's tool surface is empty.
        tools=[],
    )


def _build_plugin_chain() -> list:
    return [
        # Audit must be first — registers the process-wide audit sink
        # that the other plugins emit through via `emit_audit_event`.
        AuditPlugin(),
        # Permission rules — empty hierarchy means the engine never
        # gates. Listed to prove the plugin loads + runs without
        # `adk_cc.tools` being on the agent's tool list.
        PermissionPlugin(SettingsHierarchy.empty()),
        # Project context auto-load — picks up CLAUDE.md / AGENTS.md
        # / .adk-cc/CONTEXT.md from cwd-upward + user-level.
        ProjectContextPlugin(),
        # Pre-flight context-length guardrail.
        ContextGuardPlugin(),
        # Raw model I/O trace. Placed LAST so before_model_callback
        # captures the final state after all mutators (project context
        # prepend, plan-mode/task reminders if present, etc.).
        ModelIOTracePlugin(),
    ]


async def run() -> int:
    # Chdir into the temp project so ProjectContextPlugin's cwd-walk
    # picks up the seeded CLAUDE.md instead of whatever's around the
    # user's actual workdir.
    prev_cwd = os.getcwd()
    os.chdir(_PROJECT_DIR)
    try:
        runner = InMemoryRunner(
            agent=_make_agent(),
            plugins=_build_plugin_chain(),
            app_name="adk_cc_bare",
        )
        user_id = "alice"
        session_id = f"bare-{uuid.uuid4().hex[:8]}"
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        print(f"[bare-agent] project dir: {_PROJECT_DIR}")
        print(f"[bare-agent] audit log:   {_AUDIT_PATH}")
        print(f"[bare-agent] session_id:  {session_id}")
        print("[bare-agent] running one turn...")
        async for _ in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(
                role="user", parts=[types.Part(text="hi")]
            ),
        ):
            pass
        print("[bare-agent] turn done.\n")
    finally:
        os.chdir(prev_cwd)

    # Surface the audit events the plugins emitted so the demo output
    # proves they actually fired.
    if _AUDIT_PATH.exists():
        print("--- AUDIT JSONL ---")
        for line in _AUDIT_PATH.read_text().splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = evt.get("event")
            if event == "model_request":
                print(
                    f"  model_request   bytes={evt.get('payload_bytes')} "
                    f"tool_count={evt.get('tool_count')} "
                    f"turns={evt.get('content_turns')}"
                )
            elif event == "model_response":
                print(
                    f"  model_response  bytes={evt.get('payload_bytes')} "
                    f"parts={evt.get('parts_count')}"
                )
            elif event == "project_context_loaded":
                srcs = evt.get("sources") or []
                print(
                    f"  project_context_loaded  "
                    f"total_bytes={evt.get('total_bytes')} "
                    f"sources={len(srcs)} "
                    f"first={srcs[0].get('path') if srcs else None}"
                )
            else:
                print(f"  {event}")
    else:
        print(f"[bare-agent] WARN: no audit log at {_AUDIT_PATH}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
