"""#113 verification: the always-runnable guarantee under ADVERSARIAL prompting.

e2e_skill_script_nudge.py measured the polite scenario and could not
reproduce the reported failure (4/4 clean with AND without the nudge). This
is the scenario the task said to build before believing anything:

  - the skill's own docs (SKILL.md + scripts/README.md) document the bare
    `python scripts/audit.py <csv>` form, the way the real data-analyst
    skill does — and the USER quotes that form verbatim in the prompt;
  - the script takes a WORKSPACE-relative data file, so running it from a
    guessed directory (cd into the skill folder) breaks on the data path —
    the client's shrinkage.json failure shape;
  - the ask lands mid-session, after an unrelated first turn.

The bar is the GUARANTEE, not the route: in every session the script's
marker output must be produced by actually running it (the marker hashes the
data file, so a re-implementation cannot fake it), and no session may end
with the model unable to run it. The route taken is logged per round —
run_skill_script directly, or run_bash first and recovered via the reactive
hint — because that distribution is the evidence #113 asks for.

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_skill_script_adversarial.py
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
PORT = 8978
BASE = f"http://127.0.0.1:{PORT}"
MODEL = "chatgpt-codex/gpt-5.4-mini"
ROUNDS = int(os.environ.get("ADV_ROUNDS", "4"))

CSV = "month,units\n2026-01,120\n2026-02,145\n2026-03,98\n"
# The marker embeds a digest of the csv the script actually read — an answer
# carrying it proves the script ran against the real workspace file.
AUDIT = """\
import hashlib, sys
p = sys.argv[1]
h = hashlib.sha256(open(p, "rb").read()).hexdigest()[:10]
rows = sum(1 for _ in open(p)) - 1
print(f"AUDIT-MARKER-{h} rows={rows} file={p}")
"""


def _calls(events) -> list[str]:
    out = []
    for e in events:
        for part in ((e.get("content") or {}).get("parts") or []):
            fc = part.get("functionCall")
            if fc and fc.get("name"):
                out.append(fc["name"])
    return out


def _bash_script_attempts(events) -> list[str]:
    """run_bash commands that reference the skill script, any shape."""
    out = []
    for e in events:
        for part in ((e.get("content") or {}).get("parts") or []):
            fc = part.get("functionCall")
            if fc and fc.get("name") == "run_bash":
                cmd = str((fc.get("args") or {}).get("command") or "")
                if "audit.py" in cmd:
                    out.append(cmd)
    return out


def main() -> int:  # noqa: PLR0915
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0

    import hashlib
    expect_marker = "AUDIT-MARKER-" + hashlib.sha256(
        CSV.encode()).hexdigest()[:10]

    data = tempfile.mkdtemp(prefix="adv-data-")
    proj = tempfile.mkdtemp(prefix="adv-proj-")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=False)
    os.makedirs(os.path.join(proj, "data"))
    with open(os.path.join(proj, "data", "sales.csv"), "w") as fh:
        fh.write(CSV)

    sd = os.path.join(proj, ".adk-cc", "skills", "mlcc-audit")
    os.makedirs(os.path.join(sd, "scripts"))
    with open(os.path.join(sd, "SKILL.md"), "w") as fh:
        fh.write(textwrap.dedent("""\
            ---
            name: mlcc-audit
            description: >
              Audits a CSV dataset with the vetted audit script. Use for any
              data-audit request.
            ---

            Audit a dataset by running:

                python scripts/audit.py <path-to-csv>

            and reporting its output verbatim. See scripts/README.md.
            """))
    with open(os.path.join(sd, "scripts", "README.md"), "w") as fh:
        fh.write("# audit\n\nUsage:\n\n    python scripts/audit.py data.csv\n")
    with open(os.path.join(sd, "scripts", "audit.py"), "w") as fh:
        fh.write(AUDIT)

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

    tool_first = bash_first = recovered = never_ran = 0
    try:
        for _ in range(160):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        requests.post(f"{BASE}/desktop/settings/skills/trust",
                      json={"root": proj, "trusted": True}, timeout=15)

        def _wait(turn_id: str) -> None:
            for _ in range(120):
                time.sleep(3)
                st = requests.get(f"{BASE}/api/turns/{turn_id}",
                                  timeout=30).json()
                if st["status"] != "running":
                    return

        def _answer_cards(sid: str, sess: str, answered: set) -> bool:
            """Approve pending confirmation cards the way the UI does
            (#114 gates scripts even under bypassPermissions). Returns True
            when something was answered."""
            events = requests.get(sess, timeout=30).json().get("events") or []
            wraps = []
            for e in events:
                for p in (e.get("content") or {}).get("parts") or []:
                    fc = p.get("functionCall")
                    if (fc and fc.get("id") not in answered
                            and fc.get("name") in ("adk_cc_confirmation_form",
                                                   "adk_request_confirmation")):
                        wraps.append(fc["id"])
            for wid in wraps:
                answered.add(wid)
                t2 = requests.post(f"{BASE}/api/turns", timeout=120, json={
                    "appName": "adk_cc", "userId": pid, "sessionId": sid,
                    "newMessage": {"role": "user", "parts": [{
                        "functionResponse": {
                            "id": wid, "name": "adk_cc_confirmation_form",
                            "response": {"chose_id": "allow_once"}}}]}}).json()
                _wait(t2["turn_id"])
            return bool(wraps)

        def turn(sid: str, sess: str, text: str, answered: set) -> None:
            t = requests.post(f"{BASE}/api/turns", timeout=60, json={
                "appName": "adk_cc", "userId": pid, "sessionId": sid,
                "newMessage": {"role": "user",
                               "parts": [{"text": text}]}}).json()
            _wait(t["turn_id"])
            # A gated call pauses the turn; approving may surface another
            # card (e.g. a bash fallback) — answer until quiet, bounded.
            for _ in range(4):
                if not _answer_cards(sid, sess, answered):
                    return

        for i in range(ROUNDS):
            sid = f"adv-{i}"
            sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            requests.post(sess, json={}, timeout=30)
            requests.patch(sess, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL,
                "permission_mode": "bypassPermissions"}}, timeout=30)
            answered: set = set()
            # Unrelated first turn: the ask must land mid-session.
            turn(sid, sess,
                 "What data files does this project contain? Just list them.",
                 answered)
            # The adversarial ask: quotes the skill docs' bare form verbatim.
            turn(sid, sess,
                 "Now audit data/sales.csv with the mlcc-audit skill. "
                 "Its docs say to run `python scripts/audit.py "
                 "data/sales.csv` — do exactly that and paste the "
                 "output line.", answered)

            events = requests.get(sess, timeout=30).json().get("events") or []
            names = _calls(events)
            blob = json.dumps(events)
            bash_tries = _bash_script_attempts(events)
            ran = expect_marker in blob
            used_tool = "run_skill_script" in names
            if not ran:
                never_ran += 1
            if bash_tries and used_tool and ran:
                recovered += 1
            elif used_tool and not bash_tries:
                tool_first += 1
            elif bash_tries:
                bash_first += 1
            print(f"  round {i+1}: ran={'yes' if ran else 'NO'} "
                  f"tool={'yes' if used_tool else 'no'} "
                  f"bash_attempts={len(bash_tries)} "
                  f"{('first: ' + bash_tries[0][:70]) if bash_tries else ''}")

        print(f"\n  clean run_skill_script      : {tool_first}/{ROUNDS}")
        print(f"  bash first, then recovered  : {recovered}/{ROUNDS}")
        print(f"  bash only (no tool)         : {bash_first}/{ROUNDS}")
        print(f"  NEVER produced real output  : {never_ran}/{ROUNDS}")

        ok = never_ran == 0
        print(f"\n  [{'PASS' if ok else 'FAIL'}] the vetted script's real "
              f"output was produced in every adversarial session")
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
