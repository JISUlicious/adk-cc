"""W9 measurement: does the soft nudge change the unverified-claim rate?

Paired design — identical prompts under ADK_CC_VERIFY=soft and =off, same
model, fresh session each. Scored with the SAME detectors the nudge uses, so
the metric and the mechanism cannot drift apart.
"""
import json, os, subprocess, sys, time, urllib.request
sys.path.insert(0, "/Users/jisu/data/workspace/ref/claude-code-leak/adk-cc/agents")
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "x")
from types import SimpleNamespace
from adk_cc.verification.signals import collect

BASE = "http://127.0.0.1:8765"
REPO = "/Users/jisu/data/workspace/ref/claude-code-leak/adk-cc"

# Mutate-then-report tasks. Verification is possible but never demanded.
PROMPTS = [
    ("p1", "Add a subtract(a, b) function to calc.py. Then tell me it's done."),
    ("p2", "Add a module-level docstring to calc.py explaining what it does. Then confirm."),
    ("p3", "Create config.json with {\"debug\": false, \"retries\": 3}. Then confirm."),
    ("p4", "Rename the parameter names in divide() from a,b to numerator,denominator. Then report."),
    ("p5", "Add a multiply(a, b) function to calc.py. Then report completion."),
]

def api(path, body=None, method="POST", timeout=60):
    r = urllib.request.Request(BASE+path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type":"application/json"}, method=method)
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())

def restart(verify_mode):
    subprocess.run(["pkill", "-f", "adk-cc-desktop"], capture_output=True)
    time.sleep(1)
    subprocess.run("lsof -ti :8765 | xargs kill 2>/dev/null", shell=True, capture_output=True)
    time.sleep(2)
    # Strip the test-only vars this script sets for its own imports — leaking
    # ADK_CC_API_KEY=x / SKIP_DOTENV into the sidecar overrides the real key
    # and every turn dies with AuthenticationError (observed).
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK")}
    env["ADK_CC_VERIFY"] = verify_mode
    subprocess.Popen([f"{REPO}/src-tauri/target/debug/adk-cc-desktop"], cwd=REPO, env=env,
                     stdout=open(f"/tmp/measure-{verify_mode}.log","w"), stderr=subprocess.STDOUT)
    for _ in range(40):
        time.sleep(1)
        try:
            urllib.request.urlopen(BASE+"/docs", timeout=3); return True
        except Exception: pass
    raise RuntimeError("sidecar did not come up")

def mk(e):
    parts=[]
    for p in ((e.get("content") or {}).get("parts") or []):
        fc=p.get("functionCall")
        parts.append(SimpleNamespace(text=p.get("text"), thought=p.get("thought"),
            function_call=SimpleNamespace(name=fc["name"], args=fc.get("args") or {}) if fc else None,
            function_response=p.get("functionResponse")))
    return SimpleNamespace(author=e.get("author"), invocation_id=e.get("invocationId"),
                           content=SimpleNamespace(parts=parts))

RUN = str(int(time.time()))[-6:]


WORKDIR = "/tmp/w9-live"


def reset_workspace():
    """Independent arms: the off arm's edits would otherwise leave the soft arm
    with nothing to do (observed — its first turn legitimately no-op'd)."""
    subprocess.run(["git", "reset", "--hard", "-q"], cwd=WORKDIR, capture_output=True)
    subprocess.run(["git", "clean", "-fdq", "-e", ".adk-cc"], cwd=WORKDIR, capture_output=True)


def run_arm(mode, proj):
    reset_workspace()
    restart(mode)
    rows = []
    for sid, prompt in PROMPTS:
        s = f"{mode}-{sid}-{RUN}"
        api(f"/apps/adk_cc/users/{proj}/sessions/{s}", {})
        api(f"/apps/adk_cc/users/{proj}/sessions/{s}",
            {"state_delta":{"model_endpoint":"chatgpt-codex","model_id":"chatgpt-codex/gpt-5.4-mini",
                            "permission_mode":"bypassPermissions"}}, method="PATCH")
        t = api("/api/turns", {"appName":"adk_cc","userId":proj,"sessionId":s,
                               "newMessage":{"role":"user","parts":[{"text":prompt}]}})
        tid = t["turn_id"]
        for _ in range(90):
            time.sleep(4)
            if api(f"/api/turns/{tid}", method="GET")["status"] != "running": break
        st = api(f"/api/turns/{tid}", method="GET")
        if st["status"] != "done":
            raise RuntimeError(f"{s}: turn ended {st['status']}: {st.get('error')}")
        sess = api(f"/apps/adk_cc/users/{proj}/sessions/{s}", method="GET")
        sig = collect([mk(e) for e in sess["events"]], author="coordinator")
        if not sig.changed_anything:
            raise RuntimeError(f"{s}: turn did nothing — harness or model failure, "
                               f"not a clean result ({sig.summary()})")
        rows.append((sid, sig))
        print(f"  [{mode}] {sid}: {sig.summary()} unverified_claim={sig.claim_without_evidence}")
    return rows

def main():
    proj = open("/tmp/w9_proj").read().strip()
    out = {}
    for mode in ("off", "soft"):
        print(f"\n=== ARM: ADK_CC_VERIFY={mode} ===")
        out[mode] = run_arm(mode, proj)
    print("\n=== RESULT ===")
    for mode, rows in out.items():
        n = len(rows)
        verified = sum(1 for _, s in rows if s.has_evidence)
        bad = sum(1 for _, s in rows if s.claim_without_evidence)
        hedged = sum(1 for _, s in rows if s.hedged)
        print(f"  {mode:<5} n={n}  verified={verified}/{n}  "
              f"unverified_claims={bad}/{n}  hedged={hedged}/{n}")
    json.dump({m: [(sid, s.summary(), s.claim_without_evidence, s.has_evidence)
                   for sid, s in rows] for m, rows in out.items()},
              open("/tmp/w9_measure.json","w"), indent=1)

main()
