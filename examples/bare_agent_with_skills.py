"""Bare LlmAgent + adk-cc plugins + SkillToolset ONLY.

Same shape as `examples/bare_agent.py`, but adds project skills as
the sole tool surface. Demonstrates that you can adopt skills in a
deployment that omits adk-cc's other built-in tools (read_file,
run_bash, todo_*, ask_user_question, …) entirely.

What this proves
----------------

  - Skill discovery (`make_skill_toolset()`) works against a project's
    `.adk-cc/skills/` directory in a bare setup.
  - The dispatch tools (`list_skills`, `load_skill`,
    `load_skill_resource`, `run_skill_script`) get wired into the
    agent's tool list — no other adk-cc tool dependencies pulled in.
  - All plugin features (audit, project context, model-IO trace) still
    fire alongside the skills tool surface.

Run
---

`.venv/bin/python examples/bare_agent_with_skills.py`

Seeds a temp project with `CLAUDE.md` + a single skill under
`.adk-cc/skills/greeter/SKILL.md`, drives one scripted-LLM turn,
prints the resulting audit JSONL + the toolset's registered skills.
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

_TMP = Path(tempfile.mkdtemp(prefix="bare_agent_skills_demo_"))
_AUDIT_PATH = _TMP / "audit.jsonl"
_PROJECT_DIR = _TMP / "project"
_PROJECT_DIR.mkdir()
(_PROJECT_DIR / "CLAUDE.md").write_text(
    "# Project conventions\n\n"
    "- Bare-agent + skills demo project.\n"
    "- Greet new contributors with the `greeter` skill.\n"
)
# Seed one project skill so make_skill_toolset() returns non-None.
_SKILL_DIR = _PROJECT_DIR / ".adk-cc" / "skills" / "greeter"
_SKILL_DIR.mkdir(parents=True)
(_SKILL_DIR / "SKILL.md").write_text(
    "---\n"
    "name: greeter\n"
    "description: Greet a contributor by name in the project's style.\n"
    "---\n\n"
    "# greeter\n\n"
    "Bare-agent-with-skills demo skill. The agent loads this skill\n"
    "to learn the project's preferred greeting format.\n"
)

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-demo")
os.environ["ADK_CC_LOG_LEVEL"] = "INFO"
os.environ["ADK_CC_AUDIT_LOG"] = str(_AUDIT_PATH)
os.environ["ADK_CC_LOG_MODEL_IO"] = "1"
# Scrub anything that would shadow the project skills dir from a
# user-wide env var leaking into the demo.
os.environ.pop("ADK_CC_SKILLS_DIR", None)
os.environ.pop("ADK_CC_DISABLE_PROJECT_SKILLS", None)

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

from adk_cc.logging_setup import configure_logging
configure_logging()

from adk_cc.plugins import (
    AuditPlugin,
    ContextGuardPlugin,
    ModelIOTracePlugin,
    PermissionPlugin,
    ProjectContextPlugin,
)
from adk_cc.permissions import SettingsHierarchy
# IMPORTANT: importing make_skill_toolset is the ONLY adk_cc.tools
# import this demo uses. The plugin chain itself stays tool-independent.
from adk_cc.tools.skills import make_skill_toolset


class _ScriptedLlm(BaseLlm):
    model: str = "fake/bare-agent-skills-demo"
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


def _make_agent(toolset) -> LlmAgent:
    return LlmAgent(
        name="bare_agent_with_skills",
        model=_ScriptedLlm(
            responses=[
                LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                text="I can dispatch to project skills if asked."
                            )
                        ],
                    )
                )
            ]
        ),
        instruction="You are a bare assistant with access to project skills only.",
        # ONLY tools wired: the SkillToolset's dispatch tools.
        tools=[toolset] if toolset is not None else [],
    )


def _build_plugin_chain() -> list:
    return [
        AuditPlugin(),
        PermissionPlugin(SettingsHierarchy.empty()),
        ProjectContextPlugin(),
        ContextGuardPlugin(),
        ModelIOTracePlugin(),
    ]


async def run() -> int:
    prev_cwd = os.getcwd()
    os.chdir(_PROJECT_DIR)
    try:
        # Discovery has to happen with cwd == _PROJECT_DIR so the
        # `.adk-cc/skills/greeter` we seeded gets picked up.
        toolset = make_skill_toolset()
        if toolset is None:
            print("[bare-agent-skills] FAIL: skill discovery returned None")
            return 1

        registered = toolset._list_skills()
        print(f"[bare-agent-skills] project dir:    {_PROJECT_DIR}")
        print(f"[bare-agent-skills] audit log:      {_AUDIT_PATH}")
        print(
            f"[bare-agent-skills] toolset skills:  "
            f"{[getattr(s, 'name', None) or s.frontmatter.name for s in registered]}"
        )
        dispatch_tools = getattr(toolset, "_tools", [])
        print(
            f"[bare-agent-skills] dispatch tools:  "
            f"{[getattr(t, 'name', repr(t)) for t in dispatch_tools]}"
        )

        runner = InMemoryRunner(
            agent=_make_agent(toolset),
            plugins=_build_plugin_chain(),
            app_name="adk_cc_bare_skills",
        )
        user_id = "alice"
        session_id = f"bare-skills-{uuid.uuid4().hex[:8]}"
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        print(f"[bare-agent-skills] session_id:     {session_id}")
        print("[bare-agent-skills] running one turn...")
        async for _ in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="what skills do you have?")],
            ),
        ):
            pass
        print("[bare-agent-skills] turn done.\n")
    finally:
        os.chdir(prev_cwd)

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
        print(f"[bare-agent-skills] WARN: no audit log at {_AUDIT_PATH}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
