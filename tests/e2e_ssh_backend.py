"""E2E: a REAL agent turn executes against a REAL remote workspace over SSH.

The full chain, nothing mocked below the LLM: ADK's real runtime
(InMemoryRunner + real WriteFileTool/BashTool) with a scripted LLM, the
env-driven `ADK_CC_SANDBOX_BACKEND=ssh` factory path, `default_workspace()`
returning the remote-flagged WorkspaceRoot, and a real sshd container as
the "remote device". The turn writes a file with write_file AND creates one
via run_bash; a second turn runs with ADK_CC_BASH_STREAM=1 to drive the
true `exec_stream` path. Every artifact is then verified by reading it back
over a separate transport connection.

Proves PR 2's claim end-to-end: tools don't know they're remote — the
backend seam carries everything.

Benign commands only. Skips gracefully without Docker.

Run: `uv run python tests/e2e_ssh_backend.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

sys.path.insert(0, os.path.dirname(__file__))
from sshd_harness import SshdContainer, wait_ready  # noqa: E402

_WS = "/config/proj"
_README = "hello from the agent, over ssh\n"


def _scripted_llm_classes():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types
    from pydantic import Field

    class ScriptedLlm(BaseLlm):
        model: str = "fake/scripted-ssh"
        responses: list[LlmResponse] = Field(default_factory=list)

        @classmethod
        def supported_models(cls) -> list[str]:
            return ["fake/scripted-ssh"]

        async def generate_content_async(
            self, llm_request: Any, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            if not self.responses:
                raise AssertionError("scripted LLM queue empty")
            yield self.responses.pop(0)

    def tool_call(cid: str, name: str, args: dict) -> LlmResponse:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(id=cid, name=name, args=args)
                    )
                ],
            ),
            partial=False,
        )

    def text(t: str) -> LlmResponse:
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=t)]),
            partial=False,
        )

    return ScriptedLlm, tool_call, text


async def _run_turn(llm, message: str) -> None:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from adk_cc.tools import BashTool, WriteFileTool

    agent = LlmAgent(
        name="ssh_e2e_agent",
        model=llm,
        instruction="Test agent.",
        tools=[BashTool(), WriteFileTool()],
    )
    runner = InMemoryRunner(agent=agent, app_name="e2e-ssh")
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id="u1", session_id="s1"
    )
    async for _ in runner.run_async(
        user_id="u1",
        session_id="s1",
        new_message=types.Content(role="user", parts=[types.Part(text=message)]),
    ):
        pass


def main() -> int:
    try:
        import google.adk  # noqa: F401
    except Exception as e:  # pragma: no cover
        print(f"[SKIP] google-adk not importable: {e}")
        return 0

    failures: list[str] = []
    with SshdContainer() as box:
        if box is None:
            return 0

        # Env-driven ssh mode — set BEFORE any backend/workspace resolution.
        os.environ["ADK_CC_SANDBOX_BACKEND"] = "ssh"
        os.environ["ADK_CC_SSH_HOST"] = box.host
        os.environ["ADK_CC_SSH_PORT"] = str(box.port)
        os.environ["ADK_CC_SSH_IDENTITY_FILE"] = box.identity_file
        os.environ["ADK_CC_SSH_EXTRA_OPTS"] = " ".join(box.extra_ssh_opts)
        os.environ["ADK_CC_SSH_CONTROL_DIR"] = box.control_dir
        os.environ["ADK_CC_SSH_WORKSPACE_PATH"] = _WS

        from adk_cc.sandbox.ssh_transport import SshTransport

        # Independent verification channel (own control dir → own master).
        verify = SshTransport(
            box.host,
            port=box.port,
            identity_file=box.identity_file,
            extra_ssh_opts=box.extra_ssh_opts,
            control_dir=box.control_dir + "-verify",
        )

        async def drive() -> None:
            err = await wait_ready(verify)
            if err:
                failures.append(f"sshd never became ready: {err}")
                return

            # Resolve the factory-built backend + remote workspace exactly the
            # way the tool layer does, and bring the workspace up (the role the
            # tenancy plugin plays in production).
            from adk_cc.sandbox import get_backend
            from adk_cc.sandbox.backends.ssh_backend import SshBackend
            from adk_cc.sandbox.workspace import default_workspace

            class _Ctx:
                state: dict = {}

            backend = get_backend(_Ctx())
            if not isinstance(backend, SshBackend):
                failures.append(f"factory produced {type(backend).__name__}, not SshBackend")
                return
            ws = default_workspace()
            if not (ws.remote and ws.abs_path == _WS):
                failures.append(f"workspace not remote-flagged: {ws}")
                return
            await backend.ensure_workspace(ws)
            print("  [PASS] env-driven SshBackend + remote workspace bring-up")

            ScriptedLlm, tool_call, text = _scripted_llm_classes()

            # Turn 1 (buffered exec): write_file + run_bash both mutate the REMOTE tree.
            await _run_turn(
                ScriptedLlm(
                    responses=[
                        tool_call("c1", "write_file", {"path": "README.md", "content": _README}),
                        tool_call("c2", "run_bash", {"command": "printf 'from-bash' > bash.txt"}),
                        text("done"),
                    ]
                ),
                "write the readme and a bash file",
            )

            got = (await verify.read_file(f"{_WS}/README.md")).decode()
            if got != _README:
                failures.append(f"write_file content on remote: {got!r}")
            else:
                print("  [PASS] write_file landed on the remote device")

            got = (await verify.read_file(f"{_WS}/bash.txt")).decode()
            if got != "from-bash":
                failures.append(f"run_bash artifact on remote: {got!r}")
            else:
                print("  [PASS] run_bash executed on the remote device")

            # Turn 2: the streaming path (ADK_CC_BASH_STREAM=1 → exec_stream).
            os.environ["ADK_CC_BASH_STREAM"] = "1"
            try:
                await _run_turn(
                    ScriptedLlm(
                        responses=[
                            tool_call(
                                "c3",
                                "run_bash",
                                {"command": "echo s1 > stream.txt; echo s2 >> stream.txt"},
                            ),
                            text("done"),
                        ]
                    ),
                    "stream something",
                )
            finally:
                os.environ.pop("ADK_CC_BASH_STREAM", None)

            got = (await verify.read_file(f"{_WS}/stream.txt")).decode()
            if got != "s1\ns2\n":
                failures.append(f"exec_stream artifact on remote: {got!r}")
            else:
                print("  [PASS] exec_stream (live) path executed on the remote device")

        try:
            asyncio.run(drive())
        finally:
            for k in (
                "ADK_CC_SANDBOX_BACKEND",
                "ADK_CC_SSH_HOST",
                "ADK_CC_SSH_PORT",
                "ADK_CC_SSH_IDENTITY_FILE",
                "ADK_CC_SSH_EXTRA_OPTS",
                "ADK_CC_SSH_CONTROL_DIR",
                "ADK_CC_SSH_WORKSPACE_PATH",
            ):
                os.environ.pop(k, None)

    if failures:
        print("\nFAIL — ssh backend e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("\nssh backend e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
