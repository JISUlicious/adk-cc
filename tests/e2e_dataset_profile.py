"""E2E: profile a real dataset through the real analysis runtime (W6.2).

Deliberately NOT mocked. The profiler runs inside the uv-managed env the agent
uses, so the only test that means anything runs it there too — a mocked pandas
would prove nothing about whether the script parses a real CSV or reads parquet
metadata without scanning.

First run provisions the `core` tier (~1 min); afterwards it is cached per
project. Skips cleanly when uv/the runtime is unavailable.

Run: .venv/bin/python tests/e2e_dataset_profile.py
"""
from __future__ import annotations

import json, os, subprocess, sys, tempfile, time
import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8943
BASE = f"http://127.0.0.1:{PORT}"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    globals()['_passed' if ok else '_failed'] = (_passed + 1) if ok else (_failed + 1)


def main() -> int:
    data = tempfile.mkdtemp(prefix="prof-")
    proj = os.path.join(data, "project")
    os.makedirs(os.path.join(proj, "data"), exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    # A CSV with a known shape and a deliberate null.
    rows = ["region,revenue,orders,note"]
    for i in range(1, 501):
        note = "" if i % 10 == 0 else f"n{i}"          # 50 nulls in `note`
        rows.append(f"{'north' if i % 2 else 'south'},{100 + i},{i % 7},{note}")
    open(os.path.join(proj, "data", "sales.csv"), "w").write("\n".join(rows) + "\n")

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_API_KEY": "stub",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=10).json()["project"]["id"]
        q = f"?project_id={pid}&session_id=s1"

        t0 = time.time()
        r = requests.get(f"{BASE}/desktop/datasets/sales.csv/profile{q}", timeout=400)
        if r.status_code == 503:
            print(f"SKIP: analysis runtime unavailable — {r.text[:160]}")
            return 0
        check("profile returns 200", r.ok, f"{r.status_code} {r.text[:200]}")
        if not r.ok:
            return 1
        prof = r.json()["profile"]
        print(f"    profiled in {time.time() - t0:.1f}s: {json.dumps(prof)[:220]}")

        check("row count is exact (counted, not sampled)",
              prof.get("rows") == 500 and prof.get("rows_exact") is True, prof.get("rows"))
        cols = {c["name"]: c for c in prof["columns"]}
        check("all columns are reported", set(cols) == {"region", "revenue", "orders", "note"},
              list(cols))
        check("dtypes come from pandas, not guessed",
              cols["revenue"]["dtype"].startswith("int"), cols["revenue"]["dtype"])
        check("nulls are counted in the sample", cols["note"]["nulls"] == 50,
              cols["note"]["nulls"])
        check("head has real rows", len(prof["head"]["rows"]) == 8
              and prof["head"]["rows"][0][0] in ("north", "south"), prof["head"]["rows"][:1])

        t1 = time.time()
        r2 = requests.get(f"{BASE}/desktop/datasets/sales.csv/profile{q}", timeout=60)
        check("second call is served from cache", r2.json().get("cached") is True
              and (time.time() - t1) < 2.0, r2.json().get("cached"))

        # unknown + unsafe names
        check("unknown dataset is 404",
              requests.get(f"{BASE}/desktop/datasets/nope.csv/profile{q}", timeout=30)
              .status_code == 404)
        check("unsupported name is rejected",
              requests.get(f"{BASE}/desktop/datasets/evil.py/profile{q}", timeout=30)
              .status_code in (400, 404))
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
