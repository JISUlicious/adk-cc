"""A live session loads ITS OWN project's skill.

The unit tests pin the resolver and the toolset accessors. Neither can show that
`_root_of(tool_context)` returns the project root in a real turn — that runs
through the workspace resolver, the session state and the tenant context, and it
is the one link in the chain a pure function cannot reach.

So this asks the agent to list its skills, in two projects, inside ONE server
process — exactly the case a single shared toolset used to get wrong. The server
is deliberately started from a directory that has its own skill, so a
cwd-anchored walk-up would offer `cwd-ghost` to both.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_project_skills.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8963
BASE = f"http://127.0.0.1:{PORT}"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _make_skill(root: Path, name: str) -> None:
    # `.adk-cc/skills` is the project scope; `.claude/skills` is no longer read.
    d = root / ".adk-cc" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >\n  Project-local test skill {name}.\n"
        f"---\n\nUse {name} for the {name} job.\n", encoding="utf-8")


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs a live model turn (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0

    data = tempfile.mkdtemp(prefix="projskills-")
    # The server's run dir has its own skill. That is GLOBAL scope now, so it
    # SHOULD reach both projects — what must not happen is a project's own skill
    # going missing, or one project seeing another's.
    server_cwd = Path(data) / "launch-dir"
    server_cwd.mkdir(parents=True)
    _make_skill(server_cwd, "cwd-ghost")

    projects = {}
    for tag, skill in (("alpha", "alpha-only"), ("beta", "beta-only")):
        p = Path(data) / tag
        p.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(p)], capture_output=True)
        _make_skill(p, skill)
        projects[tag] = (p, skill)

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
        "ADK_CC_MODEL_REGISTRY_FILE": endpoints,
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(server_cwd),          # <- the launch dir, not the repo
        env=env, stdout=open(os.path.join(data, "server.log"), "w"),
        stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        seen = {}
        for tag, (path, skill) in projects.items():
            pid = requests.post(BASE + "/desktop/projects",
                                json={"path": str(path)}, timeout=15
                                ).json()["project"]["id"]
            sid = f"s-{tag}"
            sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            requests.post(sess, json={}, timeout=30)
            requests.patch(sess, json={"state_delta": {
                "model_endpoint": "chatgpt-codex",
                "model_id": "chatgpt-codex/gpt-5.4-mini"}}, timeout=30)
            t = requests.post(f"{BASE}/api/turns", timeout=60, json={
                "appName": "adk_cc", "userId": pid, "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{"text":
                    "List your available skills by name using your skills tool, "
                    "then tell me which ones look project-specific."}]}}).json()
            for _ in range(100):
                time.sleep(3)
                st = requests.get(f"{BASE}/api/turns/{t['turn_id']}",
                                  timeout=30).json()
                if st["status"] != "running":
                    break
            events = requests.get(sess, timeout=30).json()["events"]
            # Everything the turn produced: the list_skills RESPONSE carries the
            # catalogue the session actually had.
            seen[tag] = json.dumps(events)

        for tag, (path, skill) in projects.items():
            body = seen.get(tag) or ""
            other = "beta-only" if skill == "alpha-only" else "alpha-only"
            check(f"{tag} is offered its own skill ({skill})", skill in body,
                  f"{skill} never appeared in {tag}'s turn")
            check(f"{tag} is not offered the other project's skill",
                  other not in body, f"{other} leaked into {tag}")
            check(f"{tag} also gets the global (run-dir) skill",
                  "cwd-ghost" in body,
                  "the run dir's skill is global and should reach every project")
        with open(os.path.join(data, "seen.json"), "w") as fh:
            json.dump({k: len(v or "") for k, v in seen.items()}, fh, indent=2)
        print(f"    artifacts: {data}")
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
