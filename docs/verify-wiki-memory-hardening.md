# Verifying the wiki/memory hardening (#126) — operator runbook

Covers commits `79cada2` (principal scoping), `700d121` (guide +
/knowledge state), `acdfa77` (publish-time PII). Python-only server
changes + one client-side page change (frontend rebuild needed for §4).

## 0. Deploy

```bash
git pull && <restart server>
cd web && npm run build && npm run build:desktop   # for §4 only
```

Automated proof on the box (no model key needed):

```bash
ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_principal_scoping.py   # expect 5/5
ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_wiki_add_restriction.py
```

## 1. Identity scoping (the leak) — authed web only

Needs two real users (JWT or `ADK_CC_AUTH_TOKENS=tok-a=alice:local,tok-b=bob:local`).

```bash
# as alice, claim to be bob → MUST be 403
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/api/turns \
  -H "Authorization: Bearer tok-a" -H 'Content-Type: application/json' \
  -d '{"appName":"adk_cc","userId":"bob","sessionId":"any",
       "newMessage":{"role":"user","parts":[{"text":"hi"}]}}'
```

- `403` → fixed. `404` (session not found) → NOT fixed: the old code
  reached session lookup before any ownership check.
- Boot log check: with auth + memory/wiki on and `ADK_CC_AUTHZ` unset,
  the server must log `"… ADK_CC_AUTHZ is OFF — set ADK_CC_AUTHZ=1 …"`.
  Then set `ADK_CC_AUTHZ=1` (recommended end state) and re-check that
  the ADK-native path also refuses:
  `curl -H "Authorization: Bearer tok-a" $BASE/apps/adk_cc/users/bob/sessions` → 403.
- Memory-level spot check: run a turn as alice mentioning a durable fact;
  confirm the new episodic file lands under `users/alice/`, not any other
  uid: `ls <MEMORY_ROOT>/<tenant>/users/*/episodic/ | tail`.

## 2. Publish-time PII (librarian)

As any user, ask the agent to `wiki_add` a personal note (e.g. topic
`about-me`, text "my name is …") — the TOOL may already refuse
(`status: skipped, personal_info`); if it does, write the inbox file by
hand to test the second gate:

```bash
python scripts/wiki_librarian.py       # or wait for the cron
```

Expected: merge report shows `pii_withheld >= 1`; log line
`"librarian: N personal claim(s) on '<slug>' withheld from publish"`;
NO new page under `<WIKI_ROOT>/<tenant>/domain/wiki/` for that slug; the
note remains in `users/<uid>/inbox/`. A normal domain fact in the same
run must still merge (proves the filter isn't over-broad).

## 3. Memory lifecycle sanity (both shells)

Follow §5 of `docs/07-wiki-memory.md`: durable fact → episodic file →
recall next turn → consolidator → semantic file → visible on the
/knowledge memory tab. On desktop remember scoping is per-PROJECT.

## 4. /knowledge graceful state (frontend rebuild required)

- Server with `ADK_CC_KNOWLEDGE_UI` UNSET: open `/knowledge` → must show
  "The knowledge view is not enabled on this server…" (not a raw fetch
  error). 
- Server with it set: both tabs render; the memory tab shows only the
  AUTHENTICATED user's items — verify as bob that alice's facts are
  absent (this was already principal-only; re-pin it anyway).

## 5. Regression edges

- No-auth dev flows unchanged: `/api/turns` with any userId still works
  without a token; desktop app unaffected (own resolver, no auth).
- A LEGITIMATE authed request (userId == principal) is not blocked —
  anything but 403 is fine at this layer.

If any step fails, report: which step, the exact status code / log line,
and whether `ADK_CC_AUTHZ` was set — those three identify the layer.

## 6. Triage: wiki_add ran but NO inbox nodes on the web wiki tab

Inbox nodes are read straight from the store at request time — if they are
missing, the CAPTURE and the DISPLAY resolved different (tenant, user)
scopes. Check in this order, ON the remote box:

```bash
# 1. Where did the notes actually land?
find $ADK_CC_WIKI_ROOT -name '*.md' | sed "s|$ADK_CC_WIKI_ROOT/||" | head
#    → local/users/<CAPTURE-UID>/inbox/…  ← note the uid segment
```

2. Compare `<CAPTURE-UID>` with the uid the graph reads: the web graph
   route uses the AUTH PRINCIPAL's user_id and ignores `?user=`. If the
   two differ, that is the pre-`79cada2` identity bug: capture followed
   the client-supplied session user_id while display follows the
   principal. **Fix: pull ≥ `79cada2` and restart** — capture then scopes
   by the principal too, and new notes appear. (Old notes stay under the
   old uid; move the files or re-add.)
3. If the uid matches and nodes are still missing: open the wiki_add TOOL
   CARD in the thread (it expands) — a `status: "skipped",
   reason: "personal_info"` result means the PII guard refused the
   capture and nothing was stored; `status: "error"` names the cause.
4. Only if 1–3 check out: confirm the server was restarted after the pull
   (stale process = stale routes) and that `ADK_CC_WIKI_ROOT` in the
   service env matches the path you inspected in step 1.
