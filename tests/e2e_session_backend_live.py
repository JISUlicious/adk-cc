"""E2E: the per-session backend endpoint flips config→live after a REAL turn.

Runs ADK's real runtime (InMemoryRunner + desktop TenancyPlugin + real
BashTool) with a scripted LLM. Before the turn, `/desktop/sessions/backend`
reports source="config"; after the turn, the SAME session id reports
source="live" with the backend TenancyPlugin actually resolved — proving the
seeding hook fires in the real before-run chain and the route reads it back.

No Docker needed (backend resolves to noop on this host).

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_session_backend_live.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from typing import Any, AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"

_TMP = tempfile.mkdtemp(prefix="adk-cc-sbl-e2e-")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP

try:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from pydantic import Field
except Exception as e:  # pragma: no cover
    print(f"[SKIP] google-adk not importable: {e}")
    sys.exit(0)

from adk_cc.service.desktop_routes import save_projects
from adk_cc.service.desktop_workspace import desktop_tenant_resolver
from adk_cc.service.tenancy import TenancyPlugin
from adk_cc.tools import BashTool


class _ScriptedLlm(BaseLlm):
    model: str = "fake/scripted-sbl"
    responses: list[LlmResponse] = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake/scripted-sbl"]

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield self.responses.pop(0)


def _status(session_id: str) -> dict:
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.service.desktop_routes import mount_desktop_routes

    app = FastAPI()
    mount_desktop_routes(app)
    r = TestClient(app).get(
        "/desktop/sessions/backend", params={"session_id": session_id}
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _run_turn(project_id: str, session_id: str) -> None:
    llm = _ScriptedLlm(
        responses=[
            LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="c1", name="run_bash", args={"command": "echo ok"}
                            )
                        )
                    ],
                ),
                partial=False,
            ),
            LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="done")]),
                partial=False,
            ),
        ]
    )
    agent = LlmAgent(name="sbl_agent", model=llm, instruction="t", tools=[BashTool()])
    runner = InMemoryRunner(
        agent=agent,
        plugins=[TenancyPlugin(tenant_resolver=desktop_tenant_resolver)],
        app_name="e2e-sbl",
    )
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=project_id, session_id=session_id
    )
    async for _ in runner.run_async(
        user_id=project_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text="go")]),
    ):
        pass


def main() -> int:
    repo = os.path.join(_TMP, "proj")
    os.makedirs(repo)
    project_id, session_id = "projSbl", "sessSbl"
    save_projects([{"id": project_id, "name": "proj", "repo_path": repo}])

    failures: list[str] = []

    before = _status(session_id)
    if before.get("source") != "config":
        failures.append(f"before turn: expected source=config, got {before}")

    asyncio.run(_run_turn(project_id, session_id))

    after = _status(session_id)
    if after.get("source") != "live":
        failures.append(f"after turn: expected source=live, got {after}")
    elif after.get("backend") != "noop" or after.get("isolated") is not False:
        failures.append(f"after turn: unexpected backend info {after}")

    shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print("FAIL — session-backend live e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("  [PASS] before first turn: source=config")
    print("  [PASS] after a REAL turn: source=live, backend=noop (resolved)")
    print("\nsession-backend live e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
