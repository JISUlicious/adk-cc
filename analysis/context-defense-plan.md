# Context-defense plan: no turn dies of context, no finding dies of eviction

Status: PLAN (2026-08-02). Follow-up to the mid-turn overflow incident
(session `6a153d6c`, "AI Agent Framework Landscape") and the fixes already
shipped for it. Task #100 traced the root cause; this plan closes the class.

## Measured facts (all live-verified 2026-08-02)

| Fact | Value | How measured |
|---|---|---|
| gpt-5.4-mini input window (chatgpt-codex) | **~272k tokens** (400k total − 128k output/reasoning reserve) | bisection probe; last success at 269,853 server-counted prompt tokens |
| chars/4 estimator bias on web payloads | **~1.4× optimistic** | 833,675 chars ≈ 289k server tokens |
| Incident request | ~833KB replayed fetch results ≈ 289k tokens | saved session + server usage |
| Ladder in production | MAX 200k · WARN 150k · REJECT 190k · compaction threshold 140k | `.env` + boot log |
| ADK transfer semantics | a sub-agent's FULL trajectory re-enters the parent's request as text (`[Explore] \`web_fetch\` tool returned result: …`) | `flows/llm_flows/contents.py _present_other_agent_message` |
| Spawned explorers | only the REPORT text crosses to the parent | `tools/subagents.py` nested-Runner design |

## Failure modes → layers

| # | Failure mode | Defense | Status |
|---|---|---|---|
| F1 | Mid-turn burst inside one agent's own calls | L3 summarize-then-evict at the reject line | evict shipped; summarize is P0 |
| F2 | Sub-agent's raw trajectory replayed into the parent at handback (the incident) | L1 handback substitution | **P1 (core of this plan)** |
| F3 | Oversized inherited history poisons every later turn (resume/retry/continue) | L2 pre-invocation compaction | P2 |
| F4 | Decisions keyed on stale usage / text-only counts | payload-aware `estimate_request_tokens` at every decision point | shipped for the guard; L2 reuses it |
| F5 | Compaction churn & duplicate dividers | marginal-content floor + UI dedup | shipped (`1372913`) |
| F6 | A spawned child blowing its OWN window | guard inherited by children + per-child deadline | shipped |
| F7 | Overflow invisible to the user | error-event → turn ERROR + Retry; retry countdown | shipped (`245b42b`, `4face97`) |
| F0 | The haul happening at all | routing: explicit-parallel asks → `spawn_explorers` (report-only crossing) | instruction sharpening shipped; see P1 note on demoting transfer |

## The shared engine (P0): cached per-result summarization

One module powers L1 and L3 — `agents/adk_cc/context/result_summaries.py`:

- **Window = one tool result.** Each result is bounded by the tool's own cap
  (~60KB ≈ 20k tokens), so every summarization fits any summarizer window.
  No map-reduce, no chunking machinery. This is the "windowed
  summarization" idea made concrete.
- **Digest cache.** Summaries keyed by content hash (sha256 of the payload
  bytes), computed once ever. Session events are immutable, so the same
  payloads recur on every later call — cache turns per-call cost into
  one-time cost. In-memory LRU (bounded count+bytes) with write-through to
  `DATA_DIR/context/summaries/` so restarts don't re-pay.
- **Retrieval-oriented prompt.** Not "summarize": *"preserve facts, numbers,
  names, URLs, verbatim identifiers, and anything answer-relevant; drop
  boilerplate, navigation, markup."* The summaries exist to keep FINDINGS
  alive — the whole objection to mechanical eviction.
- **Model + budget.** `ADK_CC_COMPACTION_MODEL` (already a configurable,
  typically cheaper endpoint), concurrent wave capped by a small semaphore,
  per-call timeout riding the existing compaction timeout/breaker pattern.
- **Degradation.** Summarizer failure/timeout on a result → that result
  falls back to today's mechanical eviction note. REJECT stays the floor.
  Nothing ever does worse than current behavior.

## L3 (P0): summarize-then-evict at the reject line

Upgrade `_evict_tool_results`: for each eviction candidate (same selection —
oldest first, newest 2 kept, prose never touched), substitute a cached
summary instead of an eviction note; only on summarizer failure use the
note. Request-side only, session untouched — same safety envelope as
today's eviction.

## L1 (P1): handback protection — the sub-agent cannot blow up the parent

The user-named requirement: compaction at the END of the sub-agent's turns,
so its haul never reaches the parent's context.

- **Mechanism: request-side substitution, not session mutation.** A
  `before_model_callback` (in ContextGuard, reusing the P0 engine) detects
  ADK's foreign-tool-result text parts (`_FOREIGN_RESULT_RE`, already
  shipped for eviction) **above a low per-result threshold (default 8KB)**
  and substitutes cached summaries — at EVERY parent call, not just the
  reject line. The parent then never sees a sub-agent's raw haul, only
  distilled results + the sub-agent's own final text. Injecting a
  compaction event mid-invocation (the "at handback" moment) is avoided
  on purpose: ADK is appending events concurrently and its semantics for
  mid-invocation compaction ranges are unverified.
- **Durable write-through (P1.5, gated on a spike).** Post-invocation —
  quiescent session, safe timing — append a real compaction event covering
  the sub-agent's event range whose content is assembled from the cached
  summaries (no second LLM pass). The session itself then shrinks durably;
  the UI already renders compaction events. Spike first: verify ADK's
  contents processor honors a compaction range that covers only a
  mid-session span.
- **Same-agent bursts** (Explore's own next call, coordinator's own fetch
  streak) get the identical treatment via `function_response` parts —
  the substitution applies to both shapes the eviction code already knows.
- **Routing note.** With L1 in place the transfer path stops being
  dangerous, only wasteful. Still worth a small P1 follow-up: measure
  whether research-shaped asks should prefer `spawn_explorers` by
  DEFAULT (transfer reserved for codebase exploration where results are
  small), since report-only crossing beats summarized crossing.

## L2 (P2): pre-invocation compaction

Once per turn, before the first model call (`before_run`):

- Trigger: **payload-inclusive** estimate over the events that would be
  replayed (never the usage shortcut — F4). Fire above the compaction
  threshold (140k).
- Action: invoke the existing summarizer stack (`_LazyAdkCcSummarizer`,
  timeout + breaker + churn floor) over everything but the newest few
  events; append a normal compaction event. Session is quiescent — the safe
  insertion point.
- This closes F3 (the incident session was unrecoverable pre-fix: stale
  usage 76,349 < 140k meant post-turn compaction would NEVER fire while
  every turn replayed 289k). With L2 the first turn in an oversized session
  pays seconds of summarization instead of dying or leaning on eviction.

## L4 (P3): observability honesty

- Turn snapshot gains `context_estimate` = max(last usage, measured
  projection) so the gauge can stop under-reporting (the incident gauge
  said 38% while the wire carried 145%).
- Guard EVICT/substitute/compact decisions emit audit events
  (`context_evict`, `context_substitute`, `context_precompact`) via the
  existing audit sink.

## Phasing & verification

| Phase | Deliverable | Verification |
|---|---|---|
| P0 | result_summaries engine + summarize-then-evict | unit: cache hit/miss, fallback-to-evict, prompt shape; incident-replay test upgraded to assert summaries, not notes |
| P1 | foreign-result substitution at every parent call (8KB threshold) | incident-replay: parent request carries summaries only; live e2e: re-run the framework-landscape ask via transfer, confirm turn completes with findings intact |
| P1.5 | durable write-through compaction event (post-invocation) | spike test on ADK range semantics first; UI shows one divider; session shrinks on disk |
| P2 | pre-invocation compaction | unit with oversized fake history; live: reopen a heavy session, first turn compacts then answers |
| P3 | gauge honesty + audit events | gauge shows >100% on a poisoned session pre-compaction; audit lines present |

Env surface (minimal): `ADK_CC_RESULT_SUMMARIES=1` default ON with
kill-switch, `ADK_CC_RESULT_SUMMARY_MIN_CHARS` default 8192. Everything else
rides existing knobs (compaction model/timeout/threshold).

## Open decisions

1. P1 default-on vs opt-in for one release. (Recommend: on — it only
   activates above 8KB per result, and degrades to today's behavior.)
2. Demote transfer-Explore for research-shaped asks once L1 lands, or leave
   routing to the sharpened instruction. (Recommend: measure first — rerun
   the routing battery after L1 and decide on data.)
3. P1.5 ship/skip after the spike. (Recommend: ship only if the spike is
   clean; request-side substitution alone already meets the requirement.)
