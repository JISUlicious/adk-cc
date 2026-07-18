"""E2E: remote (SSH) checkpoint/undo — a real turn snapshotted and reverted.

The PR 6 chain against a real sshd container with git installed: a REAL agent
turn (TenancyPlugin + desktop_backend_factory + CheckpointPlugin, scripted
LLM + real WriteFileTool) edits a committed file on the REMOTE in place; the
CheckpointPlugin's remote branch snapshots the pre-turn state into a shadow
git ON THE REMOTE (`~/.adk-cc/checkpoints/...`); the real restore route path
(`restore_remote`) reverts the edit. Asserts:

  - the agent edited the remote file in place
  - exactly one checkpoint was logged for the turn (log is LOCAL)
  - checkpoint/list reports supported:true (+ checkpoints)
  - restore reverts the remote file to the pre-turn content
  - the user's REAL remote .git (HEAD) is untouched
  - a remote WITHOUT git degrades honestly: snapshot no-ops, list says
    supported:false with a reason (checked before installing git)

Benign commands only. Skips gracefully without Docker; the git-dependent
half SKIPs if the container can't install git (offline).

Run: `uv run python tests/e2e_remote_checkpoint.py`
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from typing import Any, AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="adk-rckpt-e2e-")
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"  # remote comes from the PROJECT

sys.path.insert(0, os.path.dirname(__file__))
from sshd_harness import SshdContainer, wait_ready  # noqa: E402

_WS = "/config/ckproj"
_ORIGINAL = "original remote readme\n"
_EDITED = "EDITED REMOTELY BY THE AGENT\n"


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

        os.environ["ADK_CC_SSH_CONTROL_DIR"] = box.control_dir
        os.environ["ADK_CC_SSH_IDENTITY_FILE"] = box.identity_file
        os.environ["ADK_CC_SSH_EXTRA_OPTS"] = " ".join(box.extra_ssh_opts)

        from adk_cc.plugins.checkpoint import CheckpointPlugin
        from adk_cc.sandbox.ssh_transport import SshTransport, get_transport
        from adk_cc.service import desktop_checkpoint as dc
        from adk_cc.service.desktop_routes import save_projects
        from adk_cc.service.desktop_workspace import (
            desktop_backend_factory,
            desktop_tenant_resolver,
        )
        from adk_cc.service.tenancy import TenancyPlugin
        from adk_cc.tools import BashTool, WriteFileTool

        t = SshTransport(
            box.host,
            port=box.port,
            identity_file=box.identity_file,
            extra_ssh_opts=box.extra_ssh_opts,
            control_dir=box.control_dir + "-seed",
        )

        project_id, session_id = "projCk", "sessCk"
        save_projects(
            [
                {
                    "id": project_id,
                    "name": "ckproj",
                    "remote": {"host": box.host, "path": _WS, "port": box.port},
                }
            ]
        )

        def _list_route() -> dict:
            from fastapi import FastAPI
            from starlette.testclient import TestClient

            from adk_cc.service.desktop_routes import mount_desktop_routes

            app = FastAPI()
            mount_desktop_routes(app)
            return (
                TestClient(app)
                .get(
                    "/desktop/checkpoint/list",
                    params={"project_id": project_id, "session_id": session_id},
                )
                .json()
            )

        class ScriptedLlm(BaseLlm):
            model: str = "fake/scripted-rckpt"
            responses: list[LlmResponse] = Field(default_factory=list)

            @classmethod
            def supported_models(cls) -> list[str]:
                return ["fake/scripted-rckpt"]

            async def generate_content_async(
                self, llm_request: Any, stream: bool = False
            ) -> AsyncGenerator[LlmResponse, None]:
                yield self.responses.pop(0)

        async def run_turn() -> None:
            llm = ScriptedLlm(
                responses=[
                    LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        id="c1",
                                        name="write_file",
                                        args={"path": "README.md", "content": _EDITED},
                                    )
                                )
                            ],
                        ),
                        partial=False,
                    ),
                    LlmResponse(
                        content=types.Content(
                            role="model", parts=[types.Part(text="done")]
                        ),
                        partial=False,
                    ),
                ]
            )
            agent = LlmAgent(
                name="rckpt_agent",
                model=llm,
                instruction="t",
                tools=[BashTool(), WriteFileTool()],
            )
            runner = InMemoryRunner(
                agent=agent,
                plugins=[
                    TenancyPlugin(
                        tenant_resolver=desktop_tenant_resolver,
                        backend_factory=desktop_backend_factory,
                    ),
                    CheckpointPlugin(),
                ],
                app_name="e2e-rckpt",
            )
            await runner.session_service.create_session(
                app_name=runner.app_name, user_id=project_id, session_id=session_id
            )
            async for _ in runner.run_async(
                user_id=project_id,
                session_id=session_id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="edit the readme")]
                ),
            ):
                pass

        async def drive() -> None:
            err = await wait_ready(t)
            if err:
                failures.append(f"sshd never became ready: {err}")
                return

            # Seed the remote "project" BEFORE git exists.
            await t.run(f"mkdir -p {_WS}")
            await t.write_file(f"{_WS}/README.md", _ORIGINAL.encode())

            # --- no-git degradation --------------------------------------
            body = _list_route()
            if not (body.get("supported") is False and body.get("reason")):
                failures.append(f"no-git list: {body}")
            else:
                print("  [PASS] no git on remote → supported:false + reason")

            # --- install git; real repo; full undo cycle -----------------
            apk = subprocess.run(
                ["docker", "exec", box.container_id, "apk", "add", "--no-cache", "git"],
                capture_output=True, text=True, timeout=120,
            )
            if apk.returncode != 0:
                print("  [SKIP] could not install git in container (offline?) — undo cycle not exercised")
                return
            # Refresh the SHARED (factory/panel/checkpoint) transport's probe —
            # it cached git=False from the supported check above.
            await get_transport(box.host, port=box.port).probe(refresh=True)

            await t.run(
                "git init -q && git add -A && "
                "git -c user.email=t@t -c user.name=t commit -qm init",
                cwd=_WS,
            )
            head_before = (await t.run("git rev-parse HEAD", cwd=_WS)).stdout.strip()

            await run_turn()

            got = (await t.read_file(f"{_WS}/README.md")).decode()
            if got != _EDITED:
                failures.append(f"agent edit missing on remote: {got!r}")
            else:
                print("  [PASS] agent edited the remote README in place")

            cps = dc.list_checkpoints(project_id, session_id)
            if len(cps) != 1:
                failures.append(f"expected 1 checkpoint, got {len(cps)}: {cps}")
            else:
                print("  [PASS] exactly one checkpoint logged for the turn")

            body = _list_route()
            if not (body.get("supported") is True and len(body.get("checkpoints", [])) == 1):
                failures.append(f"list route with git: {body}")
            else:
                print("  [PASS] checkpoint/list: supported:true + the checkpoint")

            res = await dc.restore_remote(
                project_id, session_id, get_transport(box.host, port=box.port), _WS
            )
            if res.get("status") != "ok":
                failures.append(f"remote restore failed: {res}")
            got = (await t.read_file(f"{_WS}/README.md")).decode()
            if got != _ORIGINAL:
                failures.append(f"restore did not revert: {got!r}")
            else:
                print("  [PASS] undo reverted the remote edit to the pre-turn state")

            head_after = (await t.run("git rev-parse HEAD", cwd=_WS)).stdout.strip()
            if head_after != head_before:
                failures.append("user's remote .git HEAD moved")
            else:
                print("  [PASS] the user's REAL remote .git is untouched")

        asyncio.run(drive())

    if failures:
        print("\nFAIL — remote checkpoint e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("\nremote checkpoint e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
