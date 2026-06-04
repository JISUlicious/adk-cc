"""E2E: prove the REAL Daytona honors creation-time env injection.

The mock tests (test_daytona_backend.py) assert what WE send in the
`POST /api/sandbox` payload. They CANNOT prove what Daytona DOES with it —
that it accepts an `env` field and exposes those vars to commands run via
`process/execute`. This closes that gap against a live Daytona:

  1. create a real sandbox with env_spec static={ADK_CC_ENVTEST: <nonce>}
  2. run `echo "$ADK_CC_ENVTEST"` INSIDE the sandbox
  3. assert the command sees the injected value
  4. delete the throwaway sandbox (delete_on_close)

Config is read from .env (ADK_CC_DAYTONA_*). SKIPS (exit 0) when the
instance is unconfigured or unreachable, so it never breaks a run on a box
without Daytona. Run directly:

    .venv/bin/python tests/e2e_daytona_env.py
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
from urllib.parse import urlsplit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Don't pull in the agent's full dotenv/bootstrap — we only need the
# daytona knobs, parsed straight from .env so the test is self-contained.
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")


def _load_env_file() -> dict[str, str]:
    path = os.path.join(REPO, ".env")
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if not k.startswith("ADK_CC_DAYTONA_"):
                continue
            v = v.strip().strip('"').strip("'")
            out[k] = v
    return out


def _reachable(api_url: str, timeout: float = 4.0) -> bool:
    parts = urlsplit(api_url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return False
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _skip(msg: str) -> int:
    print(f"SKIP daytona-env e2e: {msg}")
    return 0


async def _run(cfg: dict[str, str]) -> int:
    from adk_cc.sandbox.backends.daytona_backend import (
        DaytonaBackend,
        _derive_proxy_url,
    )
    from adk_cc.sandbox.sandbox_env import SandboxEnvSpec
    from adk_cc.sandbox.workspace import WorkspaceRoot
    from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig

    api_url = cfg["ADK_CC_DAYTONA_API_URL"]
    nonce = secrets.token_hex(4)
    value = f"hello-from-create-{nonce}"
    session_id = f"envtest-{nonce}"
    # /home/daytona always exists and is writable by the sandbox user, so
    # the workspace-dir preflight and exec cwd are never the variable here —
    # we're isolating the env-injection behavior.
    ws_path = "/home/daytona"

    backend = DaytonaBackend(
        session_id=session_id,
        tenant_id="local",
        api_url=api_url,
        proxy_url=cfg.get("ADK_CC_DAYTONA_PROXY_URL") or _derive_proxy_url(api_url),
        api_key=cfg.get("ADK_CC_DAYTONA_API_KEY"),
        env_spec=SandboxEnvSpec(static={"ADK_CC_ENVTEST": value}),
        snapshot=cfg.get("ADK_CC_DAYTONA_SNAPSHOT") or None,
        workspace_path=ws_path,
        delete_on_close=True,  # throwaway — clean up immediately
        verify_ssl=cfg.get("ADK_CC_DAYTONA_VERIFY_SSL", "1") != "0",
    )
    ws = WorkspaceRoot(
        tenant_id="local", session_id=session_id, abs_path=ws_path
    )

    print(f"creating real sandbox adk-cc-{session_id} with env ADK_CC_ENVTEST…")
    ok = False
    try:
        await backend.ensure_workspace(ws)
        res = await backend.exec(
            'echo "$ADK_CC_ENVTEST"',
            fs_write=FsWriteConfig(allow_paths=(ws_path,)),
            network=NetworkConfig(),
            timeout_s=30,
            cwd=ws_path,
        )
        # Daytona merges stdout+stderr into `result` (documented backend
        # quirk), so a shell locale warning can precede the echo output.
        # The injected value is the command's actual stdout — the last
        # non-empty line. Match on that, not the whole blob.
        lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
        got = lines[-1].strip() if lines else ""
        print(f"  in-sandbox `echo $ADK_CC_ENVTEST` -> {got!r} (exit {res.exit_code})")
        ok = res.exit_code == 0 and got == value
        if not ok:
            print(
                f"  FAIL: expected {value!r} as the last output line; the real "
                f"Daytona did NOT expose the create-time env var to the command. "
                f"Full output: {res.stdout!r}"
            )
    finally:
        await backend.close()  # delete the throwaway sandbox
        print("  (throwaway sandbox deleted)")

    print("\ndaytona-env e2e PASSED" if ok else "\ndaytona-env e2e FAILED")
    return 0 if ok else 1


def main() -> int:
    cfg = _load_env_file()
    api_url = cfg.get("ADK_CC_DAYTONA_API_URL")
    if not api_url:
        return _skip("ADK_CC_DAYTONA_API_URL not set in .env")
    if not (cfg.get("ADK_CC_DAYTONA_API_KEY")):
        return _skip("ADK_CC_DAYTONA_API_KEY not set in .env (single-tenant path)")
    if not _reachable(api_url):
        return _skip(f"{api_url} not reachable")
    return asyncio.run(_run(cfg))


if __name__ == "__main__":
    sys.exit(main())
