"""HTTP e2e: per-user self-service MCP servers & skills (Phase 4).

alice adds a personal MCP server + uploads a personal skill; verifies they're
listed for her, isolated from bob, and that their required env vars surface in
the grouped /auth/secrets. Model-free.

Run: .venv/bin/python tests/e2e_user_mcp_skills.py
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8932
BASE = f"http://127.0.0.1:{PORT}"
_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    _passed += 1 if ok else 0
    _failed += 0 if ok else 1


def _skill_zip(name: str, secret: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: A personal skill {name} needing a token.\n"
            f"metadata:\n  x-adk-cc/secrets: '[{{\"id\":\"{secret}\"}}]'\n---\n\nBody.\n",
        )
    return buf.getvalue()


def _tok(email, pw):
    return requests.post(BASE + "/auth/login", json={"email": email, "password": pw}, timeout=10).json()["access_token"]


def main() -> int:
    root = tempfile.mkdtemp(prefix="user-mcpskills-")
    iddir = os.path.join(root, "identity"); os.makedirs(iddir)
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    prov = EmailPasswordProvider(store, mode="single", global_tenant_id="acme")
    prov.provision(email="alice@acme.io", password="password123", tenant_id="acme", roles=["admin"])
    prov.provision(email="bob@acme.io", password="password123", tenant_id="acme", roles=["member"])

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_AUTH_PASSWORD": "1", "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "acme", "ADK_CC_IDENTITY_DIR": iddir,
        "ADK_CC_TENANT_REGISTRY_DIR": os.path.join(root, "registry"),
        "ADK_CC_TENANT_SKILLS_DIR": os.path.join(root, "skills"),
        "ADK_CC_CREDENTIAL_PROVIDER": "memory",
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
    })
    env.pop("ADK_CC_ALLOW_NO_AUTH", None)
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                if requests.get(BASE + "/auth/config", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        a = {"Authorization": f"Bearer {_tok('alice@acme.io', 'password123')}"}
        b = {"Authorization": f"Bearer {_tok('bob@acme.io', 'password123')}"}

        # --- MCP: alice adds a personal server ---
        r = requests.put(BASE + "/auth/mcp-servers/mybox", headers=a,
                         json={"transport": "http", "url": "https://example/mcp", "credential_key": "MYBOX_TOKEN"}, timeout=10)
        check("alice PUT personal MCP server", r.status_code == 200)
        alice_mcp = requests.get(BASE + "/auth/mcp-servers", headers=a, timeout=10).json()["servers"]
        check("alice sees her MCP server (scope user)",
              any(s["server_name"] == "mybox" and s["scope"] == "user" for s in alice_mcp))
        bob_mcp = requests.get(BASE + "/auth/mcp-servers", headers=b, timeout=10).json()["servers"]
        check("bob does NOT see alice's MCP server", not any(s["server_name"] == "mybox" for s in bob_mcp))

        # --- Skills: alice uploads a personal skill ---
        r = requests.put(BASE + "/auth/skills/myskill", headers=a, data=_skill_zip("myskill", "MYSKILL_TOKEN"), timeout=10)
        check("alice uploads a personal skill", r.status_code == 200)
        alice_sk = requests.get(BASE + "/auth/skills", headers=a, timeout=10).json()["skills"]
        check("alice sees her skill", "myskill" in alice_sk)
        bob_sk = requests.get(BASE + "/auth/skills", headers=b, timeout=10).json()["skills"]
        check("bob does NOT see alice's skill", "myskill" not in bob_sk)

        # --- grouped secrets reflect both (per-user discovery) ---
        groups = requests.get(BASE + "/auth/secrets", headers=a, timeout=10).json()["groups"]
        names = {(g["kind"], g["name"]) for g in groups}
        keys = {i["key"] for g in groups for i in g["inputs"]}
        check("secrets group for the personal MCP server", ("mcp", "mybox") in names and "MYBOX_TOKEN" in keys)
        check("secrets group for the personal skill", ("skill", "myskill") in names and "MYSKILL_TOKEN" in keys)
        bob_groups = requests.get(BASE + "/auth/secrets", headers=b, timeout=10).json()["groups"]
        check("bob's secrets don't include alice's groups",
              not any(g["name"] in ("mybox", "myskill") for g in bob_groups))

        # --- delete ---
        requests.delete(BASE + "/auth/mcp-servers/mybox", headers=a, timeout=10)
        requests.delete(BASE + "/auth/skills/myskill", headers=a, timeout=10)
        check("alice's MCP server removed",
              not any(s["server_name"] == "mybox" for s in requests.get(BASE + "/auth/mcp-servers", headers=a, timeout=10).json()["servers"]))
        check("alice's skill removed",
              "myskill" not in requests.get(BASE + "/auth/skills", headers=a, timeout=10).json()["skills"])

        print(f"\nuser MCP/skills e2e: {_passed} passed, {_failed} failed")
        return 1 if _failed else 0
    finally:
        proc.kill()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
