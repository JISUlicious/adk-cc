# Compaction improvements — master plan (phased, ordered by importance)

Consolidates the CC deep-dive (`compaction-deepdive.md`) + the prompt plan
(`compaction-prompt-plan.md`) into one ordered program. Each phase is
independently shippable and gated by an env flag (default-off or
behavior-preserving), so nothing destabilizes the current path.

## Current baseline (what adk-cc has)
- ADK `EventsCompactionConfig` (sliding window: token_threshold,
  event_retention_size, compaction_interval, overlap_size) + `LlmEventSummarizer`,
  wrapped by `_LazyAdkCcSummarizer` (audit, timeout, graceful-degrade) — agent.py.
- `ContextGuardPlugin` (before_model): WARN/REJECT by token estimate; does NOT
  compact — plugins/context_guard.py.
- `MemoryPlugin` (before_model): budgeted recall injection every turn.
- UI: CompactionDivider renders `actions.compaction` (P1, shipped).
- ADK already owns windowing, tool-pair/thinking invariants, and the
  compaction boundary in the session store — we do NOT reimplement those.

## Guiding principles
- Each phase env-gated, default-off / behavior-preserving.
- Implement cross-cutting behavior as **plugins** (before_model), not agent
  callbacks (matches the project's plugins-over-callbacks stance).
- Model-light by default; any model call is opt-in and paced (rate-limit aware).
- Don't port CLI/Anthropic-API-specific tricks (prompt-cache fork, server
  `clear_tool_uses`) — N/A on the LiteLlm/glm path.

---

## Phase 1 — Richer summary prompt + analysis-strip   ★★★★★ (do first)
**Why first:** smallest change, highest value/effort, no deps; immediately
improves both the summary fidelity and what the CompactionDivider shows.
**What:** populate `_LazyAdkCcSummarizer.prompt_template` with a 9-section
prompt adapted from CC (Primary Request · Tech Concepts · Files & Code · Errors &
fixes · Problem Solving · All user messages · Pending Tasks · Current Work · Next
Step), keep the `<analysis>` scratchpad, then strip it post-hoc. Env override
`ADK_CC_COMPACTION_PROMPT` / `_FILE`.
**Details:** see `compaction-prompt-plan.md` (brace-safety, placeholder append,
strip-degrades-gracefully).
**Effort:** S. **Risk:** low. **Deps:** none.
**Verify:** model-free strip/resolution/brace tests; live low-threshold e2e
(structured + analysis-free); CompactionDivider re-screenshot.

## Phase 2 — Microcompaction tier (evict large tool results)   ★★★★★ (biggest gap)
**Why:** the highest-leverage architectural gap. For a coding agent, bash/read/
grep outputs dominate context; ADK has no tool-result eviction — only whole-window
summary. Evicting before summarizing reclaims the most tokens at zero model cost
and defers/avoids expensive summarization.
**What:** a `MicrocompactPlugin` (`before_model_callback`) that rewrites the
outgoing `llm_request.contents`:
- find `functionResponse` parts (tool results) for COMPACTABLE tools
  (run_bash / read_file / grep / glob / web_fetch / web_search / edit / write),
- estimate each result's size; keep the most recent `KEEP_RECENT` (default 5) and
  any below `MIN_TOKENS`; replace older/large ones' `response` with a stub
  (`{"status":"cleared","note":"[old tool result cleared — see session history]"}`),
- **never** touch the matching `functionCall` part (preserve pairing); never evict
  the active/last call's result.
- Per-request only (ADK rebuilds from session events each turn → re-evicts each
  turn; session history stays intact, which is the desired semantic).
**Env:** `ADK_CC_MICROCOMPACT=1`, `ADK_CC_MICROCOMPACT_KEEP_RECENT`,
`ADK_CC_MICROCOMPACT_MIN_TOKENS`.
**Runs before** ContextGuard's reject check and before ADK's summarizer threshold,
so it can keep a session under the bar without ever summarizing.
**Effort:** M. **Risk:** med (must keep responses valid dicts + pairing intact).
**Deps:** none (independent of P1).
**Verify:** model-free unit — synthetic LlmRequest with N tool results → old large
ones stubbed, recent/small kept, every functionResponse still has a matching
functionCall, response stays a dict. Live e2e — drive turns with big bash/read
outputs → assert prompt token estimate drops and the agent still answers; assert
ContextGuard WARN is deferred.

## Phase 3 — Bridge memory/wiki into compaction   ★★★★☆ (strategic)
**Why:** CC's session-memory pattern (distilled notes replace/seed the summary)
is exactly what our autonomous memory + wiki already produce — currently NOT wired
into compaction. Two wins: durable facts survive compaction even if the summarizer
drops them, and the summary needn't re-derive what memory holds.
**What (two parts):**
- (a) **Confirm recall-after-compaction**: MemoryPlugin recall runs every
  before_model, so durable facts are re-injected post-compaction already — add a
  test pinning this (it's the safety net), and ensure recall budget isn't starved
  right after a compaction.
- (b) **Seed the summary**: prepend a "Known durable context (memory)" block to
  the compaction summary so it's carried inside the boundary, not only re-injected.
  Needs tenant/user in the summarizer — plumb via session state (the summarizer
  currently only gets `events`; thread the principal through, or resolve from the
  events' invocation/session). Gate `ADK_CC_COMPACTION_SEED_MEMORY=1`.
**Effort:** M-L (mostly the plumbing in (b)). **Risk:** med. **Deps:** P1 (build on
the structured prompt). 
**Verify:** (a) unit — recall block present in the request after a compaction
event. (b) live — seeded summary contains a known semantic fact that wasn't in the
recent window.

## Phase 4 — Coherent threshold ladder + reserve-for-summary   ★★★☆☆
**Why:** today `ContextGuardPlugin` (warn/reject) and ADK's
`ADK_CC_COMPACTION_TOKEN_THRESHOLD` are independent knobs with no shared math; a
deployment can warn/reject without compaction ever firing first, or reserve no
output headroom.
**What:** in ContextGuard, compute `effective = max_context − reserve`,
`reserve = min(model_max_output, 20_000)` (CC's number); derive WARN/REJECT from
that; and document/auto-derive that the compaction threshold should sit *below*
WARN so compaction is the backstop before reject (the warn→compact→block ladder).
Add a post-compaction "still over threshold" WARN (CC's `willRetriggerNextTurn`).
**Env:** reuse existing `ADK_CC_MAX_CONTEXT_TOKENS` / `..._WARN_TOKENS` /
`..._REJECT_TOKENS` / `ADK_CC_COMPACTION_TOKEN_THRESHOLD`; add derivation when
unset.
**Effort:** S-M. **Risk:** low. **Deps:** ideally after P2 (the ladder matters
once microcompact is a tier under it).
**Verify:** unit — ladder math (warn<compact<reject<block ordering, reserve
subtracted); a config-sanity log line at startup.

## Phase 5 — Continuation framing + transcript pointer   ★★★☆☆ (UX)
**Why:** ADK's EventCompaction content is a bare summary; CC wraps it with "this
session is being continued… resume directly" + a transcript pointer, which
produces cleaner resumes and a better UX marker.
**What:** (a) prepend a short continuation preamble to the summary text in
`_strip_analysis`/post-process; (b) surface a "view full history" affordance in the
CompactionDivider (the full session events are retained in ADK's store → link/expand
to them). Ties into the compaction-indicator P3 (live status/history).
**Effort:** S. **Risk:** low. **Deps:** P1 (post-process hook), and the indicator
P2/P3 for the UI side.
**Verify:** summary text starts with the preamble; UI test the affordance.

## Phase 6 — Robustness polish   ★★☆☆☆
**Why:** small reliability/edge wins.
**What:** (a) **circuit breaker** — after K consecutive summarizer failures, skip
compaction for a cooldown (don't burn the rate-limited model retrying); (b)
**image/artifact strip** before summarizing (avoid bloating/PTL the summary call);
(c) PTL-style guard if the summarizer itself returns too-long (rare — ADK controls
the input window). 
**Effort:** S each. **Risk:** low. **Deps:** P1/P2.
**Verify:** unit — breaker trips after K fails and resets on success; strip removes
image parts.

---

## Recommended order & rationale
1. **P1** (prompt) — ship now; cheap, visible, unblocks P3/P5 post-process hook.
2. **P2** (microcompaction) — the real architectural win; independent; do next.
3. **P4** (ladder) — tune thresholds now that two tiers exist.
4. **P3** (memory bridge) — strategic; reuses memory/wiki; build on P1's prompt.
5. **P5** (framing + pointer) — UX polish; pairs with the indicator P2/P3.
6. **P6** (robustness) — last.

## Status: COMPLETE
- [x] P1 prompt + analysis-strip (e5ed975)
- [x] P2 microcompaction (90726ef)
- [x] P3 memory→compaction bridge (3270bcd)
- [x] P4 threshold ladder + reserve + enforcement (1f35e47, 4c50312)
- [x] P5 continuation framing + footer (69431ba)
- [x] P6 circuit breaker (image-strip N/A: ADK summarizer formats text only)
All env-gated/behavior-preserving; model-free unit suites + live e2es green.

## Importance summary
| Phase | Impact | Effort | Priority |
|---|---|---|---|
| 1 Prompt + strip | High | S | ★★★★★ |
| 2 Microcompaction | High | M | ★★★★★ |
| 3 Memory bridge | High | M-L | ★★★★☆ |
| 4 Threshold ladder | Med | S-M | ★★★☆☆ |
| 5 Framing + pointer | Med | S | ★★★☆☆ |
| 6 Robustness | Low | S | ★★☆☆☆ |

## Cross-cutting verification
Reuse the harnesses from the indicator work: `e2e_compaction_signal.py`
(data-path, tiny threshold) and `e2e_compaction_ui.py` (rendered divider).
Per-phase model-free unit tests for the deterministic logic (strip, eviction,
ladder math, breaker). Live e2es paced under `ADK_CC_MODEL_MAX_RPM`.

## Explicitly out of scope (N/A to adk-cc)
Prompt-cache forked-agent summary call; server-side `clear_tool_uses` /
`clear_thinking` API strategies; CLI post-compact cache cleanup; partial up_to/from
snip variants (ADK's windowing covers the need) — revisit only if a concrete need
appears.
