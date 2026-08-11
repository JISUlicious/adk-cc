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
