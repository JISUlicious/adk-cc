# Gap analysis: large tool-call arguments / truncated tool-call JSON
### adk-cc vs Claude Code (the leak at `../src`)

## Problem
When the agent must produce a large payload through a tool call (a whole file
via `write_file(content=…)`, a big `edit_file` block), the model has to emit that
payload as **one JSON string** inside the tool call's `function.arguments`. On
adk-cc's OpenAI-compatible (LiteLLM) path that whole string is parsed with
`json.loads`, so it fails two ways:

1. **Escaping** — every `\`, `"`, control char across a huge string must be
   JSON-escaped; small/quantized local models slip → malformed JSON.
2. **Token budget** — the payload must fit the turn's output-token budget; exceed
   it → the stream stops mid-string → truncated JSON → the turn used to crash.

adk-cc already shipped a recovery net for this: `plugins/tolerant_tool_json`
(repairs + closers-completion), the Tier-2 marker + `TruncatedToolCallPlugin`
(truncation → clean in-turn retry), and a configurable `max_output_tokens`
(`models/selectable.py`). The question this doc answers: **how does Claude Code
handle the same thing, and what is adk-cc still missing?**

## What Claude Code does (the leak)

### File tools — same shape as adk-cc
- `FileWriteTool`: whole-file `Write(file_path, content)` — `content` is an
  unbounded `z.string()`, no size cap (`FileWriteTool.ts:56`). Whole-file atomic
  write.
- `FileEditTool`: single `old_string`/`new_string` `str_replace` (+ `replace_all`)
  — file-size cap 1 GiB, but no cap on the strings themselves (`types.ts:6`).
- `NotebookEditTool`: one cell at a time.
- **No append, no chunked write, no multi-edit.** Identical to adk-cc.
- Guidance points **toward** Write, **away** from scripts: BashTool prompt —
  *"Write files: Use Write (NOT echo >/cat <<EOF)"* (`BashTool/prompt.ts:287`).
  The Write prompt: *"Prefer the Edit tool … it only sends the diff. Only use
  this tool to create new files or for complete rewrites."* (`prompt.ts:10`).

➡️ **Conclusion: the tools are NOT the gap.** An earlier draft proposed an
`append_file` tool + "generate large artifacts via scripts" — both *diverge* from
Claude Code and are dropped.

### Model/tool-call layer — this is the difference
- **max_tokens is ALWAYS set.** Default `CAPPED_DEFAULT_MAX_TOKENS = 8_000`,
  escalatable to `ESCALATED_MAX_TOKENS = 64_000`, env `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
  (`utils/context.ts:24`, `claude.ts:1591/1715`).
- **`stop_reason === 'max_tokens'` drives a 3-tier recovery** (`query.ts:1185-1256`):
  1. **escalate** the cap to 64k and **retry once**, transparently;
  2. if still truncated, inject *"Output token limit hit. Resume directly…"* and
     retry up to **3×** (a continuation loop);
  3. then surface the error.
- **Streamed tool inputs accumulate as a plain string** (`input_json_delta` →
  `contentBlock.input += delta.partial_json`, `claude.ts:2111`) and are parsed
  **once** at `content_block_stop` — no mid-stream parsing, **no tolerant/partial
  JSON** (a strong model + the Anthropic protocol don't need it).
- Invalid/incomplete input → Zod `safeParse` → a `tool_result` `is_error:true`
  fed back → the model self-corrects in-turn (`toolExecution.ts:615`).

## The gaps (adk-cc vs Claude Code)

| Dimension | Claude Code | adk-cc (before this fix) | Gap |
|---|---|---|---|
| Default `max_tokens` | always set (8k) | knob exists, **unset by default** → endpoint default (often low) | **set a default** |
| `max_tokens` recovery | escalate 8k→64k + resume loop (3×) | none; Tier-2 marker only makes the model resend at the **same** cap | **escalate on truncation** |
| Streamed tool-input parse | clean string concat, parse once | ADK test-parses every delta + tolerant shim → premature-completion risk | weak-model band-aid CC doesn't need |
| Model/protocol | strong Anthropic model + structured deltas | small local models + `function.arguments` string | inherent — justifies extra defense |

## Decision — what to build
Keep tolerant-json + the Tier-2 marker (a justified adaptation to weaker models on
the OpenAI protocol — CC doesn't need it, adk-cc does). Fill the two gaps adk-cc
genuinely has that Claude Code has:

1. **Default `max_tokens`.** `resolve_max_output_tokens` now defaults to **8192**
   (≈ CC's 8000) when neither a per-endpoint nor `ADK_CC_MAX_OUTPUT_TOKENS` is set.
   `ADK_CC_MAX_OUTPUT_TOKENS=0` opts out (uncapped). Uses the constructor
   `max_tokens` param (universally supported by local OpenAI-compat servers,
   unlike ADK's `config → max_completion_tokens` mapping).
2. **Escalation on truncation.** On `finish_reason=MAX_TOKENS`, `SelectableLlm`
   escalates the effective cap to **`ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED`** (default
   **32768**; cf. CC's 64k) for subsequent calls by rebuilding its delegates at
   the higher cap — covering both the registry endpoints and the boot delegate.
   Monotonic-sticky for the process (only ever raises the cap; self-relaxing was
   skipped to stay concurrency-safe). The model's Tier-2 resend then has the
   headroom CC's escalation provides.
3. **Prompt nudge.** Mirror CC's Edit guidance: prefer `edit_file` (sends only the
   diff) over `write_file` (whole rewrite) for modifying existing files — reduces
   whole-file re-emits on edits.

### Not done (and why)
- **No `append_file` / chunked write** — Claude Code doesn't; it would add surface
  CC deliberately avoids.
- **No "generate via script" steering** — CC steers the opposite way.
- **Streaming-parse scoping (Layer D)** — left as a follow-up; the escalation +
  default reduce how often truncation reaches that path.

### Feasibility note
CC implements escalation/resume in **its own** query loop (`query.ts`). adk-cc
runs inside ADK's runner, so a transparent *mid-stream* re-issue (CC's tier-1) is
not possible — partial output is already streamed to the UI. The adk-cc analogue
is a **sticky cap escalation** applied to the next call (which is the model's
Tier-2 resend), plus a generous default — same outcome (the big write succeeds on
the retry with more room), achieved within the runner's constraints.
