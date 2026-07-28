"""LIVE check of the W3 slice-2 skills against the real API + small model.

Two runs, paced for the rate-limited endpoint:
  1. interactive-dashboard-builder — produces a checkable artifact (self-contained
     HTML with real data points), so its output can be verified mechanically.
  2. sql-queries — schema-first discipline on a real sqlite file.

Asserts what matters: the skill was SELECTED from the catalog (list_skills →
load_skill), and the work is real (file exists, plotly inlined, data points > 0 /
the query result matches an independently computed answer).
"""
import csv
import json
import os
import sqlite3
import subprocess
import sys
import time
import re
import urllib.request

REPO = "/Users/jisu/data/workspace/ref/claude-code-leak/adk-cc"
BASE = "http://127.0.0.1:8791"
PORT = 8791
WS = "/tmp/w3-slice2"


def api(path, body=None, method="POST", timeout=120):
    r = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read())


def workspace():
    os.makedirs(WS, exist_ok=True)
    rows = [("2026-01", "north", 1200, 34), ("2026-01", "south", 900, 21),
            ("2026-02", "north", 1500, 41), ("2026-02", "south", 700, 19),
            ("2026-03", "north", 1750, 47), ("2026-03", "south", 1100, 26),
            ("2026-04", "north", 1400, 39), ("2026-04", "south", 1600, 33)]
    with open(f"{WS}/sales.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "region", "revenue", "orders"])
        w.writerows(rows)
    db = f"{WS}/shop.db"
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INT, status TEXT, amount REAL)")
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT)")
    con.executemany("INSERT INTO customers VALUES (?,?,?)",
                    [(1, "acme", "north"), (2, "globex", "south"), (3, "initech", "north")])
    con.executemany("INSERT INTO orders VALUES (?,?,?,?)", [
        (1, 1, "paid", 100.0), (2, 1, "paid", 250.0), (3, 2, "refunded", 90.0),
        (4, 2, "paid", 300.0), (5, 3, None, 75.0), (6, 3, "paid", 425.0)])
    con.commit()
    # ground truth computed HERE, so the model's answer can be checked
    truth = con.execute(
        "SELECT c.region, SUM(o.amount) FROM orders o JOIN customers c ON c.id=o.customer_id "
        "WHERE o.status='paid' GROUP BY 1 ORDER BY 1").fetchall()
    con.close()
    return dict(truth)


def boot():
    subprocess.run(f"lsof -ti :{PORT} | xargs kill 2>/dev/null", shell=True, capture_output=True)
    time.sleep(1)
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADK_CC_API_KEY", "ADK_CC_SKIP_DOTENV", "ADK_CC_SKIP_CONFIG_CHECK")}
    env.update({
        "ADK_CC_AGENTS_DIR": f"{REPO}/agents",
        "ADK_CC_ALLOW_NO_AUTH": "1",
        # Desktop, with the fixture dir REGISTERED AS A PROJECT: the workspace
        # is the project repo, and the session's user_id IS the project id
        # (plugins/checkpoint.py: project_id = ctx.user_id). Setting
        # ADK_CC_WORKSPACE_ROOT alone left the agent unable to see the files.
        "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": "/tmp/w3-slice2-data",
        "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC": "1",
        "ADK_CC_DISABLE_PROJECT_SKILLS": "1",
        "ADK_CC_VERIFY": "soft",
    })
    p = subprocess.Popen(
        [f"{REPO}/.venv/bin/uvicorn", "adk_cc.service.server:make_app", "--factory",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=open("/tmp/w3-slice2.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(1)
        try:
            urllib.request.urlopen(BASE + "/list-apps", timeout=3)
            return p
        except Exception:
            pass
    raise RuntimeError("server did not come up — see /tmp/w3-slice2.log")


PROJ = None


def run_turn(sid, prompt):
    api(f"/apps/adk_cc/users/{PROJ}/sessions/{sid}", {})
    api(f"/apps/adk_cc/users/{PROJ}/sessions/{sid}",
        {"state_delta": {"model_endpoint": "chatgpt-codex",
                         "model_id": "chatgpt-codex/gpt-5.4-mini",
                         "permission_mode": "bypassPermissions"}}, method="PATCH")
    t = api("/api/turns", {"appName": "adk_cc", "userId": PROJ, "sessionId": sid,
                           "newMessage": {"role": "user", "parts": [{"text": prompt}]}})
    tid = t["turn_id"]
    for _ in range(150):
        time.sleep(4)
        st = api(f"/api/turns/{tid}", method="GET")
        if st["status"] != "running":
            break
    st = api(f"/api/turns/{tid}", method="GET")
    sess = api(f"/apps/adk_cc/users/{PROJ}/sessions/{sid}", method="GET")
    tools, texts = [], []
    for e in sess["events"]:
        for p in ((e.get("content") or {}).get("parts") or []):
            if p.get("functionCall"):
                fc = p["functionCall"]
                tools.append((fc["name"], json.dumps(fc.get("args") or {})[:120]))
            elif p.get("text") and not p.get("thought"):
                texts.append(p["text"])
    return st, tools, texts


def _read_settled(path, tries=120):
    """Read only once the file is COMPLETE — a stable size across two polls AND
    a closing </html>.

    Size-stability alone was not enough: a 4.8MB dashboard write raced this
    check twice, and it then reported "no real data" about a file that
    demonstrably contained it. A harness bug that looks exactly like a skill bug
    is worse than having no check at all.
    """
    last, stable, waited = -1, 0, 0.0
    for _ in range(tries):
        size = os.path.getsize(path)
        stable = stable + 1 if size == last else 0
        last = size
        if stable >= 2:
            body = open(path, encoding="utf-8", errors="ignore").read()
            if body.rstrip().endswith("</html>"):
                print(f"    (artifact settled after {waited:.1f}s at {size:,} bytes)")
                return body
        time.sleep(0.5)
        waited += 0.5
    body = open(path, encoding="utf-8", errors="ignore").read()
    print(f"    (artifact NEVER settled in {waited:.0f}s: {len(body):,} bytes, "
          f"ends-with-</html>={body.rstrip().endswith('</html>')})")
    return body


def case_dashboard(check, truth):
    """interactive-dashboard-builder: a checkable artifact."""
    st, tools, _ = run_turn(
        f"dash-{int(time.time())}",
        "Build me an interactive dashboard from sales.csv showing revenue over "
        "time by region and how the regions compare. Save it in analysis/.")
    print(f"\n  dashboard turn: {st['status']}, {len(tools)} tool calls")
    names = [n for n, _ in tools]
    # NOT asserting list_skills: ADK injects the catalog into every request's
    # system instruction, so going straight to load_skill is correct behaviour.
    # Requiring the tool call would test the model's habits, not the skill.
    loaded = [a for n, a in tools if n == "load_skill"]
    check("interactive-dashboard-builder was selected",
          any("dashboard" in a for a in loaded), loaded or names[:10])
    # Skip dot-dirs: W1 provisions a uv analysis-env into `.adk-cc/`, and
    # matplotlib ships template .html files inside site-packages. Taking the
    # "last .html found" picked up a 1.3KB library template and reported the
    # dashboard as data-less — three runs in a row, on a correct artifact.
    html = None
    for root, dirs, files in os.walk(WS):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".html"):
                cand = os.path.join(root, f)
                if html is None or os.path.getsize(cand) > os.path.getsize(html):
                    html = cand
    check("an HTML dashboard was written", html is not None)
    if not html:
        return
    body = _read_settled(html)
    # An inlined plotly bundle CONTAINS the string "cdn.plot.ly" (a default
    # topojson URL for geo charts). Self-contained means no external
    # <script src>, which is what actually breaks offline.
    ext = re.findall(r'<script[^>]+src=["\']https?://[^"\']+', body)
    check("self-contained (no external script fetch)", not ext,
          f"{len(body)} bytes, {ext[:1]}")
    hits = [v for v in ("1200", "1500", "1750", "1400", "900", "700", "1100", "1600")
            if v in body]
    check("charts contain real data from the CSV", len(hits) >= 3, hits)


def case_sql(check, truth):
    """sql-queries: the answer must exclude the refunded and NULL-status rows."""
    st, tools, texts = run_turn(
        f"sql-{int(time.time())}",
        "Using shop.db, what is total paid revenue by customer region? "
        "Give me the query and the numbers.")
    print(f"\n  sql turn: {st['status']}, {len(tools)} tool calls")
    names = [n for n, _ in tools]
    loaded = [a for n, a in tools if n == "load_skill"]
    check("sql-queries was selected", any("sql" in a for a in loaded), loaded or names[:10])
    answer = "\n".join(texts[-6:])
    north, south = truth["north"], truth["south"]
    check(f"north total is right ({north:g})", str(int(north)) in answer, answer[-300:])
    check(f"south total is right ({south:g})", str(int(south)) in answer, answer[-300:])
    check("did not silently include refunded/NULL rows",
          "165" not in answer and "1240" not in answer, answer[-200:])


def case_hiring(check, truth):
    """hiring-kit: the jurisdiction discipline, under pressure.

    The prompt deliberately invites country-specific facts (offer terms,
    probation) WITHOUT naming a country. The skill must establish or flag the
    context instead of silently picking one.
    """
    st, tools, texts = run_turn(
        f"hire-{int(time.time())}",
        "Write a job description and interview loop for a senior backend "
        "engineer, including the offer terms and probation arrangement.")
    print(f"\n  hiring turn: {st['status']}, {len(tools)} tool calls")
    loaded = [a for n, a in tools if n == "load_skill"]
    # ASKING counts. The skill tells the model to establish jurisdiction/entity
    # before writing offer terms, and the correct response to an underspecified
    # request is a question — which arrives as an ask_user_question TOOL CALL,
    # not as text. The first version of this check read only text and scored a
    # correctly-asking run as a failure.
    asked = " ".join(a for n, a in tools if n == "ask_user_question")
    answer = "\n".join(texts[-8:])
    evidence = (answer + "\n" + asked)
    open("/tmp/w3-hiring-answer.txt", "w").write(
        f"--- tool calls ---\n" + "\n".join(f"{n} {a}" for n, a in tools)
        + f"\n--- text ---\n{answer}\n")   # kept for review
    low = evidence.lower()
    check("hiring-kit was selected", any("hiring" in a for a in loaded),
          loaded or [n for n, _ in tools][:8])
    check("context gap is surfaced (asked or flagged), not silently assumed",
          any(w in low for w in ("not established", "jurisdiction", "which country",
                                 "where will", "employing entity", "location")),
          evidence[:300])
    # (?i) must be at the START of the pattern — Python 3.11+ raises "global
    # flags not at the start", which silently killed this check on its first run.
    invented = re.findall(
        r"(?i)\b\d+\s*(?:month|week|day)s?\s+(?:of\s+)?(?:probation|notice)"
        r"|\bprobation(?:ary)?\s+period\s+(?:is|of)\s+\d+"
        r"|\bat-will\b|\bstatutory\s+(?:notice|minimum)\s+is\b", evidence)
    check("no statutory employment fact invented", not invented, invented[:3])
    check("routes local specifics to a human/counsel, or has not asserted any yet",
          any(w in low for w in ("counsel", "hr ", "legal review", "verify locally",
                                 "local ", "jurisdiction")), evidence[-300:])


CASES = {"dashboard": case_dashboard, "sql": case_sql, "hiring": case_hiring}


def main():
    global PROJ
    # Each case costs a live turn on a rate-limited endpoint; --only=NAME runs one.
    only = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--only=")), None)
    cases = [(n, f) for n, f in CASES.items() if only in (None, n)]
    if not cases:
        print(f"unknown case {only!r}; choose from {list(CASES)}")
        return 2

    truth = workspace()
    proc = boot()
    PROJ = api("/desktop/projects", {"path": WS})["project"]["id"]
    print(f"  project {PROJ} bound to {WS}")
    ok = fails = 0

    def check(name, cond, detail=""):
        nonlocal ok, fails
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
              + (f" — {detail}" if detail and not cond else ""))
        if cond:
            ok += 1
        else:
            fails += 1

    try:
        for i, (name, fn) in enumerate(cases):
            if i:
                time.sleep(12)      # pace the rate-limited endpoint
            fn(check, truth)
    finally:
        proc.terminate()
    print(f"\n{ok} passed, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
