"""E2E: file-panel git change markers reflect a REAL agent turn end-to-end.

Drives ADK's real runtime (InMemoryRunner + desktop TenancyPlugin +
CheckpointPlugin + the real WriteFileTool + NoopBackend) with a scripted LLM
that, in one turn, edits a committed file in place AND creates a brand-new file
— then hits the REAL `/desktop/files/status` route (through a FastAPI app +
TestClient, with the real registry-backed workspace resolver, NOTHING mocked)
and asserts the change markers the desktop file tree renders:

    committed-then-edited file  → "modified"
    brand-new untracked file    → "new"

This proves the whole chain the UI depends on: agent edits in place → the
project's own working tree changes → the status endpoint surfaces them. The
checkpoint shadow git (separate GIT_DIR under the temp data dir) is exercised
by the same turn, so this also confirms it doesn't pollute the user's git
status.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_desktop_file_status.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

_TMP = tempfile.mkdtemp(prefix="adk-cc-status-e2e-")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP

try:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from pydantic import Field
except Exception as e:  # pragma: no cover — ADK not installed → skip
    print(f"[SKIP] google-adk not importable: {e}")
    sys.exit(0)

from adk_cc.plugins.checkpoint import CheckpointPlugin
from adk_cc.service.desktop_routes import save_projects
from adk_cc.service.desktop_workspace import desktop_tenant_resolver
from adk_cc.service.tenancy import TenancyPlugin
from adk_cc.tools import BashTool, WriteFileTool

_ORIGINAL = "original readme\n"
_EDITED = "EDITED BY THE AGENT\n"
_NEW_FILE = "notes.md"
_NEW_BODY = "a fresh file the agent created\n"


class _ScriptedLlm(BaseLlm):
    model: str = "fake/scripted-status"
    responses: list[LlmResponse] = Field(default_factory=list)
    calls_made: int = 0

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake/scripted-status"]

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not self.responses:
            raise AssertionError(f"queue empty on call #{self.calls_made + 1}")
        self.calls_made += 1
        yield self.responses.pop(0)


def _tool_call(cid: str, name: str, args: dict) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(id=cid, name=name, args=args))],
        ),
        partial=False,
    )


def _text(t: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]), partial=False)


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


async def _run_turn(project_id: str, session_id: str) -> None:
    llm = _ScriptedLlm(
        responses=[
            _tool_call("c1", "write_file", {"path": "README.md", "content": _EDITED}),
            _tool_call("c2", "write_file", {"path": _NEW_FILE, "content": _NEW_BODY}),
            _text("done"),
        ]
    )
    agent = LlmAgent(
        name="status_e2e_agent",
        model=llm,
        instruction="Test agent.",
        tools=[BashTool(), WriteFileTool()],
    )
    plugins = [TenancyPlugin(tenant_resolver=desktop_tenant_resolver), CheckpointPlugin()]
    runner = InMemoryRunner(agent=agent, plugins=plugins, app_name="e2e-status")
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=project_id, session_id=session_id
    )
    async for _ in runner.run_async(
        user_id=project_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part(text="edit the readme and add notes")]
        ),
    ):
        pass


def _status_via_route(project_id: str, session_id: str) -> dict:
    """Call the REAL `/desktop/files/status` route through a TestClient. Uses the
    real registry-backed workspace resolver — no mocks."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.service import desktop_files

    app = FastAPI()
    desktop_files.mount_desktop_files_routes(app)
    client = TestClient(app)
    r = client.get(
        "/desktop/files/status",
        params={"project_id": project_id, "session_id": session_id},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def main() -> int:
    repo = os.path.join(_TMP, "proj")
    os.makedirs(repo)
    Path(repo, "README.md").write_text(_ORIGINAL)
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], repo)

    project_id, session_id = "projStatus", "sessStatus"
    save_projects([{"id": project_id, "name": "proj", "repo_path": repo}])

    asyncio.run(_run_turn(project_id, session_id))

    failures: list[str] = []

    # The turn must have actually mutated the working tree in place.
    if Path(repo, "README.md").read_text() != _EDITED:
        failures.append("agent did not edit README in place")
    if Path(repo, _NEW_FILE).read_text() != _NEW_BODY:
        failures.append("agent did not create the new file")

    body = _status_via_route(project_id, session_id)
    st = body.get("statuses", {})
    if body.get("is_repo") is not True:
        failures.append(f"expected is_repo=True, got {body.get('is_repo')!r}")
    if st.get("README.md") != "modified":
        failures.append(f"README.md marker: expected 'modified', got {st.get('README.md')!r}")
    if st.get(_NEW_FILE) != "new":
        failures.append(f"{_NEW_FILE} marker: expected 'new', got {st.get(_NEW_FILE)!r}")

    shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print("FAIL — desktop file-status e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("  [PASS] agent edited README.md in place and created a new file")
    print("  [PASS] /desktop/files/status marks README.md 'modified'")
    print("  [PASS] /desktop/files/status marks the new file 'new'")
    print("\ndesktop file-status e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
