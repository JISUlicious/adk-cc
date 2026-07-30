"""Do the built-in skills actually get used?

A skill can be discovered, enabled, catalogued, and never once loaded — the
model has to choose it. That choice is what this measures: for each task it
reports which skills were loaded, whether a skill SCRIPT ran, and what the
answer said, then checks the skill the task was designed for was among them.

Two tasks, picked because they exercise different halves:
  * data-analyst — the flagship, with bundled scripts and references, on a CSV
    that has a deliberately obvious driver.
  * web-smoke-check — new, and the reason it exists is that verification used
    to hand-roll a DOM shim per run. Whether the verifier reaches for it now is
    the open question from that work.

Run: ADK_CC_LIVE=1 .venv/bin/python scripts/acceptance_skills.py
"""
from __future__ import annotations

import glob
import json
import os
import random
import shutil
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8962
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _csv(path: str) -> None:
    """300 rows where `pressure` drives `defect_rate` and the rest is noise —
    an analysis that does not find pressure has not done the work."""
    rnd = random.Random(7)
    rows = ["lot,pressure,temp,humidity,defect_rate"]
    for i in range(300):
        pressure = rnd.uniform(0.8, 2.4)
        temp = rnd.uniform(180, 220)
        humidity = rnd.uniform(30, 70)
        defect = 2.0 * pressure + rnd.gauss(0, 0.15)
        rows.append(f"L{i:04d},{pressure:.3f},{temp:.1f},{humidity:.1f},{defect:.3f}")
    open(path, "w").write("\n".join(rows) + "\n")


def _node_skill(proj: str) -> None:
    """A project skill whose only entrypoint is Node.

    Until the launcher grew other interpreters this could not be started at
    all — `run_skill_script` answered "Unsupported script type '.mjs'" — and
    the agent's fallback, `node scripts/…`, cannot work either because a
    skill's files are not in the workspace. So this task fails loudly if the
    fix regresses, whereas a Python skill would just keep passing.
    """
    d = os.path.join(proj, ".adk-cc", "skills", "release-notes")
    os.makedirs(os.path.join(d, "scripts"), exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as fh:
        fh.write(
            "---\nname: release-notes\ndescription: >\n"
            "  Formats a version bump into the house release-note format. Use\n"
            "  this whenever asked to write release notes; the wording and\n"
            "  ordering are fixed by scripts/render.mjs and must not be\n"
            "  hand-written.\n---\n\n"
            "Run `scripts/render.mjs` with the version and one or more change\n"
            "lines: `args=[\"1.4.0\", \"fixed login\", \"faster search\"]`.\n"
            "Use its output verbatim.\n")
    with open(os.path.join(d, "scripts", "render.mjs"), "w") as fh:
        fh.write(
            "import { banner } from './house_style.mjs';\n"
            "const [v, ...items] = process.argv.slice(2);\n"
            "console.log(banner(v));\n"
            "for (const it of items) console.log('  * ' + it);\n"
            "console.log('-- end of notes --');\n")
    with open(os.path.join(d, "scripts", "house_style.mjs"), "w") as fh:
        fh.write("export const banner = (v) => `### RELEASE ${v} ###`;\n")


_PUBLISHED = os.path.expanduser(
    "~/.claude/plugins/cache/anthropic-agent-skills/example-skills")


def _published_skills(proj: str) -> None:
    """Install skills written by someone else, exactly as they ship.

    Everything else here uses skills written for adk-cc, which only proves the
    loader agrees with its own authors. Against the published corpus four
    things broke silently — a skill dropped for an over-long description, a
    binary resource dropped by a UTF-8-only loader, a bare ModuleNotFoundError,
    and 1.1 MB materialised per invocation. Copied rather than referenced so a
    live run cannot write into the user's real skill cache.
    """
    src = sorted(p for p in glob.glob(_PUBLISHED + "/*/skills") if os.path.isdir(p))
    if not src:
        return
    dest = os.path.join(proj, ".adk-cc", "skills")
    os.makedirs(dest, exist_ok=True)
    for name in ("pdf", "docx", "web-artifacts-builder", "claude-api"):
        s = os.path.join(src[-1], name)
        if os.path.isdir(s) and not os.path.exists(os.path.join(dest, name)):
            shutil.copytree(s, os.path.join(dest, name))


TASKS = [
    {
        "tag": "data-analyst",
        "expect": "data-analyst",
        "prompt": ("data.csv has one factor that actually drives defect_rate and "
                   "two that do not. Tell me which one, and show me why you are "
                   "confident."),
    },
    {
        "tag": "web-smoke-check",
        "expect": "web-smoke-check",
        "prompt": ("Build a small web page with a button that counts clicks and "
                   "shows the total, then confirm for me that clicking it "
                   "actually updates what a user sees."),
    },
    {
        "tag": "node-skill",
        "expect": "release-notes",
        "setup": _node_skill,
        # No mention of the skill, the script or Node: choosing it is part of
        # what is being measured.
        "prompt": ("Write the release notes for version 1.4.0. It fixed login "
                   "and made search faster."),
        # The banner only comes from the sibling module, so its presence proves
        # the script ran with its neighbours materialised — not that the model
        # guessed a plausible format.
        "want_output": "### RELEASE 1.4.0 ###",
    },
    {
        "tag": "published-3p",
        # `claude-api` is the one the loader used to drop outright, so if the
        # description repair regresses this task cannot pass by luck: the skill
        # is not in the catalogue at all and the model cannot name it.
        "expect": "claude-api",
        "setup": _published_skills,
        "prompt": ("How should I use prompt caching with the Anthropic API, "
                   "and what are the pitfalls? Answer from the reference "
                   "material you have available rather than from memory."),
    },
]


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0

    data = tempfile.mkdtemp(prefix="skilluse-")
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
        cwd=REPO, env=env,
        stdout=open(os.path.join(data, "server.log"), "w"), stderr=subprocess.STDOUT)
    summary = {}
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        for task in TASKS:
            proj = os.path.join(data, task["tag"])
            os.makedirs(proj, exist_ok=True)
            subprocess.run(["git", "init", "-q", proj], capture_output=True)
            _csv(os.path.join(proj, "data.csv"))
            if task.get("setup"):
                task["setup"](proj)
            pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                                timeout=15).json()["project"]["id"]
            sid = f"s-{task['tag']}"
            sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            requests.post(sess, json={}, timeout=30)
            requests.patch(sess, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)

            print(f"\n=== {task['tag']} ===")
            t = requests.post(f"{BASE}/api/turns", timeout=60, json={
                "appName": "adk_cc", "userId": pid, "sessionId": sid,
                "newMessage": {"role": "user",
                               "parts": [{"text": task["prompt"]}]}}).json()
            for _ in range(200):
                time.sleep(3)
                st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
                if st["status"] != "running":
                    break

            events = requests.get(sess, timeout=30).json()["events"]
            loaded, scripts, calls = [], [], []
            for e in events:
                for p in ((e.get("content") or {}).get("parts") or []):
                    fc = p.get("functionCall")
                    if not fc:
                        continue
                    calls.append(fc.get("name"))
                    args = fc.get("args") or {}
                    if fc.get("name") in ("load_skill", "load_skill_resource",
                                          "search_skill_resource"):
                        loaded.append(args.get("skill_name") or "?")
                    if fc.get("name") == "run_skill_script":
                        scripts.append(
                            f"{args.get('skill_name')}::"
                            f"{args.get('file_path') or args.get('script_path')}")
                    if fc.get("name") == "run_bash":
                        cmd = str(args.get("command") or "")
                        if "smoke_page.mjs" in cmd:
                            scripts.append("web-smoke-check::smoke_page.mjs")
            answer = [
                " ".join(p["text"].split())
                for e in events if (e.get("author") or "") == "coordinator"
                for p in ((e.get("content") or {}).get("parts") or [])
                if p.get("text") and not p.get("thought")
            ]
            print(f"    turn: {st.get('status')}, {len(calls)} tool calls")
            print(f"    skills touched: {sorted(set(loaded)) or 'NONE'}")
            print(f"    skill scripts : {sorted(set(scripts)) or 'none'}")
            if answer:
                print(f"    answer: {answer[-1][:160]}")
            check(f"{task['tag']}: the intended skill was used",
                  task["expect"] in loaded or
                  any(task["expect"] in s for s in scripts),
                  f"skills touched: {sorted(set(loaded)) or 'none'}")
            if task.get("want_output"):
                # Loading a skill and reading its script is not running it; the
                # measured failure was exactly that — the right skill, the right
                # intent, and a hand-written result. Only output the script
                # alone can produce counts.
                blob = "\n".join(answer)
                for e in events:
                    for p in ((e.get("content") or {}).get("parts") or []):
                        fr = p.get("functionResponse")
                        if fr:
                            blob += "\n" + json.dumps(fr.get("response") or {})
                check(f"{task['tag']}: the script's own output was produced",
                      task["want_output"] in blob,
                      f"never saw {task['want_output']!r}")
            summary[task["tag"]] = {
                "status": st.get("status"), "calls": len(calls),
                "loaded": sorted(set(loaded)), "scripts": sorted(set(scripts)),
                "answer": answer[-1] if answer else "",
            }
        with open(os.path.join(data, "summary.json"), "w") as fh:
            json.dump(summary, fh, indent=2)
        print(f"\n    artifacts: {data}")
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
