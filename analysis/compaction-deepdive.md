# Claude Code compaction: full flow + tricks, mapped to adk-cc

Reverse-engineered from `claude-code-leak/src/services/compact/*` (+ QueryEngine,
forkedAgent, messages). This is the deep-dive behind the prompt plan: compaction
in CC is a **layered system**, not one summarize call.

## The layered stack (cheapest → heaviest), run per query (query.ts:401-468)
1. **Snip** — evict old history at read-time / projection (`projectSnippedView`).
2. **Microcompact** — surgically clear large TOOL RESULTS (not the conversation):
   COMPACTABLE_TOOLS = read/bash/grep/glob/web/edit/write. Replace body with
   `[Old tool result content cleared]`, keep the last `keepRecent` (def 5), keep
   tool_use↔tool_result pairing. Two paths: client time-based (fires when >60min
   gap → server cache already dead, microCompact.ts) and server-side API
   strategies (`clear_tool_uses_20250919` / `clear_thinking_20251015`, trigger
   180k → target 40k, apiMicrocompact.ts). Runs BEFORE the model call.
3. **Context collapse** — read-time dedup/projection (feature-gated).
4. **Session-memory compaction** — a persistent markdown "session notes" file is
   extracted DURING the conversation (sessionMemory.ts; triggers at 10k init /
   5k-delta / ≥3 tool calls) and, at compaction, **replaces the summary call
   entirely — no LLM at compact time**. `lastSummarizedMessageId` is the split
   point: messages before it are covered by the notes (dropped), after it kept.
5. **Full compaction** — the LLM 9-section summary (the prompt we already
   analyzed), as the recovery path.

## Trigger & thresholds (autoCompact.ts) — layered buffers + hysteresis
- `effectiveWindow = contextWindow(model) − reservedForSummary` where
  `reservedForSummary = min(modelMaxOutput, 20_000)` — **reserve output headroom
  so the summary itself can't overflow**.
- Ladder (buffers from the limit): **warn @ 20k → autocompact @ 13k → blocking @
  3k** (manual /compact blocked @ 3k). Warning fires early, autocompact is the
  backstop, blocking is the hard floor → hysteresis, no thrash.
- **Circuit breaker**: stop after 3 consecutive autocompact failures.
- **Warning suppression**: once-per-threshold state (compactWarningState), reset
  at each attempt, suppressed after success — no per-turn spam.
- Suppressed for query sources `session_memory`/`compact`/context-agent, and
  under REACTIVE/CONTEXT_COLLAPSE modes.

## Full-compaction orchestration (compact.ts compactConversation)
1. `executePreCompactHooks()` → gather **custom compact instructions**.
2. `getCompactPrompt(custom)` → no-tools preamble + 9-section + trailer.
3. **Model call, two paths**: (A) `runForkedAgent({maxTurns:1, no tools})` that
   **reuses the parent's prompt cache** via identical cache-key params (massive
   token save); (B) streaming fallback if the fork yields no text.
4. **PTL retry loop**: if the model says "prompt too long", drop the oldest
   API-round groups (`truncateHeadForPTLRetry`) and retry (≤3).
5. `formatCompactSummary` strips `<analysis>`, unwraps `<summary>`.
6. **compact_boundary** system message inserted; pre-compact history flushed to a
   **transcript file on disk**; live context trimmed to boundary-onward
   (`getMessagesAfterCompactBoundary`, splice for GC).
7. **Post-compact re-attachment** (minimize information loss): top-5 recent files
   (50k budget / 5k each, dedup vs preserved tail + FILE_UNCHANGED stubs), skills
   (25k), tool schemas (`preCompactDiscoveredTools` in boundary metadata), agent
   listing, MCP instructions, session metadata re-appended to the tail window.
8. `getCompactUserSummaryMessage` wraps the summary with **continuation framing**
   ("This session is being continued… resume directly") + a **transcript pointer**.

## Partial / snip (compact.ts partialCompactConversation)
- `up_to`: summarize the PREFIX, keep recent verbatim (summary becomes new prefix).
- `from`: summarize the TAIL, keep earlier (cache stays valid).
- `preservedSegment {headUuid, anchorUuid, tailUuid}` annotates the boundary;
  flush through tailUuid before writing so resume can't point at an unwritten msg.

## Invariants & grouping (grouping.ts, sessionMemoryCompact.ts)
- Group by API round; **never split [thinking, tool_use, tool_result]** across the
  keep boundary (`adjustIndexToPreserveAPIInvariants`); floor keep-index at the
  last boundary; keep-window min 10k tokens / 5 msgs, max 40k.

## Post-compact cleanup (postCompactCleanup.ts)
Reset microcompact state, context-collapse (main-thread only), user-context +
memory-file caches, system-prompt sections, classifier approvals, speculative
bash checks, tracing, file-content + session-message caches. **Preserve invoked
skills** (re-injected). Centralized so all compaction paths behave identically.

## Notable single tricks
- Image/document stripping before summarizing (avoid PTL on the summary call).
- Strip reinjected skill attachments before summarizing (re-surfaced anyway).
- `willRetriggerNextTurn` metric — detect a compaction that didn't free enough.
- Rich telemetry: cacheHitRate, true post-compact tokens, recompaction-in-chain.

---

# Mapping to adk-cc (have / gap / N-A)

adk-cc today = ADK `EventsCompactionConfig` (sliding window: token_threshold,
event_retention_size, compaction_interval, overlap_size) + `LlmEventSummarizer`,
wrapped by `_LazyAdkCcSummarizer` (audit, timeout, graceful-degrade) +
`ContextGuardPlugin` (warn/reject, does NOT compact).

| CC flow / trick | adk-cc | Notes |
|---|---|---|
| Whole-window LLM summary | **HAVE** | ADK windowing + our wrapper. |
| Tool-pair / thinking invariants, boundary in store | **HAVE (ADK)** | ADK retention/overlap + session events handle this. |
| Graceful degrade on failure | **HAVE** | timeout/exception → return None (uncompacted turn). |
| **Microcompaction** (evict large tool results, keep pairs + recent-N) | **GAP — biggest** | ADK has no tool-result eviction tier; for a coding agent, bash/read/grep outputs dominate context. No equivalent. |
| **Richer 9-section prompt + analysis-strip** | **GAP (planned)** | see `compaction-prompt-plan.md`. |
| **Reserve output headroom + coherent warn/compact/block ladder** | **PARTIAL** | ContextGuard warn/reject and ADK's compaction threshold are independent knobs; no single ladder, no reserved-for-summary math. |
| **Bridge memory/wiki INTO compaction** (session-memory-as-summary) | **GAP — strategic** | We already have autonomous memory + wiki; CC's pattern (distilled notes replace/seed the summary) is exactly what those could feed. Not wired. |
| Continuation framing + transcript pointer in the summary | **GAP** | ADK EventCompaction content is bare summary; no "resume directly" wrap, no link. (UI: CompactionDivider could surface a pointer.) |
| Warning suppression / once-per-threshold | **PARTIAL** | ContextGuard logs every turn; P2 gauge is the UI analog. |
| Custom per-project compact instructions | **GAP (minor)** | CC injects `## Compact Instructions`. |
| Circuit breaker (3 consecutive fails) | **GAP (minor)** | we degrade per-call, no streak guard. |
| PTL retry (truncate + retry) | **GAP (minor)** | ADK likely doesn't; our path just returns None. |
| Forked-agent prompt-cache reuse | **N/A** | ADK summarizer is a separate LiteLlm call; provider/cache-dependent. |
| Server-side `clear_tool_uses` API strategy | **N/A-ish** | provider-dependent (our glm/LiteLlm endpoint). |
| Post-compact cache cleanup | **N/A** | different arch (ADK owns session events). |
| Image stripping pre-summary | **GAP (minor)** | our events may carry artifacts. |

## Ranked opportunities for adk-cc
1. **Microcompaction tier** — a before-summary pass that truncates/evicts large
   tool-result events (bash/read/grep) with a stub, keeping the last N and the
   pairing. Highest leverage; genuine architectural gap; complements (doesn't
   replace) ADK's summarizer.
2. **Richer prompt + analysis-strip** — already planned; smallest, ships now.
3. **Bridge memory→compaction** — seed the compaction summary with the user's
   recalled semantic memory (and/or relevant wiki), or skip re-summarizing what
   memory already holds. Strategic; reuses work already built.
4. **Coherent threshold ladder + reserve-for-summary** — unify ContextGuard
   warn/reject with the compaction threshold; reserve output headroom.
5. **Continuation framing + transcript pointer** (summary text + CompactionDivider
   link) — low effort, better resumes + UX.
6. **Robustness**: circuit breaker, PTL-style retry, image strip — minor.

## Honest caveats
- ADK already covers windowing/invariants/boundaries — don't reimplement those.
- Several CC tricks are CLI/Anthropic-API specific (prompt-cache fork, server
  clear_tool_uses) and don't port to our LiteLlm/glm path.
- Microcompaction is the big idea worth porting; the prompt is the cheap win;
  the memory-bridge is the interesting strategic bet.
