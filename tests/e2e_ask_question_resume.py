"""Answering `ask_user_question` must not end the conversation.

Reported from desktop dogfooding: the agent halts after the user answers a
clarifying question. Answering posts a NEW turn carrying the functionResponse,
ADK resumes the paused long-running tool — and if that resumed run produces no
model text, the broker has nothing to say and finishes `done`. The user is left
looking at their own answer.

The broker already handles one shape of this: a run ending on a dangling
`_handback_to_coordinator` gets an auto-continue so the coordinator replies.
This test asks whether the question-answer path needs the same treatment.

Reports, in order, so a failure is diagnosable rather than just red:
  1. did the first turn actually ask a question (precondition)
  2. did the answer turn produce model TEXT
  3. did the agent act on the answer at all (any tool call after it)

Run: ADK_CC_LIVE=1 .venv/bin/python tests/e2e_ask_question_resume.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8966
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


def _wait(turn_id: str, limit: int = 120) -> dict:
    for _ in range(limit):
        time.sleep(3)
        st = requests.get(f"{BASE}/api/turns/{turn_id}", timeout=30).json()
        if st["status"] != "running":
            return st
    return {"status": "timeout"}


def main() -> int:
    if os.environ.get("ADK_CC_LIVE") != "1":
        print("SKIP: needs live model turns (ADK_CC_LIVE=1)."); return 0
    endpoints = os.path.expanduser(
        "~/.adk-cc-desktop/admin-data/model-endpoints.json")
    if not os.path.isfile(endpoints):
        print("SKIP: no model endpoint registry to borrow."); return 0

    data = tempfile.mkdtemp(prefix="askq-")
    proj = os.path.join(data, "project")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    # Deliberately EMPTY. A build request against an empty repo produced
    # ask_user_question twice in observed live runs ("scaffold a new app
    # here?"), whereas two plausible files just got resolved by the model
    # itself — it picked one and moved on, so there was nothing to answer.

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
    try:
        for _ in range(120):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        def _new_session(n: int) -> str:
            """A fresh session per attempt. The first try parked on plan
            approval, and a follow-up prompt sent into a session that is
            awaiting a confirmation never runs at all — the retry looked like
            the model refusing to ask when it had simply never been asked."""
            sid = f"askq{n}"
            url = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            requests.post(url, json={}, timeout=30)
            requests.patch(url, json={"state_delta": {
                "model_endpoint": "chatgpt-codex", "model_id": MODEL}}, timeout=30)
            return sid

        sess = ""

        def _find_ask():
            evs = requests.get(sess, timeout=30).json()["events"]
            found = None
            for e in evs:
                for p in ((e.get("content") or {}).get("parts") or []):
                    fc = p.get("functionCall") or {}
                    if fc.get("name") == "ask_user_question":
                        found = fc
            return found

        # A trivial ambiguity on purpose. A build request pulled the agent into
        # plan mode and it parked on plan approval, never reaching the question.
        prompts = [
            "I can't decide how to format this project. Ask me a multiple-choice "
            "question about which style I want, and wait for my answer.",
            "Use your ask_user_question tool now to ask me: tabs or spaces?",
        ]
        ask = None
        for n, prompt in enumerate(prompts, start=1):
            sid = _new_session(n)
            sess = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
            t1 = requests.post(f"{BASE}/api/turns", timeout=60, json={
                "appName": "adk_cc", "userId": pid, "sessionId": sid,
                "newMessage": {"role": "user", "parts": [{"text": prompt}]}}).json()
            _wait(t1["turn_id"])
            ask = _find_ask()
            if ask is not None:
                break
            print("    no question yet — escalating to an explicit ask")
        check("the first turn asked a clarifying question (precondition)",
              ask is not None,
              "no ask_user_question call even when asked for one directly")
        if ask is None:
            print(f"    artifacts: {data}"); return 0

        # Answer exactly as the UI does: a new turn whose message is the
        # functionResponse for that call id.
        questions = (ask.get("args") or {}).get("questions") or []
        answer = {}
        for q in questions:
            opts = q.get("options") or []
            if opts:
                answer[q.get("question", "q")] = opts[0].get("label", "yes")
        print(f"    answering: {json.dumps(answer)[:120]}")

        t2 = requests.post(f"{BASE}/api/turns", timeout=60, json={
            "appName": "adk_cc", "userId": pid, "sessionId": sid,
            "newMessage": {"role": "user", "parts": [{
                "functionResponse": {"id": ask.get("id"),
                                     "name": "ask_user_question",
                                     "response": answer}}]}}).json()
        st2 = _wait(t2["turn_id"])
        print(f"    answer turn finished: {st2.get('status')}")

        after = requests.get(sess, timeout=30).json()["events"]
        # Everything after the function response is the agent's reaction to it.
        idx = 0
        for i, e in enumerate(after):
            for p in ((e.get("content") or {}).get("parts") or []):
                if (p.get("functionResponse") or {}).get("name") == "ask_user_question":
                    idx = i
        tail = after[idx + 1:]
        model_text = [
            " ".join(p["text"].split())
            for e in tail if (e.get("author") or "") != "user"
            for p in ((e.get("content") or {}).get("parts") or [])
            if p.get("text") and not p.get("thought")
        ]
        tool_calls = [
            (p.get("functionCall") or {}).get("name")
            for e in tail
            for p in ((e.get("content") or {}).get("parts") or [])
            if p.get("functionCall")
        ]
        print(f"    after the answer: {len(tool_calls)} tool call(s), "
              f"{len(model_text)} model message(s)")
        if model_text:
            print(f"    first reply: {model_text[0][:110]}")
        check("the agent replies after the answer", bool(model_text),
              "the turn ended with no model text — the user sees only their answer")
        check("the agent acts on the answer", bool(tool_calls) or bool(model_text),
              "nothing happened after the answer at all")
        with open(os.path.join(data, "tail.json"), "w") as fh:
            json.dump({"tool_calls": tool_calls, "model_text": model_text}, fh, indent=2)
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
