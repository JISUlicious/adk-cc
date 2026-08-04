# Plan: Session context-compaction indicator

Status: planned (not started). Decision: build **full** P1 + P2 + P3
(inline marker + fullness gauge + live status/history). Separate task from the
knowledge graph.

## What this is
SESSION CONTEXT compaction — the conversation-window summarization that shrinks
event history during a long chat. NOT wiki/memory compaction. Today it happens
silently; this surfaces it in the chat UI.

## Grounding (from code survey)
- **Mechanism**: ADK's `EventsCompactionConfig` (`agents/adk_cc/agent.py:802`)
  runs `LlmEventSummarizer` **post-invocation** when tokens exceed
  `ADK_CC_COMPACTION_TOKEN_THRESHOLD`. `ContextGuardPlugin`
  (`plugins/context_guard.py`) separately WARN/REJECTs near
  `ADK_CC_MAX_CONTEXT_TOKENS` (warn 75% / reject 95%, overridable).
- **Signal that already reaches the client**: a compaction is recorded as an
  `Event` with `actions.compaction` = `{startTimestamp,endTimestamp,
  compactedContent}` (ADK `event_actions.py`). The SSE consumer
  (`web/src/api/sse.ts`) already passes `actions` through. So the "compacted
  here" marker needs **zero backend**.
- **Server-only signals (not exposed yet)**: audit events
  `compaction_triggered|success|failure` (`plugins/audit.py`,
  `emit_compaction_event`); per-event `usage_metadata.prompt_token_count` (on
  model events, arrives over SSE); `ADK_CC_MAX_CONTEXT_TOKENS` (env, server-side).
- **Renderer**: `web/src/components/Thread.tsx` `flattenEvents()` →
  `ChatRow` kinds (text/thought/function_response/tool_pair/artifact). No
  compaction handling today.

## P1 — inline "compacted here" marker (client-only, no backend)
- `Thread.tsx`: add `ChatRow` kind `"compaction"`. In `flattenEvents`, when an
  event carries `actions.compaction`, emit a compaction row at that position.
- Render a collapsible divider: "⊟ Context compacted — summarized older
  messages", expandable to show `compactedContent` (+ start/end timestamps).
- `sse.ts`: ensure `actions.compaction` (camelCase `compactedContent` etc.) is
  typed on `RunEvent`.

## P2 — context-fullness gauge (small backend)
- New `GET /api/context/limits` → `{max_tokens, compaction_threshold,
  warn_tokens, reject_tokens}` (reads the env knobs). Lets the client show the
  denominator it can't otherwise know.
- Client: track the latest `usage_metadata.prompt_token_count` from streamed
  events; render a header/composer gauge = current/max. Amber at the warn ratio,
  red near reject — so the user SEES compaction coming and understands a REJECT.

## P3 — live "compacting…" + history (more backend)
- Expose the audit compaction trail: `GET /api/context/compaction-status?
  app=&user=&session=` → last `compaction_triggered`/`success`/`failure` for that
  session (read from the audit log; reuse the audit store). Returns
  `{state:"idle"|"running"|"failed", started_at, finished_at, event_count,
  elapsed_ms}`.
- Client: briefly poll status after a turn completes (compaction is
  post-invocation, so the window is right after the response). Show a transient
  "compacting…" spinner while `running`; on transition to `idle`, a toast
  "Context compacted — N messages summarized" and a small "last compaction"
  line. History view = the compaction rows already in the thread (P1) +
  optionally the changelog of statuses.
- Note: "running" is brief (one summarizer call); if it's too fast to observe,
  the spinner may rarely show — that's fine, the marker (P1) + toast are the
  durable signals. Don't fake a delay.

## Verification
- P1: unit-render Thread with a synthetic event carrying `actions.compaction` →
  asserts a compaction row renders + expands. Playwright: drive a session past
  the threshold (set a low `ADK_CC_COMPACTION_TOKEN_THRESHOLD` in a test server)
  → assert the divider appears.
- P2: `GET /api/context/limits` returns the configured env values; gauge math
  unit-tested; amber/red thresholds.
- P3: `compaction-status` returns the right state transitions from a seeded
  audit log; live e2e (low threshold) → spinner→toast observed.

## Sequencing within the task
P1 (quick, client-only) → P2 (limits endpoint + gauge) → P3 (status endpoint +
live spinner/history). Each phase is independently shippable.

## Risks / honesty
- The "ongoing" state is inherently fleeting (post-invocation, single call). P1's
  durable marker + P3's toast carry the weight; the spinner is best-effort.
- Token counts are estimates when the model doesn't return usage_metadata
  (ContextGuard falls back to chars/4) — label the gauge "≈".
