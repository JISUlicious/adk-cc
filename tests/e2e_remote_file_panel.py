"""E2E: the desktop file panel serves a REMOTE (SSH) project — PR 5.

Real sshd container as the remote; the REAL /desktop/files/* routes (FastAPI
TestClient, registry-backed, nothing mocked) ride the shared transport:

  - tree: lists the remote root + subdirs (dirs first, `.git` skipped),
    root_exists=false BEFORE the workspace exists, 403 on `..` escape
  - read: utf-8 text + size, 404 for missing, binary flagged
  - status: git markers from the REMOTE repo's working tree when git is
    available in the container (installed via apk at test start), and the
    honest `is_repo:false` degradation when it is not

Benign commands only. Skips gracefully without Docker.

Run: `uv run python tests/e2e_remote_file_panel.py`
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")
os.environ["ADK_CC_DESKTOP"] = "1"
os.environ["ADK_CC_DESKTOP_DATA"] = tempfile.mkdtemp(prefix="adk-rfp-e2e-")

sys.path.insert(0, os.path.dirname(__file__))
from sshd_harness import SshdContainer, wait_ready  # noqa: E402

_WS = "/config/rproj"


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.service.desktop_files import mount_desktop_files_routes

    app = FastAPI()
    mount_desktop_files_routes(app)
    return TestClient(app)


def main() -> int:
    failures: list[str] = []
    with SshdContainer() as box:
        if box is None:
            return 0

        os.environ["ADK_CC_SSH_CONTROL_DIR"] = box.control_dir
        os.environ["ADK_CC_SSH_IDENTITY_FILE"] = box.identity_file
        os.environ["ADK_CC_SSH_EXTRA_OPTS"] = " ".join(box.extra_ssh_opts)

        from adk_cc.sandbox.ssh_transport import SshTransport
        from adk_cc.service.desktop_routes import save_projects

        t = SshTransport(
            box.host,
            port=box.port,
            identity_file=box.identity_file,
            extra_ssh_opts=box.extra_ssh_opts,
            control_dir=box.control_dir + "-seed",
        )

        project_id = "projPanel"
        save_projects(
            [
                {
                    "id": project_id,
                    "name": "rproj",
                    "remote": {"host": box.host, "path": _WS, "port": box.port},
                }
            ]
        )
        c = _client()

        def _get(route: str, **params):
            params = {"project_id": project_id, "session_id": "s1", **params}
            return c.get(f"/desktop/files/{route}", params=params)

        async def drive() -> None:
            err = await wait_ready(t)
            if err:
                failures.append(f"sshd never became ready: {err}")
                return

            # --- BEFORE the workspace exists: honest empty state ---------
            r = _get("tree", path="")
            if not (r.status_code == 200 and r.json()["root_exists"] is False):
                failures.append(f"pre-workspace tree: {r.status_code} {r.text[:120]}")
            else:
                print("  [PASS] tree before workspace exists → root_exists=false")

            # --- seed a workspace over the transport ---------------------
            await t.run(f"mkdir -p {_WS}/sub")
            await t.write_file(f"{_WS}/hello.txt", "hi from the remote\n".encode())
            await t.write_file(f"{_WS}/sub/nested.txt", b"nested")
            await t.write_file(f"{_WS}/blob.bin", bytes([0, 1, 2, 255]))
            await t.run(f"mkdir -p {_WS}/.git")  # must be skipped in listings

            r = _get("tree", path="")
            body = r.json()
            names = [(e["name"], e["type"]) for e in body.get("entries", [])]
            if not (
                r.status_code == 200
                and body["root_exists"] is True
                and names[0] == ("sub", "dir")  # dirs first
                and ("hello.txt", "file") in names
                and all(n != ".git" for n, _ in names)
            ):
                failures.append(f"tree listing: {names}")
            else:
                print("  [PASS] tree lists remote entries (dirs first, .git skipped)")

            r = _get("tree", path="sub")
            if [(e["name"]) for e in r.json().get("entries", [])] != ["nested.txt"]:
                failures.append(f"subdir tree: {r.text[:120]}")
            else:
                print("  [PASS] subdir tree")

            r = _get("tree", path="../escape")
            if r.status_code != 403:
                failures.append(f"escape not rejected: {r.status_code}")
            else:
                print("  [PASS] `..` escape → 403")

            # --- read ----------------------------------------------------
            r = _get("read", path="hello.txt")
            body = r.json()
            if not (
                r.status_code == 200
                and body["text"] == "hi from the remote\n"
                and body["binary"] is False
                and body["size"] == len("hi from the remote\n")
            ):
                failures.append(f"read text: {body}")
            else:
                print("  [PASS] read remote text file (+size)")

            r = _get("read", path="blob.bin")
            if not (r.status_code == 200 and r.json()["binary"] is True):
                failures.append(f"read binary: {r.text[:120]}")
            else:
                print("  [PASS] binary flagged")

            r = _get("read", path="missing.txt")
            if r.status_code != 404:
                failures.append(f"missing read: {r.status_code}")
            else:
                print("  [PASS] missing file → 404")

            # --- status: no git → honest degradation ---------------------
            r = _get("status")
            if r.json() != {"is_repo": False, "statuses": {}}:
                failures.append(f"no-git status: {r.text[:120]}")
            else:
                print("  [PASS] no git on remote → is_repo:false (no markers)")

            # --- install git in the container; full marker flow ----------
            apk = subprocess.run(
                ["docker", "exec", box.container_id, "apk", "add", "--no-cache", "git"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if apk.returncode != 0:
                print("  [SKIP] could not install git in container (offline?) — "
                      "marker flow not exercised")
                return
            # New probe needed (probe caches git=False) → fresh transport ops
            # use the panel's transport; refresh ITS probe via the route path:
            # the panel calls t.probe() (cached). Force refresh through a
            # direct probe on the SAME registry transport.
            from adk_cc.sandbox.ssh_transport import get_transport

            panel_t = get_transport(box.host, port=box.port)
            await panel_t.probe(refresh=True)

            await t.run(
                "git init -q && git add -A && "
                "git -c user.email=t@t -c user.name=t commit -qm init",
                cwd=_WS,
            )
            await t.write_file(f"{_WS}/hello.txt", b"CHANGED\n")
            await t.write_file(f"{_WS}/fresh.txt", b"new file")

            r = _get("status")
            body = r.json()
            st = body.get("statuses", {})
            if not (
                body.get("is_repo") is True
                and st.get("hello.txt") == "modified"
                and st.get("fresh.txt") == "new"
            ):
                failures.append(f"git status markers: {body}")
            else:
                print("  [PASS] remote git markers: modified + new")

        asyncio.run(drive())

    if failures:
        print("\nFAIL — remote file panel e2e:")
        for m in failures:
            print(f"  [FAIL] {m}")
        return 1
    print("\nremote file panel e2e: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
