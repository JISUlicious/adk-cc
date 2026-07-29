"""Live UI acceptance: one goal-shaped ask, driven entirely through the app.

Give it `--prompt` (three sentences, goal only) and it drives a whole build
through the real UI: types the ask, answers every human-in-the-loop gate on a
policy, and reports what the agent actually produced. `--port` lets several
genres run at once.

The harness types a THREE-SENTENCE, goal-only prompt into the real composer —
no tool names, no commands, nothing the agent could follow mechanically — and
then behaves like an attentive operator: it watches the thread, answers each
permission prompt on a policy, and records what the agent actually did.

Why through the UI rather than /api/turns: a turn started over the API runs
server-side but never renders, so nothing about the product's actual surface is
exercised (see tests/e2e_plan_mode_timing_ui.py, where that mistake produced a
green run against an empty pane).

Confirmation policy (the operator half of the loop, not a rubber stamp):
  * allow  — anything whose command matches none of the deny patterns
  * deny   — networked pipes to a shell, sudo, force-push, package publish,
             recursive deletes outside the project, writes to $HOME dotfiles
Every prompt is logged with the decision, so the transcript shows what was
authorised rather than implying nothing was asked.

`Approve` (the plan gate) counts as an answerable prompt. Leaving it out cost a
whole run: the agent planned, `write_plan` returned awaiting_user_confirmation,
and the harness — seeing no button it recognised and no stream — declared the
turn finished with zero files written.

The session is pinned to acceptEdits. The process default is bypassPermissions
(agent.py, deliberately, for the dev experience) and the UI does not set a mode
on new sessions, so an unpinned run authorises everything silently and this
policy would never execute.

Run:
  ADK_CC_LIVE=1 .venv/bin/python scripts/acceptance_mobile_shell.py [--minutes N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = "chatgpt-codex/gpt-5.4-mini"          # smallest on the subscription

# Default ask: three sentences, states the want and the qualities, names no
# tool, no command, no file, no language. If the agent picks a stack, that is
# the agent's decision and part of what is under test.
DEFAULT_PROMPT = (
    "I want a mobile app that gives me a real Linux shell on my phone. "
    "It should handle the everyday commands I actually use — listing and "
    "reading files, moving around directories, editing something in place. "
    "Build it in this project and tell me how to try it."
)

DENY_PATTERNS = [
    r"\bsudo\b", r"curl[^|]*\|\s*(ba)?sh", r"wget[^|]*\|\s*(ba)?sh",
    r"\bgit\s+push\b.*--force", r"\bnpm\s+publish\b", r"\brm\s+-rf\s+[~/]",
    r">\s*~/\.\w+", r"\bshutdown\b", r"\bdiskutil\b",
]


def _command_of(card_text: str) -> str:
    """The command only — never the card's rationale.

    The rationale enumerates the very patterns being screened for
    ("dangerous command requires confirmation (rm -rf, sudo, curl|sh, …)"), so
    matching against the whole card denies every dangerous-command prompt on
    the strength of the explanation. That cost a run three denials of a plain
    `python3 -m http.server`."""
    first = (card_text.splitlines() or [""])[0]
    body = first.split(":", 1)[1] if ":" in first else first
    return body.strip().rstrip("?").strip()


def _risky(card_text: str) -> str | None:
    command = _command_of(card_text)
    for pat in DENY_PATTERNS:
        if re.search(pat, command, re.I):
            return pat
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=45.0)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--port", type=int, default=8973)
    args = ap.parse_args()
    global BASE
    PORT = args.port
    BASE = f"http://127.0.0.1:{PORT}"

    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: desktop UI not built (npm --prefix web run build:desktop)."); return 0
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs a live model turn (ADK_CC_LIVE=1)."); return 0
    from playwright.sync_api import sync_playwright

    data = args.workspace or os.path.join(REPO, ".acceptance", "mobile-shell")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    shots = os.path.join(data, "shots")
    os.makedirs(shots, exist_ok=True)
    if not os.path.isdir(os.path.join(proj, ".git")):
        subprocess.run(["git", "init", "-q", proj], capture_output=True)
    print(f"workspace: {proj}")

    env = dict(os.environ)
    for k in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK"):
        env.pop(k, None)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist, "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "s.db"),
    })
    log = open(os.path.join(data, "run.log"), "a", buffering=1)

    def say(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")

    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(data, "server.log"), "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1400, "height": 1000})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            page.locator(".adk-project-row").first.click(timeout=15000)
            page.wait_for_timeout(2000)
            rows = page.locator(".adk-session-title")
            if rows.count():
                rows.first.click(timeout=6000)
            page.wait_for_timeout(1500)

            listed = requests.get(f"{BASE}/apps/adk_cc/users/{pid}/sessions",
                                  timeout=30).json()
            sid = sorted(listed, key=lambda s: s.get("lastUpdateTime", 0))[-1]["id"]
            sess_url = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            requests.patch(sess_url, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL,
                "permission_mode": "acceptEdits"}}, timeout=30)
            say(f"session {sid} on {MODEL}")

            box = page.locator(".adk-composer-input")
            stop = page.locator('button[title="Stop the streaming response"]')
            box.click()
            box.fill(args.prompt)
            page.keyboard.press("Enter")
            say("prompt sent (3 sentences, goal only)")

            decisions: list[dict] = []
            deadline = time.time() + args.minutes * 60
            idle = 0
            shot_n = 0
            while time.time() < deadline:
                page.wait_for_timeout(2000)

                # A clarifying question is its own widget, and it stalls a run
                # just as effectively as an unanswered permission prompt: the
                # first re-run died here, with the agent waiting on a choice it
                # had already recommended. Answer with each question's FIRST
                # option — the agent orders its own recommendation first, so
                # this decides without smuggling in new instructions.
                submit = page.get_by_role("button", name="Submit answers")
                if submit.count():
                    card = submit.first.locator(
                        "xpath=ancestor::div[contains(@class,'bg-brand-tint')][1]")
                    picks = []
                    groups = card.locator("div.space-y-2")
                    for gi in range(groups.count()):
                        opt = groups.nth(gi).locator("button").first
                        if opt.count():
                            picks.append(" ".join(opt.inner_text().split())[:60])
                            opt.click()
                            page.wait_for_timeout(200)
                    submit.first.click()
                    decisions.append({"kind": "question", "picked": picks})
                    say(f"answered question card: {picks}")
                    idle = 0
                    page.wait_for_timeout(1500)
                    continue

                # An "ask" is on screen when a confirmation card's buttons are.
                allow = page.get_by_role("button", name=re.compile(
                    r"^(Allow once|Allow|Allow this folder|Approve|Yes)$"))
                if allow.count():
                    # Read the card that owns this button — `.bg-brand-tint` alone
                    # also matches the composer's plan-mode box.
                    detail = ""
                    try:
                        detail = allow.first.locator(
                            "xpath=ancestor::div[contains(@class,'bg-brand-tint')][1]"
                        ).inner_text()[:400]
                    except Exception:
                        pass
                    bad = _risky(detail)
                    if bad:
                        deny = page.get_by_role("button", name="Deny")
                        if deny.count():
                            deny.first.click()
                        decisions.append({"detail": detail, "decision": "deny",
                                          "matched": bad})
                        say(f"DENY (matched {bad}): {detail.splitlines()[0][:120]}")
                    else:
                        allow.first.click()
                        decisions.append({"detail": detail, "decision": "allow"})
                        say(f"allow: {detail.splitlines()[0][:120] if detail else '(no detail)'}")
                    idle = 0
                    page.wait_for_timeout(1500)
                    continue

                if stop.count():
                    idle = 0
                    shot_n += 1
                    if shot_n % 30 == 0:
                        page.screenshot(path=os.path.join(shots, f"t{shot_n:04d}.png"),
                                        full_page=False)
                        say("…still working")
                else:
                    idle += 1
                    if idle >= 20:         # ~40s quiet and nothing pending
                        break

            done = not stop.count()
            say(f"turn {'finished' if done else 'STILL RUNNING at cutoff'}")
            page.screenshot(path=os.path.join(shots, "final.png"), full_page=True)
            b.close()

        # ---- what actually happened -------------------------------------
        sess = requests.get(sess_url, timeout=30).json()
        calls: list[str] = []
        for e in sess["events"]:
            for pt in ((e.get("content") or {}).get("parts") or []):
                fc = pt.get("functionCall")
                if fc:
                    calls.append(fc.get("name", "?"))
        text = "\n".join(
            pt["text"] for e in sess["events"]
            for pt in ((e.get("content") or {}).get("parts") or [])
            if pt.get("text") and (e.get("content") or {}).get("role") == "model")
        summary = {
            "model": MODEL, "prompt": args.prompt, "finished": done,
            "tool_calls": len(calls),
            "tools_used": sorted(set(calls)),
            "confirmations": decisions,
            "final_text_tail": text[-2000:],
        }
        with open(os.path.join(data, "summary.json"), "w") as fh:
            json.dump(summary, fh, indent=2)
        say(f"{len(calls)} tool calls, {len(decisions)} confirmations")
        say("files now in the project:")
        for root, dirs, files in os.walk(proj):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".adk-cc")]
            for f in sorted(files):
                p = os.path.join(root, f)
                say(f"    {os.path.relpath(p, proj)}  ({os.path.getsize(p)}b)")
        say(f"summary: {os.path.join(data, 'summary.json')}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
