# Dogfooding findings — fix plan (2026-07-22)

Source: live dogfooding session driving the desktop app purely over its own API
("AgentBox" landing page + product console, two models). Sessions of record:

- `bc7f07dbb0dc / landing-1`, `console-1` — gemma-4-31b-it:free (Openrouter, pinned)
- `6ecfe56bceca / landing-gpt-1`, `console-gpt-1` — gpt-5.6-sol (chatgpt-codex, pinned)

The exercise validated: per-session model pins under concurrent multi-provider
load, pre-stream 429 retry + surfaced stream errors, protected-path floor
(deny honored by the agent), self-initiated `enter_plan_mode` on an open-ended
prompt, the full plan → approval → execute loop, and the restyled plan/approval
UI carrying real content. The findings below are what broke or ground.

## Findings ledger

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| F1 | Turn dies on client disconnect | High | open (design first) |
| F2 | Retry ladder ≪ free-tier throttle windows; no retry affordance | Medium | **F2a FIXED** (classifier: burst/upstream/quota ladders, quota fail-fast w/ reset time); F2b UI retry button still open |
| F3 | Confirmation-resume ends at `_handback_to_coordinator` | High | **ROOT-CAUSED + client mitigation shipped** (see below); server-side fix still open |
| F4 | `exit_plan_mode` restores hardcoded `default`, not pre-plan mode | High | **FIXED** (enter records `plan_previous_mode`; exit restores it, never into plan, marker consumed) |
| F5 | Confirmation waves: N cards, mid-turn `allow_always` efficacy unverified | Medium | **F5a RESOLVED — grants work as designed** (see below); F5b apply-to-all UI still open |
| F2c | Failed zero-output turns leave duplicate user messages in history | Medium | open |
| F6 | Command-safety: heredoc handling + /tmp scope | High (security) | **FIXED** — 3 parts: heredoc bodies stripped as data (the flagged probe was a FALSE POSITIVE: a JS regex mined as a path); /tmp+$TMPDIR now in scope by design (scratch convention, user decision); `_in_scope` fail-open now logs. The live /tmp pass was thus consistent with intended design; the false positive was the actual bug. |
| F7 | Desktop audit sink apparently silent | High | **CLOSED — not a bug.** Repo `.env` sets a RELATIVE `ADK_CC_AUDIT_LOG=./.audit/audit.jsonl`; the sidecar (cwd=repo) wrote 6k+ rows there all along, incl. every dogfooding session. Hardening shipped: override absolutized at bind + the bound sink logged once at INFO (the trail was undiscoverable, which is what made this look like data loss). |

### F1 — turn dies on client disconnect
Symptom (twice): SSE consumer timing out / dropping severs the run mid-flight.
Completed tool calls persist (files were written), but remaining iterations —
including the agent's final summary — never run. Same applies to the real web
UI on tab close/refresh. Evidence: gemma `landing-1` round 1 (write landed,
summary lost); sol `console-gpt-1` build turn (died between writes, no pending
confirmation, needed a nudge).

### F2 — rate-limit retry ceiling vs free-tier reality
The in-model retry ladder (ADK_CC_MODEL_RETRIES=3, ~35s total) works as
designed but OpenRouter `:free` throttle windows are minutes. Observed: turns
failing after full ladder repeatedly (`landing-1` round 2 needed 3 external
attempts over ~9 min; `console-1` T1 needed more). The user's only affordance
after the surfaced error is manually re-sending the message.

### F3 — confirmation-resume drops the coordinator continuation
**Root cause (confirmed in ADK source)**: `Runner._find_agent_to_run` roots a
function-response resume at the agent that ISSUED the call (the sub-agent).
Our `_force_coordinator_continuation` handback is a synthetic non-final event
that only works when a coordinator flow WRAPS the specialist — in the resumed
invocation there is none, so the marker dangles and the run ends.
**Shipped mitigation (client)**: ChatPage detects a stream that ENDS on a
dangling `_handback_to_coordinator` (never happens in healthy turns — the
coordinator's reply always follows it) and auto-continues once ("Continue.").
**Still open (server)**: continuation belongs server-side — e.g. after a
resumed run terminates on a dangling handback, re-invoke the coordinator in
the same request. Client mitigation covers the web UI only; API drivers still
see the drop.
Repro observed on `console-gpt-1`: Explore (sub-agent, in plan mode) hit a
protected-path confirmation; answering it resumed the run; Explore finished
and called `_handback_to_coordinator`; the RUN ENDED there — the coordinator
never got its post-handback iteration (no plan written until a manual
"Go on."). Suspect: ADK's resume flow roots the resumed run at the paused
agent, so its completion ends the run instead of returning to the parent.

### F4 — `exit_plan_mode` loses the pre-plan permission mode
`agents/adk_cc/tools/exit_plan_mode.py` (~line 130) sets
`ctx.state["permission_mode"] = "default"` on approval. A desktop session that
was in `bypassPermissions` (the desktop default) before `enter_plan_mode`
comes back to `default` — every subsequent write in the approved build then
demands confirmation (observed: 6+ pending write confirmations immediately
after plan approval on `console-gpt-1`). `enter_plan_mode` already computes
`previous_mode` for its tool result; it just isn't persisted/restored.

### F5 — confirmation waves and mid-turn `allow_always`
**F5a verdict (code-read, plugins/permissions.py:518)**: grants are stored in
session state (`adk_cc_allow_rules`) and reloaded by `_effective_settings` on
EVERY decide — a mid-turn `allow_always` DOES cover all future iterations.
The observed waves have a structural cause: the model emits several gated
calls in ONE iteration, and each requests confirmation before any answer can
exist. So the remaining work is ergonomics (F5b apply-to-all; optionally a
server sweep where a new grant auto-resolves other PENDING confirmations it
covers), not a grant bug. NOTE: `tests/e2e_confirmation_flow.py` is broken
in this environment (fails identically at bc78acd, pre-dating all of this
work — separate investigation).
A plan-approved build emits many writes; each wave parks the turn on N
pending confirmation cards. Whether an `allow_always` grant registered
mid-turn suppresses subsequent same-pattern confirmations in the SAME turn is
unverified — the dogfooding observations were contaminated by driver-client
bugs (answered-call detection matched by name; responses are recorded under
the ORIGINAL call name `adk_request_confirmation`, not the rewrite name
`adk_cc_confirmation_form` — match by call id). Also: no "apply to all
pending" affordance exists.

### F6 — out-of-project heredoc redirect passed the write gate
Observed in the gpt-5.4-mini A/B run (`f4e707522004/mvp-1`): two commands of
the shape `cat > /tmp/console_probe.js <<'NODE' ...` executed with status ok,
while a third out-of-project write was correctly stopped with "command writes
or deletes a path outside the project — requires confirmation". Suggests the
danger classifier misses (some) `>` redirect targets — possibly heredoc
parsing. Security-relevant (silent-exec/exfil class); reproduce with the
classifier's unit harness (pure string→verdict, no exec) and fix detection.

## Fix plan

### P1 — clear bugs (small, do first)
1. **F4**: `enter_plan_mode` persists `state["plan_previous_mode"]`;
   `exit_plan_mode` approval restores it (fallback `"default"` when absent —
   e.g. plan mode entered via UI toggle). Deny path unchanged (stays in plan
   mode). Tests: unit on both tools; integration asserting
   `bypassPermissions → plan → approve → bypassPermissions`.
2. **F3**: deterministic repro (pending sub-agent confirmation → answer →
   observe run end), then read ADK's resume path. Candidate fixes:
   (a) make the handback transfer survive resume; (b) app-level guard — when
   a resumed run terminates with `_handback_to_coordinator` as the final
   action, immediately re-run the coordinator (synthetic continuation).
   Choose based on where the drop actually happens.

### P2 — rate-limit UX + confirmation ergonomics

#### F2 design (researched 2026-07-22, openrouter.ai/docs/api-reference/limits)
OpenRouter facts: free-variant models (`:free`) are capped at **20 req/min**
AND **50 req/day** (1,000/day with ≥$10 lifetime credits); daily caps reset at
**UTC midnight**. 429s carry `X-RateLimit-Limit/Remaining/Reset` and sometimes
`Retry-After`; `GET /api/v1/key` reports remaining quota. Crucially there are
THREE distinct 429 classes and one ladder cannot serve them:

| Class | Signature | Right response |
|---|---|---|
| Burst (20 rpm) | OpenRouter `rate_limit_exceeded`, reset near | current ladder (5/10/20s) is well-matched |
| Daily quota | `X-RateLimit-Reset` far away (hours) | retrying is POINTLESS — fail fast with "free-tier daily quota exhausted, resets HH:MM UTC (in Xh)"; suggest switching model |
| Upstream provider throttle | `metadata.provider_name` + "temporarily rate-limited upstream" (today's gemma case) | longer paced ladder: ~30/60/120s, then surface with the Retry button |

Implementation sketch (`models/selectable.py` + a small
`models/rate_limit.py` classifier):
1. `classify_429(err) -> (kind, reset_hint_s)` — parse
   `X-RateLimit-Reset`/`Retry-After` from `err.response.headers`, sniff
   `provider_name`/"rate-limited upstream" in the body for the upstream class.
2. Ladder per class: burst → base·2^n (today's behavior); upstream →
   (6·base)·2^n capped 120s; quota → zero retries, raise a wrapped error whose
   message carries the human reset time. No new env knobs — both ladders
   derive from ADK_CC_MODEL_RETRIES / ADK_CC_MODEL_RETRY_BASE_S.
3. Keep every sleep behind the global pacing throttle (never burst).
4. NOT doing in-product: proactive `GET /api/v1/key` quota probes — requires
   handling the raw key outside the write-only boundary. As an OPERATOR
   diagnostic it's documented here: read the key from the registry file
   locally, `GET https://openrouter.ai/api/v1/key`, never echo the key.
5. ACCOUNT FACTS (probed 2026-07-23, user-authorized): this account has the
   $10 deposit → `is_free_tier: false`, free-model cap 1,000/day, key-level
   rate limit unlimited. The 2026-07-22 all-day gemma:free 429s were therefore
   the SHARED UPSTREAM POOL (Google AI Studio capacity for OpenRouter's free
   route, pooled across all OpenRouter users), NOT account quota — ~30-40
   requests used of 1,000. For the `upstream` class there is no computable
   reset; switching the session's model is the only reliable remedy (the
   classifier + slow ladder behaved correctly; the earlier "daily quota wall,
   resets UTC midnight" narrative in this doc was wrong for this account).

3. **F2a** backend: honor an explicit provider `Retry-After` hint up to 120s
   (computed backoff stays capped at 60s). Subsumed by the classifier above.
4. **F2b** UI: rate-limit stream error → render a **"Retry turn"** button.
   MUST reuse the existing user event, not append a new one — see F2c.
5. **F2c** (found while retrying gemma): a turn that fails before ANY model
   output still leaves its user message appended to the session — external
   retries piled up 6 duplicate "Go on." events + a duplicated task prompt in
   `console-1`. Fix direction: roll back (or dedupe) the user event when the
   run dies with zero model-authored events, or make retry-by-resend reuse
   the last user event. Also poisons context for the eventual successful turn.
5. **F5a** investigation: controlled test — grant `allow_always` at wave N,
   verify wave N+1 of the same pattern doesn't ask. Fix grant-consultation
   timing in the permission engine if it does.
6. **F5b** UI: "Apply to all pending" control when multiple confirmations are
   pending in one turn.

### P3 — durable turns (design-first, biggest)
**Design note written: analysis/durable-runs-design.md (PROPOSED)** — Turn
Broker owning run execution server-side; closes F1, F3-server, F2b, F2c in
four phases. Review before implementing.

7. **F1**: decouple run execution from the SSE consumer — the run executes as
   a server-side task persisting events; `/run_sse` tails it; disconnect
   loses the view, not the work; reopening re-attaches. Touches the
   `run_sse` wrapping in `build_fastapi_app`; explicit abort must still
   cancel. Same shape as "background turns". Write a short design note
   before implementing; decide scope after P1/P2.

## Notes for the implementer
- Driver-client lesson that applies to the web UI too: pending-confirmation
  detection must match function responses by **call id**, never by name.
- The model-selection semantics settled during the same period: `/model` +
  composer chip = per-session pin; Settings → Models = global default
  (see analysis/../memory; don't regress while touching plan/permission flows).
- Cosmetic, unrelated: the seeded `default` endpoint carries a doubled model
  prefix (`openai/openai/gpt-oss-120b`) from pre-prefix-routing data; harmless
  at runtime, tidy up in the registry when convenient.
