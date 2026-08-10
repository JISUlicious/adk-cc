"""Upload routes end-to-end against real servers (#121 P0).

Boots the actual `make_app` server twice — desktop mode and web mode — and
proves the binary round-trip through each route, the policy edges
(unknown project, overwrite, size cap, unsafe name), and the /api/turns
inline-blob hardening rider. No model calls: uploads and the 413 rider both
resolve before any turn runs.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_upload_routes.py
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")

import requests

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


BLOB = bytes(range(256)) * 512  # 128 KiB, not valid utf-8
SHA = hashlib.sha256(BLOB).hexdigest()


def _sealed_env(data_dir: str, extra: dict) -> dict:
    env = dict(os.environ)
    # Seal every door (feedback-sealed-e2e-env): shell leaks + repo .env.
    for k in list(env):
        if k.startswith(("ADK_CC_UPLOAD", "ADK_CC_TURN_INLINE",
                         "ADK_CC_SANDBOX", "ADK_CC_DESKTOP",
                         "ADK_CC_WORKSPACE_ROOT", "ADK_CC_TENANCY")):
            env.pop(k)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_API_KEY": "stub", "ADK_CC_DATA_DIR": data_dir,
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_SANDBOX_BACKEND": "noop",
        "PYTHONPATH": str(REPO / "agents"),
    })
    env.update(extra)
    return env


def _boot(port: int, env: dict, log_path: str):
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            if requests.get(base + "/list-apps", timeout=2).ok:
                return proc, base
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"server on :{port} never came up — see {log_path}")


def desktop_scenario() -> None:
    data = tempfile.mkdtemp(prefix="upl-desk-")
    proj = tempfile.mkdtemp(prefix="upl-proj-")
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    env = _sealed_env(data, {
        "ADK_CC_DESKTOP": "1", "ADK_CC_DESKTOP_DATA": data,
        "ADK_CC_TENANCY_MODE": "single", "ADK_CC_GLOBAL_TENANT_ID": "local",
    })
    proc, base = _boot(8951, env, os.path.join(data, "server.log"))
    try:
        pid = requests.post(base + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        u = f"{base}/desktop/uploads/data.bin?project_id={pid}&session_id=s1"

        r = requests.put(u, data=BLOB,
                         headers={"Content-Type": "application/octet-stream"},
                         timeout=30)
        check("desktop: upload accepted", r.status_code == 200, (r.status_code, r.text[:200]))
        dest = Path(proj) / "uploads" / "data.bin"
        check("desktop: file lands in the PROJECT workspace", dest.is_file())
        check("desktop: bytes binary-exact (sha256)",
              dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == SHA)
        check("desktop: response carries rel path",
              r.ok and r.json()["upload"]["rel_path"] == "uploads/data.bin", r.text[:200])

        r2 = requests.put(u, data=b"x", timeout=15)
        check("desktop: duplicate without overwrite → 409", r2.status_code == 409, r2.status_code)
        r3 = requests.put(u + "&overwrite=1", data=b"x", timeout=15)
        check("desktop: overwrite=1 replaces",
              r3.status_code == 200 and dest.read_bytes() == b"x")

        # An encoded slash never matches the single-segment route (404); a
        # literal dotfile reaches the validator (400). Both must refuse.
        r4 = requests.put(
            f"{base}/desktop/uploads/..%2Fescape?project_id={pid}&session_id=s1",
            data=b"x", timeout=15)
        check("desktop: traversal name rejected",
              r4.status_code in (400, 404), r4.status_code)
        r4b = requests.put(
            f"{base}/desktop/uploads/.env?project_id={pid}&session_id=s1",
            data=b"x", timeout=15)
        check("desktop: dotfile name rejected", r4b.status_code == 400,
              r4b.status_code)
        check("desktop: nothing escaped the workspace",
              not (Path(proj).parent / "escape").exists())

        r5 = requests.put(
            f"{base}/desktop/uploads/x.bin?project_id=nope&session_id=s1",
            data=b"x", timeout=15)
        check("desktop: unknown project → 404", r5.status_code == 404, r5.status_code)

        big = requests.put(u.replace("data.bin", "big.bin"), data=b"x" * 2048,
                           headers={"Content-Length": "2048"}, timeout=15)
        # cap unset → 100MB default: 2KB passes; now assert the env cap works
        check("desktop: small file under default cap passes", big.status_code == 200)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


def web_scenario() -> None:
    data = tempfile.mkdtemp(prefix="upl-web-")
    wsroot = tempfile.mkdtemp(prefix="upl-wsroot-")
    env = _sealed_env(data, {
        "ADK_CC_WORKSPACE_ROOT": wsroot,
        "ADK_CC_UPLOAD_MAX_MB": "0.001",  # ~1 KB — prove the cap end to end
    })
    proc, base = _boot(8952, env, os.path.join(data, "server.log"))
    try:
        u = f"{base}/api/uploads/f.bin?session_id=s1&user_id=u1"
        r = requests.put(u, data=b"tiny-binary\xff\xfe", timeout=30)
        check("web: upload accepted", r.status_code == 200, (r.status_code, r.text[:200]))
        dest = Path(wsroot) / "local" / "u1" / "uploads" / "f.bin"
        check("web: file lands in the tenant/user workspace", dest.is_file(),
              str(dest))
        check("web: bytes exact",
              dest.is_file() and dest.read_bytes() == b"tiny-binary\xff\xfe")

        rcap = requests.put(u.replace("f.bin", "big.bin"), data=b"x" * 4096,
                            timeout=15)
        check("web: cap enforced → 413", rcap.status_code == 413, rcap.status_code)

        # ---- /api/turns inline-blob rider --------------------------------
        sess = f"{base}/apps/adk_cc/users/u1/sessions/s-rider"
        requests.post(sess, json={}, timeout=30)
        blob_b64 = base64.b64encode(b"z" * (2 * 1024 * 1024)).decode()
        rt = requests.post(f"{base}/api/turns", timeout=30, json={
            "appName": "adk_cc", "userId": "u1", "sessionId": "s-rider",
            "newMessage": {"role": "user", "parts": [
                {"text": "look at this"},
                {"inlineData": {"mimeType": "application/octet-stream",
                                "data": blob_b64}}]}})
        check("turns rider: oversized inline blob → 413",
              rt.status_code == 413, (rt.status_code, rt.text[:200]))
        check("turns rider: message points at the upload path",
              "upload" in (rt.text or "").lower(), rt.text[:200])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


def main() -> int:
    print("== desktop mode")
    desktop_scenario()
    print("== web mode")
    web_scenario()
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
