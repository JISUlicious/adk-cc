"""#75/#121-P2 opt-in live: dataset + upload delivery on a REAL Daytona sandbox.

Creates a throwaway sandbox (deleted on close), then drives the exact helpers
the routes use: deliver_upload (binary-exact readback), dataset put → list →
stat → env snapshot → delete. Profile/provisioning is covered by the docker
and ssh acceptances — a throwaway sandbox should not pay for a full env build.

Config from .env (ADK_CC_DAYTONA_*); SKIPS (exit 0) when unset/unreachable.

Run: .venv/bin/python tests/e2e_datasets_daytona.py
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _env_from_dotenv() -> dict[str, str]:
    cfg: dict[str, str] = {}
    p = REPO / ".env"
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("ADK_CC_DAYTONA_") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


async def _run(cfg: dict[str, str]) -> None:
    from adk_cc.sandbox.backends.daytona_backend import (
        DaytonaBackend,
        _derive_proxy_url,
    )
    from adk_cc.sandbox import analysis_env as ae
    from adk_cc.sandbox.workspace import WorkspaceRoot
    from adk_cc.service import datasets_backend as dsb
    from adk_cc.service.uploads import deliver_upload

    nonce = secrets.token_hex(4)
    session_id = f"dstest-{nonce}"
    ws_path = "/home/daytona"
    api_url = cfg["ADK_CC_DAYTONA_API_URL"]

    backend = DaytonaBackend(
        session_id=session_id,
        tenant_id="local",
        api_url=api_url,
        proxy_url=cfg.get("ADK_CC_DAYTONA_PROXY_URL") or _derive_proxy_url(api_url),
        api_key=cfg.get("ADK_CC_DAYTONA_API_KEY"),
        snapshot=cfg.get("ADK_CC_DAYTONA_SNAPSHOT") or None,
        workspace_path=ws_path,
        delete_on_close=True,  # throwaway — clean up immediately
        verify_ssl=cfg.get("ADK_CC_DAYTONA_VERIFY_SSL", "1") != "0",
    )
    ws = WorkspaceRoot(tenant_id="local", session_id=session_id,
                       abs_path=ws_path)
    print(f"creating real sandbox adk-cc-{session_id}…")
    try:
        await backend.ensure_workspace(ws)

        blob = bytes(range(256)) * 40  # 10KiB binary, not utf-8
        row = await deliver_upload(ws, backend, "probe.bin", blob,
                                   overwrite=True)
        check("upload delivered", row["rel_path"] == "uploads/probe.bin"
              and row["bytes"] == len(blob), row)
        back = await backend.read_bytes(f"{ws_path}/uploads/probe.bin",
                                        fs_read=ws.fs_read_config())
        check("upload binary-exact readback", back == blob,
              f"{len(back)} bytes")

        csv = b"a,b\n1,2\n3,4\n"
        drow = await dsb.put_via(ws, backend, "t.csv", csv)
        check("dataset put lands", drow["name"] == "t.csv"
              and drow["bytes"] == len(csv), drow)
        rows = await dsb.listing_via(ws, backend)
        check("dataset listing sees it", "t.csv" in [r["name"] for r in rows],
              rows)
        check("stat matches",
              ((await dsb.stat_via(ws, backend, "t.csv")) or {}).get("bytes")
              == len(csv))

        st = await ae.status_via(backend, ws)
        check("env snapshot runs in the sandbox (absent, not unknown)",
              st.get("state") in ("absent", "ready", "external"), st)

        check("dataset delete", await dsb.remove_via(ws, backend, "t.csv")
              is True)
    finally:
        await backend.close()
        print("  (throwaway sandbox deleted)")


def main() -> int:
    cfg = _env_from_dotenv()
    api_url = cfg.get("ADK_CC_DAYTONA_API_URL") or ""
    if not api_url:
        print("SKIP: ADK_CC_DAYTONA_API_URL not set in .env")
        return 0
    if not cfg.get("ADK_CC_DAYTONA_API_KEY"):
        print("SKIP: ADK_CC_DAYTONA_API_KEY not set in .env")
        return 0
    try:
        import ssl

        # Reachability only — a self-signed cert answering IS reachable; the
        # backend applies the configured verify_ssl/CA policy itself.
        urllib.request.urlopen(api_url, timeout=5,
                               context=ssl._create_unverified_context())
    except Exception as e:  # noqa: BLE001 — any HTTP answer means reachable
        if "HTTP" not in type(e).__name__:
            print(f"SKIP: {api_url} not reachable ({e})")
            return 0
    asyncio.run(_run(cfg))
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
