# Caveman prompt profile — implementation plan

2026-07-25. Follow-up to `analysis/caveman-instructions.md` (feasibility A/B:
40–63% instruction-token savings, behavior parity on the agentic task at the
3B lower bound). Requirement (user): an OPTION for small-context endpoints,
**off by default** — normal prompts unless explicitly enabled.

## Verified foundations (checked against the installed ADK, not assumed)

- `LlmAgent.instruction` accepts `str | Callable[[ReadonlyContext], str |
  Awaitable[str]]` — an **InstructionProvider**. Profile selection can happen
  per model call with zero agent rebuilding and zero new plumbing.
- ContextGuard's ladder (`resolved_limits()`) is GLOBAL (env
  `ADK_CC_MAX_CONTEXT_TOKENS`) — there is **no per-model window table
  today**. AUTO mode therefore needs a window source (P2 below); P1 ships
  without it.
- Active model at call time: session state `model_endpoint`/`model_id`
  (ModelSessionPlugin) — readable from the provider's `ReadonlyContext.state`.

## Design

### Profile values

`ADK_CC_PROMPT_PROFILE` = `full` (DEFAULT — zero behavior change) |
`caveman` (force compressed everywhere) | `auto` (per-call: compressed only
when the active model's known context window ≤ `ADK_CC_CAVEMAN_MAX_WINDOW`,
default 8192; unknown window → `full`, conservative).

Per-session override: state key `prompt_profile` (same pattern as the model
pin — settable via PATCH state now, chip/UI later). Precedence:
session state > env.

### Components

1. **Variant texts** (`prompts.py`): `COORDINATOR_INSTRUCTION_CAVEMAN`
   (~3,380 → ≤1.6K tok), `EXPLORE_INSTRUCTION_CAVEMAN` (497 → ~185, drafted
   in the feasibility doc), `VERIFY_INSTRUCTION_CAVEMAN` (2,039 → ≤1K).
   Hand-maintained companions — deterministic, reviewable, diffable. The
   compression contract lives in a docstring right above them: every
   enforceable rule, tool name, and format token MUST survive; rhetoric and
   worked examples MAY be dropped (consciously, per variant). VERIFY keeps
   the failure-pattern list and the `VERDICT: PASS|FAIL|PARTIAL` + command-
   output contracts; drops the persuasion prose (flagged for P3 validation —
   that prose is load-bearing psychology on big models).
2. **Selection** (`prompts_profile.py`, ~60 LOC): `active_profile(state) ->
   "full"|"caveman"` implementing the precedence above, and
   `profiled(full_text, cave_text)` returning an InstructionProvider closure.
   `agent.py` wires the three agents: `instruction=profiled(FULL, CAVEMAN)`.
   Utility prompts (capture/synth/title) are EXCLUDED by design — they fit
   any window, and the A/B located the fidelity cost exactly there.
3. **Window lookup for AUTO (P2)**: the model-endpoints registry gains an
   optional per-endpoint `context_window` field (admin-editable in
   Settings → Models), plus a tiny conservative builtin fallback map for
   known families (`apple-fm`→4096, gemma-family→8192; anything unmatched →
   None → full). No scraping, no guessing beyond that list.
4. **Observability**: one INFO log per turn when caveman is active
   (`prompts: caveman profile (model=…, window=…)`), and the active profile
   included in the `/api/context/limits` payload so the UI can badge it
   later without a new endpoint.

### Tests

- **Drift test** (`test_prompt_profiles.py`) — the maintenance backstop:
  a per-agent checklist of enforceable tokens (tool names like
  `transfer_to_agent`/`task_create`/`read_current_plan`; format contracts
  like `VERDICT: PASS|FAIL|PARTIAL`; rule keywords like read-only, the
  first-action HARD RULE, never-address-the-user) asserted present in BOTH
  variants, plus `len(caveman) ≤ 0.5 * len(full)`. A rule edited into the
  full text without a caveman counterpart fails CI.
- **Selection units**: env × state-override × window-lookup matrix, unknown-
  window conservatism, threshold boundary.
- **Harness test**: real App + scripted LLM with profile forced to caveman —
  capture the actual `LlmRequest.config.system_instruction` and assert the
  caveman text was served and the turn completes (reuses the
  test_resumability harness pattern).
- **Live validation (P3, recorded not automated)**: 3–5 scripted explore
  tasks on gpt-5.4-mini, full vs caveman — parity bar: same tools, same
  conclusions. AFM smoke via `scripts/probe_apple_fm.py`.

### Config schema

Two rows (ADVANCED tier), `.env.example` regenerated:
`ADK_CC_PROMPT_PROFILE` (default `full`),
`ADK_CC_CAVEMAN_MAX_WINDOW` (default `8192`, AUTO only).

## Phases

- **P1 (~0.5–1 day)** — ship dark: the three variant texts, `full|caveman`
  env selection + provider wiring, drift + selection + harness tests, schema
  rows. Default `full` ⇒ nothing changes for anyone.
- **P2 (~0.5 day)** — `auto`: registry `context_window` field + Settings
  text input, builtin fallback map, session-state override, observability.
- **P3 (~0.5 day)** — validation + rollout: mini A/B parity recorded in the
  analysis doc; VERIFY-honesty A/B before recommending caveman VERIFY; then
  the Apple-FM lite profile (apple-fm plan P2) consumes this profile as its
  instruction source.

## Non-goals / risks

- No runtime auto-compression (hand-maintained texts only). No tool-list
  slimming here — that's the Apple-FM lite-toolset work; this plan only
  covers instructions.
- Risk: silent behavior drift in compressed VERIFY → gated behind the P3
  honesty A/B; until then AUTO applies caveman to coordinator+explore only
  if VERIFY parity is unproven (per-agent applicability flag in the
  variants module).
- Risk: two texts drifting → the drift test is CI-enforced; the compression
  contract docstring is the reviewer checklist.
- Provider overhead: a dict lookup + string pick per call — negligible.
