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
| F2 | Retry ladder ≪ free-tier throttle windows; no retry affordance | Medium | open |
| F3 | Confirmation-resume ends at `_handback_to_coordinator` | High | open (investigate) |
| F4 | `exit_plan_mode` restores hardcoded `default`, not pre-plan mode | High | open (small fix) |
| F5 | Confirmation waves: N cards, mid-turn `allow_always` efficacy unverified | Medium | open (investigate) |
| F2c | Failed zero-output turns leave duplicate user messages in history | Medium | open |

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
A plan-approved build emits many writes; each wave parks the turn on N
pending confirmation cards. Whether an `allow_always` grant registered
mid-turn suppresses subsequent same-pattern confirmations in the SAME turn is
unverified — the dogfooding observations were contaminated by driver-client
bugs (answered-call detection matched by name; responses are recorded under
the ORIGINAL call name `adk_request_confirmation`, not the rewrite name
`adk_cc_confirmation_form` — match by call id). Also: no "apply to all
pending" affordance exists.

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
3. **F2a** backend: honor an explicit provider `Retry-After` hint up to 120s
   (computed backoff stays capped at 60s).
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
