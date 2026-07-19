"""ACCEPTANCE: the SSH remote-workspace feature against a REAL second device.

The Docker-sshd e2es prove function; this proves the production path on real
hardware: auth from the user's own ssh config/agent (no identity/extra-opt
injection), a real network, a real filesystem. Gated on env — skips unless:

    ADK_CC_ACCEPT_SSH_HOST   e.g. "mybox" or "user@192.168.0.42"
    ADK_CC_ACCEPT_SSH_PATH   absolute scratch path the test MAY create and
                             write under (its write boundary)

Optional: ADK_CC_ACCEPT_SSH_PORT.

What it does (everything inside the scratch path; artifacts are LEFT in
place afterwards so the run is inspectable — delete the dir yourself when
done):

  1. probe + latency (cold master establishment, warm multiplexed ops)
  2. seed a git repo at the scratch root (baseline commit, -c identity —
     the device's git config is never touched)
  3. register a remote project (TEMPORARY desktop data dir — the real
     ~/.adk-cc-desktop is untouched) and run a REAL agent turn (scripted
     LLM + TenancyPlugin + desktop backend factory + CheckpointPlugin):
     write_file edits README.md, run_bash creates from-bash.txt
  4. verify artifacts over the transport; file-panel routes (tree/read/
     status markers); per-session backend truth route (live ssh + host)
  5. checkpoint: list (supported:true), UNDO → README reverts, the file
     created during the turn is removed, and the scratch repo's own HEAD
     never moved
  6. print a latency + pass/fail report

Benign commands only; no sudo; nothing outside the scratch path is written
(the remote shadow store lives at ~/.adk-cc/checkpoints/<proj>/<sess> —
inherent to the undo feature; also left in place).

Run:
  ADK_CC_ACCEPT_SSH_HOST=... ADK_CC_ACCEPT_SSH_PATH=/abs/scratch \
    uv run python tests/acceptance_ssh_device.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from typing import Any, AsyncGenerator

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-acceptance")

_HOST = os.environ.get("ADK_CC_ACCEPT_SSH_HOST") or ""
_PATH = (os.environ.get("ADK_CC_ACCEPT_SSH_PATH") or "").rstrip("/")
_PORT_RAW = os.environ.get("ADK_CC_ACCEPT_SSH_PORT") or ""

if not _HOST or not _PATH.startswith("/"):
    print("[SKIP] set ADK_CC_ACCEPT_SSH_HOST and ADK_CC_ACCEPT_SSH_PATH (absolute)")
    sys.exit(0)

os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="adk-accept-ssh-")
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"  # remote must come from the PROJECT

# Per-run token: the acceptance dir is deliberately LEFT IN PLACE between
# runs, so every content/filename this run creates must differ from the last
# run's leftovers — otherwise the seed commit sees a clean tree ("nothing to
# commit", exit 1 with the message on stdout) and the bash artifact is already
# tracked at baseline, so its "new" marker never appears. Re-run-safe by
# construction.
import uuid as _uuid

_RUN = _uuid.uuid4().hex[:8]
_ORIGINAL = f"acceptance: original readme ({_RUN})\n"
_EDITED = f"acceptance: EDITED BY THE AGENT over real SSH ({_RUN})\n"
_BASH_FILE = f"from-bash-{_RUN}.txt"
_GIT_ID = "-c user.email=accept@adk-cc.local -c 'user.name=adk-cc acceptance'"


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

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.plugins.checkpoint import CheckpointPlugin
    from adk_cc.sandbox.ssh_transport import get_transport
    from adk_cc.service import desktop_checkpoint as dc
    from adk_cc.service.desktop_files import mount_desktop_files_routes
    from adk_cc.service.desktop_routes import mount_desktop_routes, save_projects
    from adk_cc.service.desktop_workspace import (
        desktop_backend_factory,
        desktop_tenant_resolver,
    )
    from adk_cc.service.tenancy import TenancyPlugin
    from adk_cc.tools import BashTool, WriteFileTool

    port = int(_PORT_RAW) if _PORT_RAW else None
    t = get_transport(_HOST, port=port)  # PRODUCTION path: user's ssh config/agent

    project_id, session_id = "projAccept", f"accept-{os.getpid()}"
    save_projects(
        [
            {
                "id": project_id,
                "name": "ssh-acceptance",
                "remote": {"host": _HOST, "path": _PATH, **({"port": port} if port else {})},
            }
        ]
    )

    app = FastAPI()
    mount_desktop_routes(app)
    mount_desktop_files_routes(app)
    web = TestClient(app)

    failures: list[str] = []
    report: list[str] = []

    class ScriptedLlm(BaseLlm):
        model: str = "fake/scripted-accept"
        responses: list[LlmResponse] = Field(default_factory=list)

        @classmethod
        def supported_models(cls) -> list[str]:
            return ["fake/scripted-accept"]

        async def generate_content_async(
            self, llm_request: Any, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            yield self.responses.pop(0)

    def tool_call(cid: str, name: str, args: dict) -> LlmResponse:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(id=cid, name=name, args=args))],
            ),
            partial=False,
        )

    def text(s: str) -> LlmResponse:
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=s)]), partial=False
        )

    async def run_turn() -> None:
        llm = ScriptedLlm(
            responses=[
                tool_call("c1", "write_file", {"path": "README.md", "content": _EDITED}),
                tool_call("c2", "run_bash", {"command": f"printf 'created-by-bash' > {_BASH_FILE}"}),
                text("done"),
            ]
        )
        agent = LlmAgent(
            name="accept_agent", model=llm, instruction="t",
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
            app_name="acceptance-ssh",
        )
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=project_id, session_id=session_id
        )
        async for _ in runner.run_async(
            user_id=project_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text="edit the project")]),
        ):
            pass

    def _panel(route: str, **params):
        return web.get(
            f"/desktop/files/{route}",
            params={"project_id": project_id, "session_id": session_id, **params},
        )

    async def drive() -> None:
        # --- 1. latency: cold master, then warm multiplexed ops ----------
        t.close()  # ensure a genuinely cold start for the measurement
        t0 = time.monotonic()
        res = await t.run("echo up", timeout_s=30)
        cold_ms = (time.monotonic() - t0) * 1000
        if res.exit_code != 0:
            failures.append(f"cold connect failed: {res.stderr[:200]}")
            return
        warm: list[float] = []
        for _ in range(3):
            t0 = time.monotonic()
            await t.run("true")
            warm.append((time.monotonic() - t0) * 1000)
        probe = await t.probe(refresh=True)
        report.append(
            f"connect: cold {cold_ms:.0f}ms, warm {min(warm):.0f}–{max(warm):.0f}ms · "
            f"remote: {probe['uname']}, home={probe['home']}, git={probe['git']}"
        )
        print(f"  [PASS] probe: {report[-1]}")

        # --- 2. seed the scratch repo (inside the boundary only) ---------
        await t.run(f"mkdir -p {_PATH}")
        await t.write_file(f"{_PATH}/README.md", _ORIGINAL.encode())
        res = await t.run(
            f"git init -q && git add -A && git {_GIT_ID} commit -qm baseline",
            cwd=_PATH,
        )
        if res.exit_code != 0:
            failures.append(f"seed repo failed: {res.stderr[:200]}")
            return
        head_before = (await t.run("git rev-parse HEAD", cwd=_PATH)).stdout.strip()
        print("  [PASS] scratch repo seeded (baseline commit)")

        # --- 3. the REAL agent turn --------------------------------------
        t0 = time.monotonic()
        await run_turn()
        turn_s = time.monotonic() - t0
        got = (await t.read_file(f"{_PATH}/README.md")).decode()
        bash_got = (await t.read_file(f"{_PATH}/{_BASH_FILE}")).decode()
        if got != _EDITED or bash_got != "created-by-bash":
            failures.append(f"turn artifacts wrong: {got!r} / {bash_got!r}")
            return
        report.append(f"agent turn (write_file + run_bash + checkpoint): {turn_s:.1f}s")
        print(f"  [PASS] real agent turn mutated the device ({turn_s:.1f}s)")

        # --- 4. panel routes + backend truth -----------------------------
        t0 = time.monotonic()
        tree = _panel("tree", path="").json()
        tree_ms = (time.monotonic() - t0) * 1000
        names = [e["name"] for e in tree.get("entries", [])]
        if not (tree.get("root_exists") and "README.md" in names and ".git" not in names):
            failures.append(f"tree: {names}")
        else:
            print(f"  [PASS] panel tree ({tree_ms:.0f}ms; .git hidden)")

        read = _panel("read", path="README.md").json()
        if read.get("text") != _EDITED:
            failures.append(f"panel read: {read}")
        else:
            print("  [PASS] panel read")

        st = _panel("status").json()
        marks = st.get("statuses", {})
        if not (
            st.get("is_repo")
            and marks.get("README.md") == "modified"
            and marks.get(_BASH_FILE) == "new"
        ):
            failures.append(f"status markers: {st}")
        else:
            print(f"  [PASS] git change markers: README modified, {_BASH_FILE} new")

        sb = web.get(
            "/desktop/sessions/backend",
            params={"session_id": session_id, "project_id": project_id},
        ).json()
        if not (sb.get("source") == "live" and sb.get("backend") == "ssh" and sb.get("detail") == _HOST):
            failures.append(f"backend truth: {sb}")
        else:
            print(f"  [PASS] badge truth: live ssh · {_HOST}")

        # --- 5. undo ------------------------------------------------------
        lst = web.get(
            "/desktop/checkpoint/list",
            params={"project_id": project_id, "session_id": session_id},
        ).json()
        if not (lst.get("supported") and len(lst.get("checkpoints", [])) == 1):
            failures.append(f"checkpoint list: {lst}")
            return
        res = await dc.restore_remote(project_id, session_id, t, _PATH)
        if res.get("status") != "ok":
            failures.append(f"undo failed: {res}")
            return
        got = (await t.read_file(f"{_PATH}/README.md")).decode()
        gone = (await t.run(f"[ -e {_PATH}/{_BASH_FILE} ] && echo yes || echo no")).stdout.strip()
        head_after = (await t.run("git rev-parse HEAD", cwd=_PATH)).stdout.strip()
        if got != _ORIGINAL:
            failures.append(f"undo did not revert README: {got!r}")
        elif gone != "no":
            failures.append("undo left the turn-created file behind")
        elif head_after != head_before:
            failures.append("scratch repo HEAD moved (must never happen)")
        else:
            print("  [PASS] undo: README reverted, turn's new file removed, repo HEAD untouched")

    asyncio.run(drive())

    print()
    for line in report:
        print(f"  · {line}")
    print(f"  · artifacts left in place: {_HOST}:{_PATH} (+ remote ~/.adk-cc/checkpoints/{project_id}/)")
    if failures:
        print("\nFAIL — ssh device acceptance:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("\nssh device acceptance: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
