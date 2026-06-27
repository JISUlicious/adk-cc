"""HTTP e2e: non-secret declared variables surface their value; secrets don't.

alice uploads a skill declaring one NON-secret input (secret:false) and one
SECRET input. Verifies /auth/secrets:
  - reports `secret` per input,
  - returns the `value` of a non-secret input once set (plain text, editable),
  - NEVER returns a value for a secret input (hygiene preserved).
Model-free.

Run: .venv/bin/python tests/e2e_nonsecret_vars.py
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
PORT = 8934
BASE = f"http://127.0.0.1:{PORT}"
_passed = _failed = 0


def check(name, ok):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    _passed += 1 if ok else 0
    _failed += 0 if ok else 1


def _skill_zip() -> bytes:
    decl = (
        '[{"id":"MY_REGION","secret":false,"description":"Deploy region"},'
        '{"id":"MY_TOKEN","secret":true,"description":"API token"}]'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "SKILL.md",
            "---\nname: cfgskill\ndescription: non-secret config + a secret token.\n"
            f"metadata:\n  x-adk-cc/secrets: '{decl}'\n---\n\nBody.\n",
        )
    return buf.getvalue()


def _tok(email, pw):
    return requests.post(BASE + "/auth/login", json={"email": email, "password": pw}, timeout=10).json()["access_token"]


def _input(headers, group_name, key):
    groups = requests.get(BASE + "/auth/secrets", headers=headers, timeout=10).json()["groups"]
    g = next((g for g in groups if g["name"] == group_name), None)
    if not g:
        return None
    return next((it for it in g["inputs"] if it["key"] == key), None)


def main() -> int:
    root = tempfile.mkdtemp(prefix="nonsecret-")
    iddir = os.path.join(root, "identity"); os.makedirs(iddir)
    from adk_cc.identity.store import JsonFileUserStore
    from adk_cc.identity.provider import EmailPasswordProvider
    store = JsonFileUserStore(os.path.join(iddir, "users.json"))
    prov = EmailPasswordProvider(store, mode="single", global_tenant_id="acme")
    prov.provision(email="alice@acme.io", password="password123", tenant_id="acme", roles=["admin"])

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

        r = requests.put(BASE + "/auth/skills/cfgskill", headers=a, data=_skill_zip(), timeout=10)
        check("alice uploads the config skill", r.status_code == 200)

        # --- before setting any value: flags present, no values ---
        region = _input(a, "cfgskill", "MY_REGION")
        token = _input(a, "cfgskill", "MY_TOKEN")
        check("non-secret input reports secret=false", region is not None and region["secret"] is False)
        check("secret input reports secret=true", token is not None and token["secret"] is True)
        check("unset non-secret has no value", region is not None and "value" not in region)
        check("both start unset", region["status"] == "unset" and token["status"] == "unset")

        # --- set both ---
        requests.put(BASE + "/auth/secrets/MY_REGION", headers=a, json={"value": "ap-northeast-2"}, timeout=10)
        requests.put(BASE + "/auth/secrets/MY_TOKEN", headers=a, json={"value": "super-secret-xyz"}, timeout=10)

        region = _input(a, "cfgskill", "MY_REGION")
        token = _input(a, "cfgskill", "MY_TOKEN")
        check("non-secret value is returned after set", region.get("value") == "ap-northeast-2")
        check("non-secret status is user", region["status"] == "user")
        check("SECRET value is NEVER returned", "value" not in token)
        check("secret status is user (set), still secret=true", token["status"] == "user" and token["secret"] is True)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(root, ignore_errors=True)

    print(f"\nnon-secret vars e2e: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
