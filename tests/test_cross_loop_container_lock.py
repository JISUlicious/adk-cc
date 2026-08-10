"""Two private event loops sharing one container backend must not deadlock.

Client-diagnosed from production (17min+ "agent is working"): after a
confirmation BUNDLE, two run_skill_scripts execute in parallel; each
code_executor call runs in its own thread with its OWN asyncio.run loop; both
hit DockerBackend._ensure_container's asyncio.Lock. A Lock waiter's wake-up
future belongs to the loop that created it, so release from loop A never
wakes loop B — the second script waits forever. Mirror of the Daytona #116
freeze (a threading.Lock blocking a loop); the fix is a threading.Lock
acquired OFF-loop via to_thread, so waiters park in worker threads.

Needs Docker; skips cleanly without. 60s watchdog = the old code FAILS here.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_cross_loop_container_lock.py
"""
from __future__ import annotations
import asyncio, os, shutil, subprocess, sys, tempfile, threading
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ.setdefault("ADK_CC_SANDBOX_NETWORK", "0")

def main() -> int:
    if not shutil.which("docker") or subprocess.run(["docker","info"],capture_output=True).returncode:
        print("SKIP: docker not available."); return 0
    if subprocess.run(["docker","image","inspect","adk-cc-sandbox:latest"],capture_output=True).returncode:
        print("SKIP: sandbox image not built."); return 0
    from adk_cc.sandbox.backends.docker_backend import DockerBackend
    from adk_cc.sandbox.workspace import WorkspaceRoot
    ws_dir = tempfile.mkdtemp(prefix="xloop-")
    ws = WorkspaceRoot(tenant_id="t", session_id="xloop", abs_path=ws_dir)
    be = DockerBackend(session_id="xloop", tenant_id="t", workspace_abs_path=ws.abs_path)
    fsw = ws.fs_write_config()
    results, threads = {}, []
    def run(tag):
        # The production shape verbatim: a fresh loop per thread.
        async def go():
            await be.write_text(os.path.join(ws.abs_path, f"{tag}.txt"),
                                f"{tag}-ok\n", fs_write=fsw)
            return True
        try: results[tag] = asyncio.run(go())
        except Exception as e: results[tag] = f"{type(e).__name__}: {e}"
    for tag in ("alpha", "beta"):
        t = threading.Thread(target=run, args=(tag,), daemon=True); t.start(); threads.append(t)
    for t in threads: t.join(timeout=60)
    hung = [t for t in threads if t.is_alive()]
    ok = not hung and results.get("alpha") is True and results.get("beta") is True
    print(f"  [{'PASS' if ok else 'FAIL'}] two loops, one backend, no deadlock — "
          f"results={results} hung={len(hung)}")
    subprocess.run(["docker","rm","-f","adk-cc-xloop"],capture_output=True)
    asyncio.run(be.close()) if not hung else None
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
