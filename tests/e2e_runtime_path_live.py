"""P1 firm live test: the MODEL reads a /workspace path through the product.

The unit and backend-level checks proved resolve() and to_host_path in
isolation. This is the production shape end to end: a real desktop server on
the real DockerBackend, a real model, and a prompt that makes the model call
read_file with the RUNTIME spelling — the exact call that failed on the
remote with `read denied by fs_read: /workspace/...`.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_runtime_path_live.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
PORT = 8993
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
MARK = "P1-MARKER-93517"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs a live model (ADK_CC_LIVE=1)."); return 0
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP: docker daemon not running."); return 0

    data = tempfile.mkdtemp(prefix="p1live-data-")
    proj = tempfile.mkdtemp(prefix="p1live-proj-")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=False)
    Path(proj, "notes.txt").write_text(f"the secret phrase is {MARK}\n")

    env = dict(os.environ)
    env.update({
        "ADK_CC_MODEL_REGISTRY_FILE": os.path.expanduser(
            "~/.adk-cc-desktop/admin-data/model-endpoints.json"),
        "ADK_CC_AGENTS_DIR": str(REPO / "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_DATA_DIR": data,
        "ADK_CC_TENANCY_MODE": "single", "ADK_CC_GLOBAL_TENANT_ID": "local",
        # The REAL docker backend — the mode the failure was reported from.
        "ADK_CC_SANDBOX_BACKEND": "docker", "ADK_CC_SANDBOX_NETWORK": "0",
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
    })
    log = os.path.join(data, "server.log")
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(REPO), env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(200):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        sid = "p1-live"
        sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
        requests.post(sess, json={}, timeout=30)
        requests.patch(sess, json={"state_delta": {
            "model_endpoint": "chatgpt-codex", "model_id": MODEL,
            "permission_mode": "bypassPermissions"}}, timeout=30)

        t = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{"text":
                'Call the read_file tool with path="/workspace/notes.txt" — '
                'use exactly that absolute path, do not substitute another. '
                'Then repeat the secret phrase it contains verbatim.'}]}}).json()
        for _ in range(80):
            time.sleep(3)
            st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
            if st["status"] != "running":
                break
        check("the turn completed", st.get("status") == "done", str(st)[:200])

        blob = requests.get(sess, timeout=30).text
        ev = json.loads(blob)
        # The model must have actually used the runtime spelling…
        check("the model called read_file with /workspace/notes.txt",
              "/workspace/notes.txt" in blob, "it substituted another path")
        # …and the product must have SERVED it rather than denying it.
        check("no fs_read denial anywhere in the session",
              "denied by fs_read" not in blob, "the production failure is back")
        texts = " ".join(
            p.get("text", "") for e in ev.get("events", [])[-6:]
            for p in (e.get("content") or {}).get("parts") or [])
        check("the model's reply carries the file's content",
              MARK in texts, texts[-200:])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["docker", "rm", "-f", f"adk-cc-{sid}"],
                       capture_output=True)

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        print(f"server log: {log}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
