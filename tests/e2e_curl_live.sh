#!/usr/bin/env bash
# Live API smoke test via curl against a RUNNING adk-cc server.
#
# Sends real HTTP requests (no Python harness) to exercise the agent end to
# end: create a session, ingest a note into the wiki (→ wiki_add), then query
# it back (→ wiki_search, which overlays the caller's inbox). Verifies the
# real REST surface a client would use.
#
# Usage:
#   tests/e2e_curl_live.sh
#   ADK_CC_BASE=http://host:8000 ADK_CC_USER=alice ADK_CC_TOKEN=tok tests/e2e_curl_live.sh
#
# Env:
#   ADK_CC_BASE   server base URL (default http://127.0.0.1:8000)
#   ADK_CC_USER   user id in the path (default local; dev server is no-auth)
#   ADK_CC_TOKEN  bearer token (optional; omit for a no-auth dev server)
#
# Exit 0 = pass, 1 = fail, 2 = server unreachable. Tolerant of model variance:
# REST mechanics are asserted firmly; the agent's tool use is reported and
# checked, with the raw responses printed for inspection.
set -uo pipefail

BASE="${ADK_CC_BASE:-http://127.0.0.1:8000}"
APP="adk_cc"
USER_ID="${ADK_CC_USER:-local}"
MAXT=180
PY="$(dirname "$0")/../.venv/bin/python"; [ -x "$PY" ] || PY=python3

AUTH=()
[ -n "${ADK_CC_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${ADK_CC_TOKEN}")

OK=1
pass() { echo "  [PASS] $1: ${2:-}"; }
fail() { echo "  [FAIL] $1: ${2:-}"; OK=0; }

# field <json> <python-expr over `d`>  — parse a field with python (no jq dep)
field() { printf '%s' "$1" | "$PY" -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('');sys.exit()
$2" 2>/dev/null; }

# tools_of <run-events-json> -> space-separated functionCall names
tools_of() { printf '%s' "$1" | "$PY" -c "import sys,json
try: ev=json.load(sys.stdin)
except Exception: print('');sys.exit()
print(' '.join(p['functionCall']['name'] for e in ev for p in (e.get('content') or {}).get('parts') or [] if p.get('functionCall')))" 2>/dev/null; }

# run_turn <text> -> echoes the raw /run events JSON.
# JSON is built with dict()/parens (no brace literals) and values passed via
# env, so the shell never sees a "{a,b}" that it would brace-expand. Body goes
# through a temp file (curl --data @-) to dodge all quoting hazards.
run_turn() {
  local text="$1" pf
  pf=$(mktemp -t adkcc_run.XXXXXX)
  TEXT="$text" A="$APP" U="$USER_ID" S="$SID" "$PY" -c 'import os, json
print(json.dumps(dict(
    appName=os.environ["A"], userId=os.environ["U"], sessionId=os.environ["S"],
    newMessage=dict(role="user", parts=[dict(text=os.environ["TEXT"])]))))' > "$pf"
  curl -s -X POST "$BASE/run" --max-time "$MAXT" -H 'Content-Type: application/json' \
    ${AUTH[@]+"${AUTH[@]}"} --data @"$pf"
  rm -f "$pf"
}

echo "=== adk-cc curl live test → $BASE (user=$USER_ID) ==="

# 1. health
code=$(curl -s -o /dev/null -w '%{http_code}' ${AUTH[@]+"${AUTH[@]}"} "$BASE/list-apps")
if [ "$code" != "200" ]; then echo "  server unreachable at $BASE (HTTP $code)"; exit 2; fi
pass "server reachable" "GET /list-apps -> 200"

# 2. create session
SESS_JSON=$(curl -s -X POST "$BASE/apps/$APP/users/$USER_ID/sessions" \
  -H 'Content-Type: application/json' ${AUTH[@]+"${AUTH[@]}"} -d '{}')
SID=$(field "$SESS_JSON" "print(d.get('id',''))")
[ -n "$SID" ] && pass "session created" "$SID" || fail "session created" "$SESS_JSON"
[ -n "$SID" ] || { echo "cannot continue without a session"; exit 1; }

# 3. ingest turn (→ wiki_add)
echo "  -- turn 1: ingest --"
ING=$(run_turn "Save this to the wiki under topic 'curl-smoke': The L1 cache line size is 64 bytes.")
T1=$(tools_of "$ING")
echo "    tools: [$T1]"
case " $T1 " in *" wiki_add "*) pass "ingest called wiki_add" "$T1";; *) fail "ingest called wiki_add" "got [$T1]";; esac
sleep 8  # pace under the model rate limit

# 4. query turn (→ wiki_search overlaying the caller's inbox)
echo "  -- turn 2: query --"
Q=$(run_turn "Search the wiki for the cache line size and tell me the value.")
T2=$(tools_of "$Q")
echo "    tools: [$T2]"
case " $T2 " in *" wiki_search "*) pass "query called wiki_search" "$T2";; *) fail "query called wiki_search" "got [$T2]";; esac
if printf '%s' "$Q" | grep -q "64"; then pass "ingested value recalled" "found '64' in the answer"
else fail "ingested value recalled" "'64' not in the query response"; fi

echo
[ "$OK" = 1 ] && { echo "curl live test PASSED"; exit 0; } || { echo "curl live test FAILED"; exit 1; }
