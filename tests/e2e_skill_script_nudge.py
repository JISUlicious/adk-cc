"""Does the skill-script nudge actually change what the model DOES?

Two instructions now aim at the same mistake:
  1. proactive — shipped with the skill catalogue, before the first attempt
     (_SKILL_SCRIPT_INSTRUCTION), and
  2. reactive — attached to a failed run_bash (_skill_script_hint).

Asserting that they exist is not evidence. The reported failure was
behavioural: the model ran a skill's script with run_bash from guessed
directories, and because each guess was a different command string, "allow
always" never matched and it re-prompted every time.

So this MEASURES it, over several independent sessions, with a prompt worded
the way a user would word it — naming the script by its relative path, which
is exactly what invites `run_bash python scripts/…`. It reports the rate
rather than asserting a single run, because one sample of a model is noise.

MEASURED 2026-08-04, and the result is NEGATIVE — read before trusting it:

    with the nudge     4/4 run_skill_script, 0 run_bash
    nudge removed      4/4 run_skill_script, 0 run_bash

So this scenario does not reproduce the reported failure at all, and the
nudge's benefit here is exactly zero. What it does prove is that the happy
path works (the script always ran, and the confirmation form was raised every
time). It is kept as a REGRESSION guard, not as evidence for the nudge.

To actually measure the nudge, this has to first reproduce the failure: the
live case involved a real skill whose README documents the bare
`python scripts/<name>.py` form, inside a longer session, with the model
guessing directories across several attempts. Until a control run shows
run_bash without the nudge, treat the proactive instruction as unproven — the
reactive hint (_skill_script_hint) is the mechanism with evidence behind it.

Passing bar: the model reaches the script through run_skill_script in the
clear majority of sessions, and never ends up unable to run it.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_skill_script_nudge.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8977
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
ROUNDS = int(os.environ.get("NUDGE_ROUNDS", "4"))
MARKER = "GREETINGS-FROM-THE-SKILL"


def _calls(events) -> list[str]:
    """Every function-call name the turn made, in order."""
    out = []
    for e in events:
        for part in ((e.get("content") or {}).get("parts") or []):
            fc = part.get("functionCall")
            if fc and fc.get("name"):
                out.append(fc["name"])
    return out


def main() -> int:  # noqa: PLR0915
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0

    data = tempfile.mkdtemp(prefix="nudge-data-")
    proj = tempfile.mkdtemp(prefix="nudge-proj-")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=False)

    # A project skill with a script that prints a marker. Its README documents
    # the bare `python scripts/greet.py` form on purpose — that is what the
    # real data-analyst skill does, and what led the model astray live.
    sd = os.path.join(proj, ".adk-cc", "skills", "greeter")
    os.makedirs(os.path.join(sd, "scripts"))
    with open(os.path.join(sd, "SKILL.md"), "w") as fh:
        fh.write(textwrap.dedent("""\
            ---
            name: greeter
            description: >
              Greets by running its own script. Use for any greeting request.
            ---

            To greet, run `python scripts/greet.py <name>` and report its output.
            """))
    with open(os.path.join(sd, "scripts", "greet.py"), "w") as fh:
        fh.write(f'import sys\nprint("{MARKER}", sys.argv[1] if len(sys.argv)>1 else "")\n')

    env = dict(os.environ)
    env.pop("ADK_CC_API_KEY", None)
    env.update({
        "ADK_CC_MODEL_REGISTRY_FILE": os.path.expanduser(
            "~/.adk-cc-desktop/admin-data/model-endpoints.json"),
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SKIP_DOTENV": "1", "ADK_CC_SKIP_CONFIG_CHECK": "1",
    })
    log = os.path.join(data, "server.log")
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)

    used_tool = bash_first = never_ran = told = 0
    try:
        for _ in range(160):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        # A project's skills are withheld until the folder is trusted.
        requests.post(f"{BASE}/desktop/settings/skills/trust",
                      json={"root": proj, "trusted": True}, timeout=15)

        for i in range(ROUNDS):
            sid = f"nudge-{i}"
            sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            requests.post(sess, json={}, timeout=30)
            requests.patch(sess, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL,
                "permission_mode": "bypassPermissions"}}, timeout=30)
            t = requests.post(f"{BASE}/api/turns", timeout=60, json={
                "appName": "adk_cc", "userId": pid, "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{"text":
                    "Use the greeter skill: run its scripts/greet.py with the "
                    "name Ada and tell me exactly what it printed."}]}}).json()
            for _ in range(100):
                time.sleep(3)
                st = requests.get(f"{BASE}/api/turns/{t['turn_id']}",
                                  timeout=30).json()
                if st["status"] != "running":
                    break
            events = requests.get(sess, timeout=30).json().get("events") or []
            names = _calls(events)
            blob = json.dumps(events)
            # #113 part 1: load_skill must TELL the agent where the skill is,
            # instead of leaving it to guess. Asserted against the real
            # project dir, in the running desktop server.
            told_dir = sd in blob and "base_dir" in blob
            if told_dir:
                told += 1
            got_output = MARKER in blob
            first_script_call = next(
                (n for n in names if n in ("run_skill_script", "run_bash")), None)
            if first_script_call == "run_skill_script":
                used_tool += 1
            elif first_script_call == "run_bash":
                bash_first += 1
            if not got_output:
                never_ran += 1
            print(f"  round {i+1}: first={first_script_call} "
                  f"calls={names[:6]} marker={'yes' if got_output else 'NO'} "
                  f"base_dir={'told' if told_dir else 'MISSING'}")

        print(f"\n  run_skill_script first : {used_tool}/{ROUNDS}")
        print(f"  run_bash first         : {bash_first}/{ROUNDS}")
        print(f"  never got the output   : {never_ran}/{ROUNDS}")
        print(f"  told the skill's dir   : {told}/{ROUNDS}")

        ok = used_tool > ROUNDS / 2 and never_ran == 0 and told == ROUNDS
        print(f"\n  [{'PASS' if ok else 'FAIL'}] the script always runs AND "
              f"every load_skill named the skill's real directory")
        if not ok:
            print(f"  server log: {log}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
