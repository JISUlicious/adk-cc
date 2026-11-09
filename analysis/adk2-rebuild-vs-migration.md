# adk-cc on ADK 2.14: rebuild vs migration — two plans, compared

Date: 2026-11-09. Companion to `adk2-migration-spec.md` (the migration plan in detail) and to the analysis-docs general analysis `google-adk/architecture/ADK_2x_그래프와_에이전트루프.md` (public, JISUlicious/analysis-docs @ d3e6a54).

Numbers used below (measured 2026-11-09): 53.5K Python lines in `agents/adk_cc` — 29.3K in 131 files that never import ADK, 24.3K in 80 files that do; 18.1K lines of web/desktop UI; 31 plugins, 40 tools, 307 test files (23 Runner-based, 27 live); 52 "measured in real use / found by the live e2e" annotations in code; 27 commits to plugins+service in the last 90 days; code that 2.14 makes redundant is about 2.1K lines of plugins (`precompact`, `microcompact`, `context_guard`, `tolerant_tool_json`, `session_retry`) + about 130 lines in `turns.py` + part of the 2.6K-line skills launcher.

---

## 0. What the agreement fixes — the yardstick for both plans

The cross-review with analysis-docs settled principles any ADK 2.x design of an *interactive* harness must satisfy:

1. **Core is a model-driven loop** (`LlmAgent` root). `Workflow` only for unattended, fixed-shape, re-runnable surfaces.
2. **What a harness takes from 2.x is the execution journal** — abort sealing, runner-owned compaction with call/response-safe ranges, node checkpoints, `StaleSessionError` — not graph syntax.
3. **Gates are step-boundary callbacks with a strength ladder** (off/soft/hard) and **external judgment** (tests, verifier agent, human) — never the working model's self-report.
4. **Stages are policy bundles** (tool surface + permissions + prompt); **transitions are harness-held** (plan mode, approval-gated exit).
5. **Spawning has three layers**: lifetime binding (in-process registry, or an *awaited* `run_node`), history consistency (upstream sealing), idempotent resume (a journal of child results under stable keys).
6. **Mid-turn user input is delivered at step boundaries only.**
7. **Root choice is a delegation-semantics choice**: inside a `Workflow`, a transfer ends the node and the scheduler drives the handoff; AutoFlow keeps it in-run.

The observation that drives the comparison: **adk-cc already has this shape** — coordinator `LlmAgent` + plugins + Turn Broker + tools; plan mode; `ADK_CC_VERIFY` ladder with an external verifier; spawn/collect with a session registry; mid-turn plugin at `before_model`. The agreement did not surface a different target architecture. It confirmed the current one and named the gaps, all of which are journal items (abort sealing, compaction ownership, explorer result persistence).

---

## 1. Plan R — Rebuild

**Definition.** New skeleton written against 2.14 idioms; ADK-free modules carried over as libraries; the ADK-coupled layer re-implemented; parity proven against the current system; big-bang cutover. (A "rebuild" that copies all 65K+ ADK-free lines and re-implements the rest is the only honest version — rewriting the sandbox backends or the web UI would buy nothing.)

| Phase | Work | Exit criterion | Effort |
|---|---|---|---|
| **R0 Skeleton** | new package; `App`/`Runner` with resumability and runner-owned `EventsCompactionConfig`; `abort_signal`; `api_server`-based FastAPI; `FileSessionService` kept (same JSONL, same data dir); config schema reused | hello-world coordinator + one tool streams to the *existing* web UI contract | 1 wk |
| **R1 Carry over ADK-free libraries** | sandbox backends + process registry (7.9K), command-safety classifier, config schema, memory store + consolidation, identity/authz/tenancy, datasets, wiki, web/desktop UI (18K; unchanged if the API contract is kept) | their unit tests pass unchanged | 1–2 wk |
| **R2 Re-implement the ADK-coupled layer** | 31 plugins on the same 14 hooks (most become near-verbatim ports — the value is the policy, not the glue); 40 tools; Turn Broker rewritten on the upstream journal (`abort_signal`, sealing, `GetSessionConfig`, per-session compaction via `before_run`); confirmation flow on 2.12+ resume routing with no monkeypatch; skills on upstream environment mode (`BaseEnvironment` over our sandbox backends) + trust gate / lazy deps / `SKILL_DATA_DIR` as hooks; memory plugin on `after_run`-on-abort semantics; mid-turn plugin; spawn/collect with the result journal | feature-by-feature parity checklist green | 5–7 wk |
| **R3 Parity** | port 307 test files; live batteries A/B old vs new on the same scenarios; dogfood rounds to rediscover behaviors that only live in the 52 annotations and in users' habits | batteries equal or better; no P0/P1 regressions in two dogfood rounds | 3–4 wk |
| **R4 Cutover** | dual-run; data compatibility (sessions JSONL, memory store, endpoint registry, desktop data dir); desktop build; remote deploy; decommission old | old tree deleted | 1 wk |

**Total: 11–15 weeks of effort**, with a regression tail, and during it either a feature freeze on `main` or double maintenance.

**What R buys that M does not, by itself:** no `compat/adk.py` shim; legacy removed in one sweep (dormant worktree isolation, tenancy modes, the 206-var env surface); upstream skills environment mode from day one; freedom to lay out batteries on `Workflow` natively; cleaner module layering.

**What R risks:** unknown unknowns — a rewrite re-derives the bugs the tests and annotations encode (dash `kill --`, orphan calls after Stop, confirmation waves, memory dedup, "explorer answered from memory"); it bets the whole product on upstream semantics *before* measuring them (resume of an ended gated invocation, sealing of pending confirmations, skills cwd); the client's daily path (skills) changes wholesale; the only reversibility point is the cutover; months of opportunity cost.

---

## 2. Plan M — Migration (spec §5, as updated by the agreement)

| Phase | Work | Exit criterion | Effort |
|---|---|---|---|
| **P0 Spike** | pin 2.14; run the 23 Runner-based suites + plugin/tool units; failure inventory by cause class; `compat/adk.py` with a pin test per private symbol | inventory with owners | 1–2 d |
| **P1 Compat, no behavior change** | imports/re-exports; `FileSessionService.flush`; Event round-trip incl. `node_info`/`isolation_scope`; `get_fast_api_app` update; drop `extract_adk_web_server`; **A/B each workaround against its original repro** (confirmation-resume patch, `tolerant_tool_json`, `session_retry`, #119 pin) | units green on 2.14; live battery green; no monkeypatch without a pin test | 3–5 d |
| **P2 Runtime adoption (the journal)** | `abort_signal` + upstream sealing replaces `_close_interrupted_calls`; decide Stop-with-pending-confirmation semantics; per-session compaction via `before_run`, ContextGuard recalibrated; `after_run`-on-abort guards for memory/title/notes; **explorer result journal** (child envelope → parent-session event keyed by child id + task hash) | durable-runs, confirmation, mid-turn, compaction, subagent e2es green | 4–6 d |
| **P3 Skills** | option (a) adapt `RunSkillScriptTool` declaration, keep launcher and workspace cwd; (b) `BaseEnvironment` over our backends only after a measured win | skill suites green; adversarial e2e 4/4; client scenario re-run | 2–3 d (+1–2 wk only for (b)) |
| **P4 Server** | `api_server`/`dev_server` composition; `http.disconnect` abort; `GetSessionConfig` for long sessions | web + desktop UI e2es green; remote deploy dry-run | 2–3 d |
| **P5 Opportunities (after merge, incremental)** | `Workflow` for batteries/evals first, then memory consolidation and wiki passes; decisions classifier for auto mode; legacy sweeps as separate PRs | each behind a flag with its own A/B | ongoing |

**Total to merge: 3–5 weeks**, `main` keeps shipping, every removed workaround ships only with an A/B showing its repro passes without it.

**What the agreement changed in M:** P5 `Workflow` scope narrowed to unattended surfaces; the interactive verify gate stays plugin-level; explorer journal added to P2; transfer semantics recorded as the constraint on root choice; `run_node` ruled out for spawn-now/collect-later by its own contract.

---

## 3. Comparison

| Dimension | R — Rebuild | M — Migration |
|---|---|---|
| Effort to a shippable 2.14 system | 11–15 wk | 3–5 wk |
| `main` during the work | frozen or double-maintained | keeps shipping; small merges |
| Regression risk | unknown unknowns; rediscovery by dogfood | known unknowns — the §1 touchpoint inventory; each change A/B-gated |
| Reversibility | one point: cutover | per phase, per workaround (branch + A/B) |
| Knowledge preservation | tests ported; 52 annotations and users' habits must be re-learned | tests *are* the gates; annotations stay attached to the code they explain |
| Upstream-semantics exposure | bets the product on them before measuring | P0/P1 measure first; adopt only what passes |
| End-state architecture | `LlmAgent` root + plugins + broker + journal — **the same** as M (per §0) | same |
| End-state code quality | cleaner layering, no shim | shim module + legacy until P5 sweeps (each a small PR) |
| Skills (client's daily path) | changes wholesale to upstream environment mode | option (a) keeps behavior; (b) only on measured win |
| Data compatibility | must be designed in (R0/R4) | inherited |
| Ability to stop halfway | nothing usable until R3/R4 | every merged phase is a usable system |

The decisive row is **end-state architecture**: the agreement established that the correct 2.x architecture for an interactive harness is the one adk-cc already has. A rebuild therefore converges to the migration's end state by a path three to four times longer, paying for the convergence with a cutover and a regression tail.

---

## 4. When R would be the right call

- If the target architecture differed from the current one — it does not (§0).
- If the ADK-coupled layer were mostly glue — it is mostly *policy* (permissions, confirmation UX, memory capture, verify ladder, plan mode), and the policies do not change on 2.x.
- If 1.31 → 2.14 had broken public APIs broadly — it did not: all 34 imported modules exist, hooks are additive, the break is 8 private touchpoints plus the skills tool contract.
- If a new product shape were wanted (e.g. an unattended agent service built around `Workflow`) — that is a *second app* next to this one, not a rebuild of this one.

## 5. Recommendation and decision triggers

**Migration (M)**, with the three strangler rebuilds it already contains — Turn Broker on the upstream journal (P2), the skills decision (P3), the server (P4) — and the legacy sweeps as separate cleanup PRs after merge.

Revisit toward R only if P0/P1 produce one of these:
1. the failure inventory shows behavior-class failures in **public**-API usage outside the §1 touchpoint list (i.e. our model of the break is wrong);
2. the P1 A/B shows upstream resume/sealing semantics incompatible with the confirmation UX in a way that forces a different broker design rather than a parameter;
3. skills option (a) is impossible (the 2.14 tool contract cannot carry workspace-relative execution) *and* option (b) cannot host the trust gate.

None of these is expected; all three are cheap to detect in the first two weeks.

## 6. Next step

P0 on branch `migrate/adk-2.14`: pin 2.14, run the suites, produce the failure inventory, land `compat/adk.py`. Deliverable: the inventory appended to `adk2-migration-spec.md` §7, and the P0 measurements sent to analysis-docs for its limitations section.
