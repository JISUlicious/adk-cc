"""E2E: desktop in-place workspace — a real ADK turn runs in the project root.

Drives ADK's REAL runtime — ``InMemoryRunner`` + ``LlmAgent`` + the desktop
``TenancyPlugin`` + the REAL ``BashTool`` / ``WriteFileTool`` + ``NoopBackend`` —
with a *scripted* LLM that emits the tool calls. The live (rate-limited) model
endpoint is never touched, so this is deterministic and cheap, yet exercises the
same wiring a real turn does: the runner seeds the tenant workspace, the tool
layer resolves it, and the backend execs there.

What it proves for Phase 2 (in-place desktop):
  - the agent's ``run_bash`` executes with ``cwd == the project's repo root``
    (``pwd -P`` output), and a RELATIVE shell write lands in the user's REAL
    project dir — not a per-session git worktree;
  - the ``write_file`` tool resolves relative to that same root;
  - NO worktree is created for the session;
  - the file panel (``_resolve_within``) roots at the identical dir, so it lists
    exactly what the agent just wrote (panel == agent cwd, by construction).

Run: PYTHONPATH=agents .venv/bin/python tests/e2e_desktop_inplace.py
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

# Don't pull the real model/key from .env — the scripted LLM stands in for it,
# and we must not risk a live call against the rate-limited endpoint.
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

# Point the desktop data dir at a throwaway dir + turn desktop mode ON, BEFORE
# importing any desktop module (they read these on import / first call).
_TMP = tempfile.mkdtemp(prefix="adk-cc-inplace-e2e-")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = _TMP

try:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.events.event import Event
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types
    from pydantic import Field
except Exception as e:  # pragma: no cover — ADK not installed → skip, don't fail
    print(f"[SKIP] google-adk not importable: {e}")
    sys.exit(0)

from adk_cc.service.desktop_files import _resolve_within
from adk_cc.service.desktop_routes import save_projects
from adk_cc.service.desktop_workspace import desktop_tenant_resolver
from adk_cc.service.tenancy import TenancyPlugin
from adk_cc.tools import BashTool, WriteFileTool


# --- Scripted LLM (same shape as e2e_confirmation_flow) -------------


class _ScriptedLlm(BaseLlm):
    """Yields the next queued LlmResponse per turn — no network."""

    model: str = "fake/scripted-inplace"
    responses: list[LlmResponse] = Field(default_factory=list)
    calls_made: int = 0

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake/scripted-inplace"]

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not self.responses:
            raise AssertionError(
                f"_ScriptedLlm queue empty on call #{self.calls_made + 1}"
            )
        self.calls_made += 1
        yield self.responses.pop(0)


def _tool_call(call_id: str, name: str, args: dict) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))],
        ),
        partial=False,
    )


def _text(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        partial=False,
    )


def _git(args: list[str], cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _bash_stdout(events: list[Event]) -> str | None:
    """Pull run_bash's stdout out of its function_response event, if present."""
    for ev in events:
        content = getattr(ev, "content", None)
        for part in getattr(content, "parts", None) or []:
            fr = getattr(part, "function_response", None)
            if fr is not None and fr.name == "run_bash":
                resp = fr.response or {}
                return resp.get("stdout")
    return None


async def _run_turn(project_id: str, session_id: str) -> list[Event]:
    llm = _ScriptedLlm(
        responses=[
            # 1) prove cwd + write a file via a RELATIVE shell redirect
            _tool_call("c1", "run_bash", {"command": "pwd -P; printf 'phase2-proof-bash' > proof_bash.txt"}),
            # 2) prove the write_file tool resolves relative to the same root
            _tool_call("c2", "write_file", {"path": "proof_wf.txt", "content": "phase2-proof-wf"}),
            # 3) end the turn
            _text("done"),
        ]
    )
    agent = LlmAgent(
        name="inplace_e2e_agent",
        model=llm,
        instruction="Test agent.",
        tools=[BashTool(), WriteFileTool()],
    )
    plugin = TenancyPlugin(tenant_resolver=desktop_tenant_resolver)
    runner = InMemoryRunner(agent=agent, plugins=[plugin], app_name="e2e-inplace")
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=project_id, session_id=session_id
    )
    events: list[Event] = []
    async for ev in runner.run_async(
        user_id=project_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text="go")]),
    ):
        events.append(ev)
    return events


def main() -> int:
    # A real git project OUTSIDE the desktop data dir (like a user's checkout).
    proj_root = os.path.join(_TMP, "myproject")
    os.makedirs(proj_root)
    with open(os.path.join(proj_root, "README.md"), "w") as f:
        f.write("hello from the real project\n")
    _git(["init", "-q"], proj_root)
    _git(["add", "-A"], proj_root)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], proj_root)

    project_id = "projInplace"
    session_id = "sessInplace"
    save_projects([{"id": project_id, "name": "myproject", "repo_path": proj_root}])

    events = asyncio.run(_run_turn(project_id, session_id))

    real = Path(proj_root).resolve()
    failures: list[str] = []

    # 1) run_bash executed in-place (pwd -P == the project root)
    stdout = (_bash_stdout(events) or "").strip().splitlines()
    pwd_line = stdout[0] if stdout else ""
    if not pwd_line or Path(pwd_line).resolve() != real:
        failures.append(f"run_bash cwd: pwd -P said {pwd_line!r}, want {real}")

    # 2) the RELATIVE shell write landed in the REAL project dir
    bash_file = real / "proof_bash.txt"
    if not bash_file.is_file() or bash_file.read_text() != "phase2-proof-bash":
        failures.append(f"run_bash relative write missing/wrong at {bash_file}")

    # 3) write_file resolved relative to the same root
    wf_file = real / "proof_wf.txt"
    if not wf_file.is_file() or wf_file.read_text() != "phase2-proof-wf":
        failures.append(f"write_file output missing/wrong at {wf_file}")

    # 4) NO per-session worktree was created (in-place, not isolated)
    wt = Path(_TMP) / "worktrees" / project_id
    if wt.exists():
        failures.append(f"a worktree was created at {wt} — should be in-place")

    # 5) the file panel roots at the SAME dir and lists what the agent wrote
    panel_root = _resolve_within(project_id, session_id, "")
    if panel_root != real:
        failures.append(f"file panel root {panel_root} != agent cwd {real}")
    else:
        names = {c.name for c in panel_root.iterdir()}
        for expected in ("proof_bash.txt", "proof_wf.txt"):
            if expected not in names:
                failures.append(f"file panel does not list {expected} (has {sorted(names)})")

    shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print("FAIL — desktop in-place e2e:")
        for msg in failures:
            print(f"  [FAIL] {msg}")
        return 1
    print("  [PASS] run_bash executed with cwd == project root (pwd -P)")
    print("  [PASS] relative shell write landed in the real project dir")
    print("  [PASS] write_file resolved to the project root")
    print("  [PASS] no per-session worktree created (in-place)")
    print("  [PASS] file panel roots at the agent's cwd and lists both files")
    print("\ndesktop in-place e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
