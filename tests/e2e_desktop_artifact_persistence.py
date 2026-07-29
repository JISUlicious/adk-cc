"""#73: a desktop artifact must survive an app restart.

The bug this pins: sessions persist (sqlite/JSONL) while artifacts lived only in
memory, so reopening yesterday's conversation showed the chart chip with
`preview failed: 404 Not Found`. Reproduced exactly that way — a probe restarted
the server against a persisted session and the chip 404'd.

So the test restarts the server for real. Anything short of that (asserting the
URI, or reading the service in-process) would have passed on the broken build.

Run: .venv/bin/python tests/e2e_desktop_artifact_persistence.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8947
BASE = f"http://127.0.0.1:{PORT}"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def boot(data):
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "sessions.db"),
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
    })
    env.pop("ADK_CC_ARTIFACT_STORAGE_URI", None)   # the DEFAULT is what's under test
    p = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            if requests.get(BASE + "/list-apps", timeout=2).ok:
                return p
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not start")


def main() -> int:
    data = tempfile.mkdtemp(prefix="artifact-persist-")
    user, sid, fname = "proj1", "sess1", "chart.html"
    body = b"<html><body>persisted chart</body></html>"

    proc = boot(data)
    try:
        base = f"{BASE}/apps/adk_cc/users/{user}/sessions/{sid}"
        requests.post(base, json={}, timeout=30)
        # Save through the same service the agent's save_as_artifact uses.
        import base64
        r = requests.post(
            f"{BASE}/apps/adk_cc/users/{user}/sessions/{sid}/artifacts/{fname}",
            json={"inlineData": {"displayName": fname,
                                 "data": base64.b64encode(body).decode(),
                                 "mimeType": "text/html"}}, timeout=30)
        if not r.ok:   # older/newer ADK route shape — fall back to the service
            print(f"    (artifact POST returned {r.status_code}; using the service directly)")
            import sys
            sys.path.insert(0, os.path.join(REPO, "agents"))
            import asyncio
            from google.adk.cli.service_registry import get_service_registry
            from google.genai import types
            svc = get_service_registry().create_artifact_service(
                f"file://{os.path.join(data, 'artifacts')}")
            asyncio.run(svc.save_artifact(
                app_name="adk_cc", user_id=user, session_id=sid, filename=fname,
                artifact=types.Part(inline_data=types.Blob(data=body, mime_type="text/html"))))

        got = requests.get(f"{base}/artifacts/{fname}", timeout=30)
        check("artifact readable before restart", got.ok, f"{got.status_code} {got.text[:120]}")
        on_disk = os.path.isdir(os.path.join(data, "artifacts"))
        check("artifacts were written to disk, not memory", on_disk,
              f"no {data}/artifacts dir — still in-memory?")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    # THE point: restart, same data dir.
    proc = boot(data)
    try:
        base = f"{BASE}/apps/adk_cc/users/{user}/sessions/{sid}"
        sess = requests.get(base, timeout=30)
        check("the session survived the restart", sess.ok, f"{sess.status_code}")
        got = requests.get(f"{base}/artifacts/{fname}", timeout=30)
        check("the artifact ALSO survived (this is the 404 that was reported)",
              got.ok, f"{got.status_code} {got.text[:120]}")
        if got.ok:
            import base64 as b64
            payload = got.json()
            raw = (payload.get("inlineData") or payload.get("inline_data") or {}).get("data", "")
            # ADK returns URL-SAFE base64 (`-`/`_`), which plain b64decode rejects.
            check("its bytes are intact", b64.urlsafe_b64decode(raw) == body,
                  str(payload)[:160])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
