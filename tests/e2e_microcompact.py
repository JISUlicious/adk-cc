"""E2E: microcompaction evicts old large tool results before the model call.

Runs a bash-heavy multi-turn session twice (microcompact OFF then ON) and checks:
  1. ON logs "microcompact: evicted N tool result(s)" (authoritative — the
     per-request token estimators don't count function_response payloads, so the
     plugin's own log is the ground truth that it fired); OFF logs none.
  2. The model's reported prompt_token_count on the final turn (from session
     usage_metadata) is no larger with microcompact ON — best-effort, since it
     depends on the endpoint returning usage_metadata.
  3. The agent still answers the final turn with microcompact ON.

Live model required; skips without one. Run:
    .venv/bin/python tests/e2e_microcompact.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import adk_cc  # noqa: F401

os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIG_BASH = ("Run this bash command and report only the last line: "
            "`for i in $(seq 1 400); do echo \"line $i: the quick brown fox "
            "jumps over the lazy dog repeatedly\"; done`")
PROMPTS = [BIG_BASH, BIG_BASH, BIG_BASH,
           "How many bash commands have I asked you to run so far? One number."]


def _last_prompt_tokens(events) -> int:
    """Most recent usage_metadata.prompt_token_count across session events."""
    best = 0
    for e in events or []:
        um = e.get("usageMetadata") or e.get("usage_metadata") or {}
        n = um.get("promptTokenCount") or um.get("prompt_token_count")
        if isinstance(n, int):
            best = n  # last one wins (chronological)
    return best


def _run_session(port: int, micro: str) -> tuple[int, int, bool]:
    """Returns (evicted_log_count, final_prompt_tokens, final_answered)."""
    wks = tempfile.mkdtemp(prefix="mc-w-")
    art = tempfile.mkdtemp(prefix="mc-a-")
    log_path = os.path.join(art, "srv.log")
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_WORKSPACE_ROOT": wks,
        "ADK_CC_ARTIFACT_STORAGE_URI": f"file://{art}",
        "ADK_CC_MICROCOMPACT": micro,
        "ADK_CC_MICROCOMPACT_KEEP_RECENT": "1",
        "ADK_CC_MICROCOMPACT_MIN_TOKENS": "300",
        "ADK_CC_TOOL_TITLES": "0",
        # host-exec backend so run_bash actually produces output (the .env's
        # daytona backend is unreachable here). Workspace is a tempdir → allowed
        # by NoopBackend's path safety.
        "ADK_CC_SANDBOX_BACKEND": "noop",
    })
    for k in ("ADK_CC_COMPACTION_TOKEN_THRESHOLD", "ADK_CC_COMPACTION_INTERVAL",
              "ADK_CC_MAX_CONTEXT_TOKENS"):
        env.pop(k, None)  # isolate microcompaction (no summarizer / guard)
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    answered = False
    ptoks = 0
    try:
        for _ in range(60):
            try:
                if requests.get(base + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.5)
        sid = requests.post(f"{base}/apps/adk_cc/users/local/sessions", json={}, timeout=15).json()["id"]
        surl = f"{base}/apps/adk_cc/users/local/sessions/{sid}"
        for i, p in enumerate(PROMPTS, 1):
            try:
                r = requests.post(f"{base}/run", timeout=150, json={
                    "appName": "adk_cc", "userId": "local", "sessionId": sid,
                    "newMessage": {"role": "user", "parts": [{"text": p}]}})
                if i == len(PROMPTS) and r.ok:
                    answered = any(
                        (part.get("text") or "").strip()
                        for e in r.json() for part in (e.get("content") or {}).get("parts") or []
                    )
            except Exception as e:
                print(f"  [micro={micro}] turn {i} err: {type(e).__name__}")
            time.sleep(5)
        try:
            ptoks = _last_prompt_tokens(requests.get(surl, timeout=15).json().get("events", []))
        except Exception:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        log.close()

    evicted = 0
    try:
        with open(log_path) as fh:
            evicted = len(re.findall(r"microcompact: evicted (\d+) tool result", fh.read()))
    except Exception:
        pass
    shutil.rmtree(wks, ignore_errors=True)
    shutil.rmtree(art, ignore_errors=True)
    return evicted, ptoks, answered


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY — microcompact e2e skipped.")
        return 0

    print("== run OFF (ADK_CC_MICROCOMPACT=0) ==")
    off_ev, off_tok, off_ans = _run_session(8784, "0")
    print(f"  evicted-log-lines={off_ev}  final_prompt_tokens={off_tok}  answered={off_ans}")

    print("== run ON (ADK_CC_MICROCOMPACT=1) ==")
    on_ev, on_tok, on_ans = _run_session(8785, "1")
    print(f"  evicted-log-lines={on_ev}  final_prompt_tokens={on_tok}  answered={on_ans}")

    fired = on_ev > 0
    off_silent = off_ev == 0
    print(f"\n  [{'PASS' if fired else 'FAIL'}] microcompaction fired with ON "
          f"({on_ev} eviction events)")
    print(f"  [{'PASS' if off_silent else 'FAIL'}] no eviction with OFF ({off_ev})")
    if off_tok and on_tok:
        smaller = on_tok <= off_tok
        print(f"  [{'PASS' if smaller else 'WARN'}] model prompt tokens not larger ON: "
              f"{off_tok} → {on_tok} (Δ{on_tok - off_tok})")
    else:
        print("  [info] endpoint didn't report usage_metadata; skipping token delta")
    print(f"  [{'PASS' if on_ans else 'WARN'}] agent still answered with ON")

    ok = fired and off_silent
    print("\nmicrocompact e2e " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
