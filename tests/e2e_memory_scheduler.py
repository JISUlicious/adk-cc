"""e2e: the in-process memory-consolidation scheduler runs on server boot.

Proves that binding consolidation to the API server's lifespan
(ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S) actually grows semantic memory while the
server is up — WITHOUT running the external cron. Model-free: deterministic
latest-wins consolidation needs no model, so this is fast and rate-limit-free.

Seed alice's episodic captures, boot uvicorn with a short interval, then poll
her SEMANTIC tier until the background loop materializes it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ["ADK_CC_SKIP_DOTENV"] = "1"
os.environ["ADK_CC_API_KEY"] = "stub"  # deterministic consolidation → no model
os.environ["ADK_CC_MEMORY"] = "1"
for _k in ("ADK_CC_WIKI_STORE_URI", "ADK_CC_MEMORY_STORE_URI"):
    os.environ.pop(_k, None)

import requests

from adk_cc.memory import MemoryStore

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8776
BASE = f"http://127.0.0.1:{PORT}"
TENANT = "acme"
USER = "alice"


def main() -> int:
    mem_root = tempfile.mkdtemp(prefix="sched-mem-")
    data_dir = tempfile.mkdtemp(prefix="sched-art-")
    log_path = os.path.join(data_dir, "server.log")

    # seed two episodic captures on one topic (latest-wins → one semantic fact)
    seed = MemoryStore.for_tenant(TENANT, root=mem_root)
    seed.add_episodic(USER, "The team chose Postgres for storage.", topic="datastore")
    seed.add_episodic(USER, "The team chose Postgres 16 for storage.", topic="datastore")
    assert seed.list_semantic(USER) == [], "precondition: no semantic yet"

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_MEMORY": "1",
        "ADK_CC_MEMORY_ROOT": mem_root,
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{data_dir}",
        # scheduler: first pass ~1s after boot, then every 3s
        "ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S": "3",
        "ADK_CC_MEMORY_CONSOLIDATE_DELAY_S": "1",
    })
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    ok = False
    try:
        # wait for the server to be up
        for _ in range(60):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.5)

        # poll the SEMANTIC tier — it should appear once the loop's first pass runs
        sem = []
        for _ in range(30):  # up to ~15s
            sem = MemoryStore.for_tenant(TENANT, root=mem_root).list_semantic(USER)
            if sem:
                break
            time.sleep(0.5)

        ok = len(sem) >= 1
        print(f"  [{'PASS' if ok else 'FAIL'}] scheduler materialized semantic memory: "
              f"{[(i.topic, i.text) for i in sem]}")

        # corroborate via the server's own log line
        with open(log_path) as fh:
            blob = fh.read()
        started = "consolidation scheduler started" in blob
        ran = "memory consolidation:" in blob
        print(f"  [{'PASS' if started else 'FAIL'}] server logged scheduler start: {started}")
        print(f"  [{'PASS' if ran else 'INFO'}] server logged a consolidation pass: {ran}")
        ok = ok and started

        print("\nmemory scheduler e2e " + ("PASSED" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()
        for d in (mem_root, data_dir):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
