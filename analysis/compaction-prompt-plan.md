# Plan: richer context-compaction summary prompt (+ analysis-strip)

Status: planned (not started). Goal: replace ADK's one-sentence default
compaction summary with a structured, high-fidelity prompt adapted from Claude
Code's (`claude-code-leak/src/services/compact/prompt.ts`), and strip the
`<analysis>` scratchpad so it doesn't bloat the context compaction is meant to
shrink.

## Why
Our `_make_compaction_summarizer` (agents/adk_cc/agent.py) plumbs a
`prompt_template` field through `_LazyAdkCcSummarizer` but **never sets it**
(factory at ~771 omits it), so ADK's `LlmEventSummarizer._DEFAULT_PROMPT_TEMPLATE`
is used — a single sentence ("summarize… concise… capture the essence"). Result:
compaction loses files/code/errors/user-corrections/next-step. Claude Code uses a
9-section summary with an `<analysis>` pass; that fidelity is what lets a session
resume cleanly (the summary that opened THIS session is in that format).

## Design

### 1. Default prompt constant (adapted, domain-agnostic)
Add `_ADKCC_COMPACTION_PROMPT` — CC's BASE structure, trimmed to fit ADK's
interpolation contract:
- Sections: Primary Request & Intent · Key Technical Concepts · Files & Code
  Sections (snippets) · Errors & fixes · Problem Solving · All user messages ·
  Pending Tasks · Current Work · Optional Next Step (verbatim quotes).
- Keep the `<analysis>…</analysis>` scratchpad then `<summary>…</summary>`
  (analysis improves quality; we strip it after — step 3).
- MUST end with the literal `{conversation_history}` placeholder (ADK calls
  `template.format(conversation_history=...)`).
- **Brace gotcha**: `str.format` chokes on any other literal `{`/`}`. The
  adapted prompt must contain NO stray braces (CC's uses `[...]`/`<example>`, no
  braces — safe). Add a guard/test asserting the template has exactly the one
  placeholder.
- Drop CC's no-tools preamble/trailer: ADK's summarizer call is a bare LiteLlm
  with no tools attached, so the wasted-turn failure mode doesn't apply here.
  (Note it as intentionally omitted.)

### 2. Wire prompt resolution (env override)
In `_make_compaction_summarizer` factory, resolve:
- `ADK_CC_COMPACTION_PROMPT` (inline string) — operator override; else
- `ADK_CC_COMPACTION_PROMPT_FILE` (path) — read file; else
- `_ADKCC_COMPACTION_PROMPT` default.
Pass as `prompt_template=` to the wrapper class. If the resolved template lacks
`{conversation_history}`, append `\n\n{conversation_history}` (don't silently
produce a history-less prompt).

### 3. Analysis-strip post-process (mirror formatCompactSummary)
ADK returns the model text verbatim as `EventCompaction.compacted_content` (a
Content). Without stripping, the `<analysis>` scratchpad lands in context —
defeating compaction. After `result` is obtained in `maybe_summarize_events`
(agent.py ~681, before `return result`), add `_strip_analysis(result)`:
- locate `result.actions.compaction.compacted_content` (Content with `parts[].text`;
  tolerate str / mock shapes like `_summary_bytes` does),
- on each text part: remove `<analysis>…</analysis>` (non-greedy, DOTALL);
  unwrap `<summary>…</summary>` → its inner content with a `Summary:` header;
  collapse blank runs.
- If no `<summary>` tag (weak model didn't follow structure) → keep the raw text
  minus any analysis block. Never return empty (fall back to original text).
- Recompute `summary_bytes` AFTER stripping so the audit reflects what actually
  enters context.

## Files
- MODIFY `agents/adk_cc/agent.py`: add `_ADKCC_COMPACTION_PROMPT`,
  `_resolve_compaction_prompt()`, `_strip_analysis(event)`; set `prompt_template`
  in the factory; call strip before `return result`.
- MODIFY `.env.example`: document `ADK_CC_COMPACTION_PROMPT` /
  `ADK_CC_COMPACTION_PROMPT_FILE`.
- TEST `tests/test_compaction_prompt.py` (model-free): `_strip_analysis` removes
  analysis + unwraps summary on a synthetic Event; idempotent; no-tags passthrough;
  empty-guard. Prompt resolution: default vs inline vs file; placeholder
  auto-append; brace-safety (template.format succeeds).
- EXTEND `tests/e2e_compaction_signal.py` (live): assert the compactedContent now
  contains section headers (e.g. "Primary Request" / "Key Technical Concepts")
  and contains NO "<analysis>" — proves prompt applied + strip worked.

## Verification
1. Unit (model-free): strip + resolution + brace guard.
2. Live: low-threshold server → compactedContent is structured, analysis-free.
3. UI: re-screenshot the CompactionDivider — the expanded summary now shows the
   richer sections (nice side benefit of the P1 work).

## Risks / honesty
- **Bigger summaries**: a 9-section summary is larger than the one-liner, so on
  SMALL conversations compaction saves less. Still far smaller than full history,
  and the win is fidelity. Acceptable; the prompt asks for concision where it can.
- **Weak compaction models** may ignore the structure → strip must degrade (keep
  raw, drop analysis only). Covered by the no-tags passthrough test.
- **Brace collisions** in custom operator prompts → the brace-safety test + the
  placeholder-append guard contain it; document the constraint in .env.example.
- Scope: this only changes the SUMMARY CONTENT. The compaction trigger/mechanism
  (EventsCompactionConfig, ContextGuardPlugin) is unchanged.

## Not in scope (defer)
- PARTIAL / UP_TO variants (CC has 3; ADK's windowed path may not need prompt
  awareness — revisit only if windowed summaries read wrong).
- Continuation framing / transcript pointer / per-project compact instructions
  (CC's `getCompactUserSummaryMessage`) — lower value here.
