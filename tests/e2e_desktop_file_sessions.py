"""E2E: real ADK turn round-trips through the FileSessionService JSONL store.

Runs an actual turn on ADK's Runner wired with FileSessionService (+ the desktop
TenancyPlugin + real BashTool + NoopBackend, scripted LLM — no live model). The
turn produces genuine ADK Events: a user message, a model function_call
(run_bash), a tool function_response, and a model text reply. We then reload the
session from a FRESH FileSessionService instance (simulating a restart) and assert
every event came back intact from the per-project JSONL file.

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_desktop_file_sessions.py
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

_TMP = tempfile.mkdtemp(prefix="adk-cc-fss-e2e-")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP

try:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.genai import types
    from pydantic import Field
except Exception as e:  # pragma: no cover
    print(f"[SKIP] google-adk not importable: {e}")
    sys.exit(0)

from adk_cc.service.desktop_workspace import desktop_tenant_resolver
from adk_cc.service.file_session_service import FileSessionService
from adk_cc.service.tenancy import TenancyPlugin
from adk_cc.tools import BashTool

APP = "e2e-fss"


class _ScriptedLlm(BaseLlm):
    model: str = "fake/scripted-fss"
    responses: list[LlmResponse] = Field(default_factory=list)
    calls_made: int = 0

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake/scripted-fss"]

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


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _parts(ev):
    content = getattr(ev, "content", None)
    return getattr(content, "parts", None) or []


async def _run() -> tuple[FileSessionService, str, str, str]:
    repo = os.path.join(_TMP, "proj")
    os.makedirs(repo)
    Path(repo, "README.md").write_text("hi\n")
    _git(["init", "-q"], repo)
    _git(["add", "-A"], repo)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], repo)

    from adk_cc.service.desktop_routes import save_projects

    project_id, session_id = "projFSS", "sessFSS"
    save_projects([{"id": project_id, "name": "proj", "repo_path": repo}])

    base = os.path.join(_TMP, "store")
    fss = FileSessionService(base)

    llm = _ScriptedLlm(
        responses=[
            _tool_call("c1", "run_bash", {"command": "echo hello-from-turn"}),
            _text("all done"),
        ]
    )
    agent = LlmAgent(name="fss_e2e_agent", model=llm, instruction="Test.", tools=[BashTool()])
    runner = Runner(
        app_name=APP,
        agent=agent,
        session_service=fss,
        artifact_service=InMemoryArtifactService(),
        plugins=[TenancyPlugin(tenant_resolver=desktop_tenant_resolver)],
    )
    await fss.create_session(app_name=APP, user_id=project_id, session_id=session_id)
    async for _ in runner.run_async(
        user_id=project_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text="please run it")]),
    ):
        pass
    return fss, base, project_id, session_id


def main() -> int:
    fss, base, project_id, session_id = asyncio.run(_run())
    failures: list[str] = []

    # The file exists at the per-project path.
    f = Path(base) / "projects" / project_id / "sessions" / f"{session_id}.jsonl"
    if not f.is_file():
        failures.append(f"session file missing at {f}")

    # Reload from a FRESH instance (simulate a restart) — the whole point.
    fresh = FileSessionService(base)
    got = asyncio.run(
        fresh.get_session(app_name=APP, user_id=project_id, session_id=session_id)
    )
    if got is None:
        print("FAIL — session did not reload from disk")
        return 1

    evs = got.events
    has_user_msg = any(
        any(getattr(p, "text", None) == "please run it" for p in _parts(e)) for e in evs
    )
    has_bash_call = any(
        any(getattr(p, "function_call", None) and p.function_call.name == "run_bash" for p in _parts(e))
        for e in evs
    )
    has_bash_resp = any(
        any(getattr(p, "function_response", None) and p.function_response.name == "run_bash" for p in _parts(e))
        for e in evs
    )
    has_final_text = any(
        any(getattr(p, "text", None) and "all done" in p.text for p in _parts(e)) for e in evs
    )

    if not has_user_msg:
        failures.append("user message did not round-trip")
    if not has_bash_call:
        failures.append("run_bash function_call did not round-trip")
    if not has_bash_resp:
        failures.append("run_bash function_response did not round-trip")
    if not has_final_text:
        failures.append("final model text did not round-trip")
    if got.last_update_time <= 0:
        failures.append("last_update_time not set from events")

    shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print("FAIL — file-session e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print(f"  [PASS] real turn wrote {len(evs)} events to per-project JSONL")
    print("  [PASS] user message round-tripped")
    print("  [PASS] run_bash function_call round-tripped")
    print("  [PASS] run_bash function_response round-tripped")
    print("  [PASS] final model text round-tripped")
    print("  [PASS] reload from a FRESH service instance saw the full transcript")
    print("\nfile-session e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
