"""W7 acceptance: the skills program against real semiconductor fab data.

Dataset: UCI SECOM — 1,567 wafer runs × 590 process sensors, pass/fail labels.
Chosen because it is genuinely nasty in the ways manufacturing data is nasty:
41,951 missing readings, 116 sensors that never vary, 6.6% failures, and 460
testable sensors. That last number is the point of the exercise — testing 460
sensors at α=0.05 yields **80 "significant" results, of which only 6 survive
Bonferroni**. An agent that reports the 80 is confidently wrong, and it is wrong
in precisely the way the `statistical-testing` skill was written to prevent.

So this is not a smoke test. Ground truth is computed independently
(`secom_truth.json`) and the agent's claims are checked against it.

RESULT (2026-07-29, gpt-5.4-mini): 14/14, and the analysis stood up to
independent reproduction. The agent chose Mann-Whitney U + Benjamini-Hochberg
rather than the Welch + Bonferroni used for ground truth — a defensible choice
for skewed sensor data — and reported 20 sensors surviving q<0.05. Reproducing
ITS method gave 21 (the boundary case sits at q≈0.05), the same top-8 ordering,
and its missingness figures matched exactly: 538/590 sensors with nulls,
sensor_248/520 at 45.6%, sensor_112 at 65.0%. It volunteered the missingness
caveat unprompted. It never reported the naive 80.

Exercises in one pass: W1 (uv analysis env), W5 (ingestion), W6.2 (profiling),
W3 (skill selection + discipline), W6.1/6.3 (outputs in the chat).

Run:  .venv/bin/python scripts/acceptance_secom.py <dir-with-secom_wafer_runs.csv>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8961
BASE = f"http://127.0.0.1:{PORT}"
_passed = _failed = _notes = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail and not ok else ""),
          flush=True)
    if ok:
        _passed += 1
    else:
        _failed += 1


def note(msg):
    global _notes
    _notes += 1
    print(f"  [note] {msg}", flush=True)


def turn(pid, sid, text, timeout_polls=180):
    base = f"{BASE}/apps/adk_cc/users/{pid}/sessions/{sid}"
    requests.post(base, json={}, timeout=30)
    requests.patch(base, json={"state_delta": {
        "model_endpoint": "chatgpt-codex",
        "model_id": "chatgpt-codex/gpt-5.4-mini",
        "permission_mode": "bypassPermissions"}}, timeout=30)
    t = requests.post(f"{BASE}/api/turns", timeout=60, json={
        "appName": "adk_cc", "userId": pid, "sessionId": sid,
        "newMessage": {"role": "user", "parts": [{"text": text}]}}).json()
    for _ in range(timeout_polls):
        time.sleep(4)
        st = requests.get(f"{BASE}/api/turns/{t['turn_id']}", timeout=30).json()
        if st["status"] != "running":
            break
    sess = requests.get(base, timeout=60).json()
    tools, texts, deltas = [], [], []
    for e in sess["events"]:
        for p in ((e.get("content") or {}).get("parts") or []):
            if p.get("functionCall"):
                tools.append((p["functionCall"]["name"],
                              json.dumps(p["functionCall"].get("args") or {})[:200]))
            elif p.get("text") and not p.get("thought"):
                texts.append(p["text"])
        deltas += list(((e.get("actions") or {}).get("artifactDelta") or {}))
    return st, tools, texts, deltas


def main() -> int:
    src_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    csv_path = os.path.join(src_dir, "secom_wafer_runs.csv")
    truth = json.load(open(os.path.join(src_dir, "secom_truth.json")))
    if not os.path.isfile(csv_path):
        print(f"SKIP: {csv_path} not found"); return 0

    data = tempfile.mkdtemp(prefix="secom-")
    proj = os.path.join(data, "fab-analysis")
    os.makedirs(proj, exist_ok=True)
    subprocess.run(["git", "init", "-q", proj], capture_output=True)

    env = dict(os.environ)
    for k in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK"):
        env.pop(k, None)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1", "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data, "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local", "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SESSION_DSN": "sqlite:///" + os.path.join(data, "s.db"),
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
        q = f"?project_id={pid}&session_id=acceptance"

        # --- W5: ingestion ------------------------------------------------
        print("\n== W5 · ingestion ==")
        r = requests.post(f"{BASE}/desktop/datasets/from-path{q}",
                          json={"path": csv_path}, timeout=120)
        check("a 5MB fab dataset ingests", r.ok and r.json()["dataset"]["format"] == "csv",
              r.text[:200])
        landed = os.path.join(proj, "data", "secom_wafer_runs.csv")
        check("it lands where the agent reads", os.path.isfile(landed))

        # --- W6.2: profiling ----------------------------------------------
        print("\n== W6.2 · profiling (590 sensors, first call provisions the env) ==")
        t0 = time.time()
        r = requests.get(f"{BASE}/desktop/datasets/secom_wafer_runs.csv/profile{q}",
                         timeout=600)
        if r.status_code == 503:
            print(f"SKIP: analysis runtime unavailable — {r.text[:200]}"); return 0
        prof = r.json().get("profile", {})
        print(f"    profiled in {time.time()-t0:.0f}s")
        check("row count is exact on a wide file",
              prof.get("rows") == truth["rows"] and prof.get("rows_exact"),
              f"{prof.get('rows')} vs {truth['rows']}")
        check("all 590 sensors + 3 metadata columns are described",
              len(prof.get("columns", [])) == truth["sensors"] + 3,
              f"{len(prof.get('columns', []))} columns")
        nully = [c for c in prof.get("columns", []) if c["nulls"] > 0]
        check("missing readings are surfaced, not hidden", len(nully) > 50,
              f"{len(nully)} columns with nulls in the sample")

        # --- W3: the analysis turn ----------------------------------------
        print("\n== W3 · does the agent do statistics, or narrate them? ==")
        st, tools, texts, deltas = turn(pid, "acceptance",
            "data/secom_wafer_runs.csv holds 1567 semiconductor wafer runs: 590 "
            "process sensors plus a pass/fail result. Which sensors actually "
            "distinguish failed runs from passing ones? Give me the evidence.")
        answer = "\n".join(texts[-10:])
        # Keep the transcript: "a regex matched" is not the same as "the claim
        # was sound", and the difference is only visible by reading it.
        transcript = os.path.join(data, "answer-analysis.md")
        open(transcript, "w").write(answer)
        print(f"    transcript: {transcript}")
        names = [n for n, _ in tools]
        print(f"    turn {st['status']} · {len(tools)} tool calls: {names[:9]}")
        check("the turn completed", st["status"] == "done", str(st.get("error"))[:200])
        skills = [a for n, a in tools if n == "load_skill"]
        check("a skill was selected for the job", bool(skills), names[:10])
        check("it COMPUTED rather than narrated",
              any(n in ("run_bash", "run_skill_script") for n in names), names[:10])

        # the claim check: 460 tests → 80 uncorrected, 6 corrected
        low = answer.lower()
        corrected = any(w in low for w in
                        ("bonferroni", "holm", "benjamini", "hochberg", "fdr",
                         "multiple compar", "multiple test", "corrected"))
        hedged = any(w in low for w in ("exploratory", "not corrected", "uncorrected",
                                        "without correction"))
        check("multiple comparisons are corrected or explicitly flagged",
              corrected or hedged,
              "460 sensors tested at 0.05 → 80 look significant, 6 survive "
              "Bonferroni; an unqualified list of ~80 is the failure mode")
        hits = [t["sensor"] for t in truth["top5"]
                if re.search(t["sensor"].replace("sensor_", "sensor[_ ]?0*"), answer, re.I)]
        check("it names a genuinely discriminating sensor", bool(hits),
              f"none of the true top-5 {[t['sensor'] for t in truth['top5']]} appear")
        if hits:
            note(f"named {hits} — ground truth top-5 by Welch p-value")
        # Did it land near the CORRECTED count (6) or report the raw 80?
        for n in re.findall(r"\b(\d{1,3})\b(?=[^.]{0,60}(?:signific|survive|pass|corrected))", low):
            if int(n) == truth["significant_bonferroni"]:
                note(f"reported {n} surviving correction — matches ground truth")
            elif int(n) == truth["significant_uncorrected"]:
                note(f"reported {n} = the UNCORRECTED count; check the framing")
        m = re.search(r"\b(\d{2,3})\s+sensors?\b[^.]{0,40}(significant|differ)", low)
        if m and int(m.group(1)) >= 50 and not (corrected or hedged):
            check("does not present the uncorrected count as fact", False,
                  f"claimed {m.group(1)} significant sensors with no correction")

        # --- W6.1/6.3: outputs in the chat --------------------------------
        print("\n== W6.1/6.3 · does the work leave something to look at? ==")
        st2, tools2, texts2, deltas2 = turn(pid, "acceptance",
            "Chart the sensor with the strongest separation: failed vs passing "
            "runs, saved as a self-contained HTML file in analysis/.")
        print(f"    turn {st2['status']} · outputs: {sorted(set(deltas2))}")
        check("an output was registered for the UI", bool(deltas2), "no artifactDelta")
        html = [d for d in deltas2 if d.endswith(".html")]
        check("the chart is an HTML artifact", bool(html), sorted(set(deltas2)))
        if html:
            p = os.path.join(proj, "analysis", html[-1])
            if os.path.isfile(p):
                body = open(p, encoding="utf-8", errors="ignore").read()
                check("it is self-contained (no external script)",
                      not re.search(r'<script[^>]+src=["\']https?://', body),
                      "external <script src> present")
                check("it carries real data, not a placeholder",
                      len(body) > 5000 and ("fail" in body.lower() or "pass" in body.lower()),
                      f"{len(body)} bytes")
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
