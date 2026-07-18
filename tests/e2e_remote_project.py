"""E2E: a REMOTE (SSH) desktop project, end-to-end through the DESKTOP path.

The full PR 4 chain against a real sshd container, nothing mocked below the
LLM: the project registry holds a remote binding → `desktop_tenant_resolver`
returns a remote ctx → TenancyPlugin + `desktop_backend_factory` seed a
remote-flagged WorkspaceRoot + SshBackend for the session → a REAL agent
turn (scripted LLM + real WriteFileTool/BashTool) edits files on the
"remote device" → artifacts verified over an independent transport →
`/desktop/sessions/backend` reports source=live backend=ssh with the host.

Also proves the permission plugin path stays healthy with a remote session
in the loop (the PermissionPlugin isn't in this runner — its floor is
covered purely in test_remote_projects.py — but tenancy + checkpoint-free
seeding is the production desktop order).

Benign commands only. Skips gracefully without Docker.

Run: `uv run python tests/e2e_remote_project.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any, AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="adk-remote-e2e-")
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"  # remote must come from the PROJECT, not env

sys.path.insert(0, os.path.dirname(__file__))
from sshd_harness import SshdContainer, wait_ready  # noqa: E402

_WS = "/config/proj"
_README = "remote project readme, via the desktop path\n"


def main() -> int:
    try:
        from google.adk.agents.llm_agent import LlmAgent
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import InMemoryRunner
        from google.genai import types
        from pydantic import Field
    except Exception as e:  # pragma: no cover
        print(f"[SKIP] google-adk not importable: {e}")
        return 0

    failures: list[str] = []
    with SshdContainer() as box:
        if box is None:
            return 0

        # The transport layer reads these for EVERY get_transport call — the
        # registry entry only carries host/path, auth comes from ssh config /
        # these test overrides.
        os.environ["ADK_CC_SSH_CONTROL_DIR"] = box.control_dir

        from adk_cc.sandbox.ssh_transport import SshTransport, get_transport
        from adk_cc.service.desktop_routes import save_projects
        from adk_cc.service.desktop_workspace import (
            desktop_backend_factory,
            desktop_tenant_resolver,
        )
        from adk_cc.service.tenancy import TenancyPlugin
        from adk_cc.tools import BashTool, WriteFileTool

        # Pre-register the transport for this host WITH the test's key/known-
        # hosts opts, so the factory's get_transport() call (host, no port
        # kwargs beyond registry) resolves THIS instance. The registry key
        # includes port+identity+opts — so seed it with exactly what the
        # factory will ask for: (host, port, None, ()).  We instead register
        # the project WITH the port and pass identity/known-hosts via env-
        # style extra opts on a pre-created transport under the same key.
        factory_key_transport = get_transport(box.host, port=box.port)
        # Point the factory-keyed transport's ssh invocation at the test key +
        # throwaway known_hosts (fields are per-instance; same object the
        # factory will fetch).
        factory_key_transport._identity = box.identity_file
        factory_key_transport._extra = box.extra_ssh_opts

        verify = SshTransport(
            box.host,
            port=box.port,
            identity_file=box.identity_file,
            extra_ssh_opts=box.extra_ssh_opts,
            control_dir=box.control_dir + "-verify",
        )

        project_id, session_id = "projRemote", "sessRemote"
        save_projects(
            [
                {
                    "id": project_id,
                    "name": "remote-proj",
                    "remote": {"host": box.host, "path": _WS, "port": box.port},
                }
            ]
        )

        class ScriptedLlm(BaseLlm):
            model: str = "fake/scripted-remote"
            responses: list[LlmResponse] = Field(default_factory=list)

            @classmethod
            def supported_models(cls) -> list[str]:
                return ["fake/scripted-remote"]

            async def generate_content_async(
                self, llm_request: Any, stream: bool = False
            ) -> AsyncGenerator[LlmResponse, None]:
                yield self.responses.pop(0)

        def tool_call(cid: str, name: str, args: dict) -> LlmResponse:
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id=cid, name=name, args=args
                            )
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

        async def drive() -> None:
            err = await wait_ready(verify)
            if err:
                failures.append(f"sshd never became ready: {err}")
                return

            llm = ScriptedLlm(
                responses=[
                    tool_call("c1", "write_file", {"path": "README.md", "content": _README}),
                    tool_call("c2", "run_bash", {"command": "printf 'bash-on-remote' > touch.txt"}),
                    text("done"),
                ]
            )
            agent = LlmAgent(
                name="remote_proj_agent",
                model=llm,
                instruction="Test agent.",
                tools=[BashTool(), WriteFileTool()],
            )
            runner = InMemoryRunner(
                agent=agent,
                plugins=[
                    TenancyPlugin(
                        tenant_resolver=desktop_tenant_resolver,
                        backend_factory=desktop_backend_factory,
                    )
                ],
                app_name="e2e-remote-proj",
            )
            await runner.session_service.create_session(
                app_name=runner.app_name, user_id=project_id, session_id=session_id
            )
            async for _ in runner.run_async(
                user_id=project_id,
                session_id=session_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="work on the remote project")]
                ),
            ):
                pass

            got = (await verify.read_file(f"{_WS}/README.md")).decode()
            if got != _README:
                failures.append(f"write_file via desktop path: {got!r}")
            else:
                print("  [PASS] registry→resolver→factory→turn: write_file on remote")

            got = (await verify.read_file(f"{_WS}/touch.txt")).decode()
            if got != "bash-on-remote":
                failures.append(f"run_bash via desktop path: {got!r}")
            else:
                print("  [PASS] run_bash executed on the remote project")

            # The badge's truth endpoint reports the live ssh backend + host.
            from fastapi import FastAPI
            from starlette.testclient import TestClient

            from adk_cc.service.desktop_routes import mount_desktop_routes

            app = FastAPI()
            mount_desktop_routes(app)
            body = TestClient(app).get(
                "/desktop/sessions/backend",
                params={"session_id": session_id, "project_id": project_id},
            ).json()
            if not (
                body.get("source") == "live"
                and body.get("backend") == "ssh"
                and body.get("detail") == box.host
                and body.get("isolated") is False
            ):
                failures.append(f"session-backend live report: {body}")
            else:
                print("  [PASS] /desktop/sessions/backend: live ssh + host + not-isolated")

        asyncio.run(drive())

    if failures:
        print("\nFAIL — remote project e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("\nremote project e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
