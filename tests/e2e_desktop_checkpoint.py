"""E2E: desktop checkpoint/undo — a real ADK turn is snapshotted and reverted.

Drives ADK's REAL runtime (InMemoryRunner + desktop TenancyPlugin +
CheckpointPlugin + the real WriteFileTool + NoopBackend) with a scripted LLM that
edits a tracked file in place. No live model call. Then invokes the real restore
(``desktop_checkpoint.restore``, the POST /desktop/checkpoint/restore body path)
and asserts the edit is undone — while the user's real .git is untouched.

Proves Phase 3 end-to-end: the CheckpointPlugin actually fires in the real
before_tool chain (after Tenancy seeds the workspace), snapshots the pre-turn
state once, and "Undo last turn" reverts the agent's in-place edit.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_desktop_checkpoint.py
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

_TMP = tempfile.mkdtemp(prefix="adk-cc-ckpt-e2e-")
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
from adk_cc.service import desktop_checkpoint as dc
from adk_cc.service.desktop_routes import save_projects
from adk_cc.service.desktop_workspace import desktop_tenant_resolver
from adk_cc.service.tenancy import TenancyPlugin
from adk_cc.tools import BashTool, WriteFileTool

_ORIGINAL = "original readme\n"
_EDITED = "EDITED BY THE AGENT\n"


class _ScriptedLlm(BaseLlm):
    model: str = "fake/scripted-ckpt"
    responses: list[LlmResponse] = Field(default_factory=list)
    calls_made: int = 0

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake/scripted-ckpt"]

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
            _text("done"),
        ]
    )
    agent = LlmAgent(
        name="ckpt_e2e_agent",
        model=llm,
        instruction="Test agent.",
        tools=[BashTool(), WriteFileTool()],
    )
    # Production order: Tenancy seeds the workspace, THEN CheckpointPlugin snapshots.
    plugins = [TenancyPlugin(tenant_resolver=desktop_tenant_resolver), CheckpointPlugin()]
    runner = InMemoryRunner(agent=agent, plugins=plugins, app_name="e2e-ckpt")
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=project_id, session_id=session_id
    )
    async for _ in runner.run_async(
        user_id=project_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text="edit the readme")]),
    ):
        pass


def main() -> int:
    repo = os.path.join(_TMP, "proj")
    os.makedirs(repo)
    Path(repo, "README.md").write_text(_ORIGINAL)
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], repo)

    project_id, session_id = "projCkpt", "sessCkpt"
    save_projects([{"id": project_id, "name": "proj", "repo_path": repo}])

    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    reflog_before = _git(["reflog", "--format=%H"], repo).stdout

    asyncio.run(_run_turn(project_id, session_id))

    failures: list[str] = []

    # 1) the agent edited the file IN PLACE
    if Path(repo, "README.md").read_text() != _EDITED:
        failures.append("agent did not edit README in place")

    # 2) exactly one checkpoint was taken for the turn (the pre-edit state)
    cps = dc.list_checkpoints(project_id, session_id)
    if len(cps) != 1:
        failures.append(f"expected 1 checkpoint, got {len(cps)}")

    # 3) Undo last turn → the edit reverts to the original committed content
    res = dc.restore(project_id, session_id, repo)
    if res.get("status") != "ok":
        failures.append(f"restore failed: {res}")
    elif Path(repo, "README.md").read_text() != _ORIGINAL:
        failures.append("restore did not revert the agent's edit")

    # 4) the user's REAL git is untouched
    if _git(["rev-parse", "HEAD"], repo).stdout.strip() != head_before:
        failures.append("real HEAD moved")
    if _git(["reflog", "--format=%H"], repo).stdout != reflog_before:
        failures.append("real reflog changed")

    shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print("FAIL — desktop checkpoint e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("  [PASS] agent edited README in place")
    print("  [PASS] exactly one checkpoint snapshotted the pre-turn state")
    print("  [PASS] undo-last-turn reverted the agent's edit")
    print("  [PASS] the user's real .git (HEAD/reflog) is untouched")
    print("\ndesktop checkpoint e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
