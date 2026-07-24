# Apple on-device Foundation Model (AFM) as an adk-cc model

Research + integration plan, 2026-07-24. Source docs:
`~/data/workspace/vibe/mac-model/apple-on-device-llm.md` (verified facts about
the platform). All measurements below were re-verified live on this machine
(MacBook Air M4 32 GB, macOS 26.5.2, gen-2 ~3B model, `apple-fm-sdk==0.1.1`
in a Python 3.12 venv — 0.2.1 does not build against Xcode 26.3's SDK).

## Measured results (probe suite, this machine)

| Probe | Result |
|---|---|
| Availability | `.available`, no setup, no API key, fully offline |
| Latency (trivial reply) | 0.84 s cold, **0.20 s warm** |
| Throughput | **~75 tok/s** decode (915 chars in 3.05 s); prefill roughly 0.5–1K tok/s |
| Context window | ~4K tokens TOTAL (prompt+instructions+response). Probe: ~4.5K-char-estimated prompt ok, ~5K → `ExceededContextWindowSizeError` |
| Tool calling | **Works.** SDK `Tool` subclass with a `@fm.generable` schema; model called `get_config(key='db_engine')` and composed the answer, 0.69 s total |
| Structured output | Works (`generating=` a `@fm.generable` class → typed value) |
| Concurrency | 2 parallel sessions both fine (0.4 s each); `ConcurrentRequestsError` exists but not hit at 2-way |
| Sampling knobs | `GenerationOptions(temperature=…, maximum_response_tokens=…)` |
| Guardrails | NO trips on realistic agent content: `rm -rf` explanation, OOM log summary, file-deletion codegen, security-incident text. Korean works |

### adk-cc task-fit quality (actual prompts)

| Task | Output | Verdict |
|---|---|---|
| Session title (SessionTitlePlugin-style) | `Market Anchor Fix` | Ship it |
| Memory capture (`_CAPTURE_PROMPT`) | Correct `TOPIC: x \| fact` lines; one spurious trailing `NONE` (already ignored by `_parse_facts`); minor topic mislabel | Usable as-is |
| Memory synthesis (synth prompt) | Correct newest-wins merge, kept specifics | Usable |

## The hard boundary

The coordinator instruction alone is **~3,380 tokens**; with ~25 tool schemas
and any history, a coordinator turn is 10–30K tokens. AFM's window is ~4K
**total**. The full agent CANNOT run on AFM gen-2 — this is not a quality
question, it's arithmetic. (Explore's instruction is ~500 tokens; even that
only fits with a stripped toolset and no meaningful history.)

What fits — and is exactly where adk-cc spends paid tokens on trivia today:

- **Session titles** (SessionTitlePlugin): one short call per session, today
  billed to the user's pinned model (metered on the codex subscription).
- **Memory capture** (MemoryPlugin.after_run, 1 call/turn, ≤6K-char
  transcript ≈ 1.5K tokens — fits): the single biggest hidden model-call
  consumer in desktop use.
- **Memory topic resolve + consolidation synthesis** (short prompts).
- **Compaction seed/summaries** where the input chunk is bounded.
- Confirmation-title/summary generation and other sub-1K utility prompts.

AFM at 0.2 s/call makes all of these free, instant, and private.

## Integration options

**A. In-process utility model (recommended, P1).** A small
`AppleFmLlm(BaseLlm)` adapter in `models/apple_fm.py`: maps `LlmRequest`
(system instruction + contents flattened to a prompt) → `LanguageModelSession
.respond()`, yields ONE complete `LlmResponse` (double-yield-safe by
construction). No tool bridging needed for the utility tier. A
`utility_model()` resolver hands it to SessionTitlePlugin / MemoryPlugin /
synth when `ADK_CC_UTILITY_MODEL=apple-fm` and the SDK imports; falls back to
the session model otherwise. Import is lazy + guarded (`.available` check at
first use, cached).

**B. OpenAI-compatible local shim (P2, optional).** A ~100-line FastAPI shim
(`scripts/apple_fm_server.py`) exposing `/v1/chat/completions` over the SDK,
registered as a normal model-endpoint (`apple-fm` in the registry, keyless).
Then the EXISTING SelectableLlm/LiteLlm path works unchanged and users can
PIN a session to apple-fm for lite chats. Out-of-process = an SDK/FFI crash
can't take down the sidecar. Requires a "lite prompt profile" to be useful
(see P2). On macOS 27 this entire shim is replaced by Apple's own `fm serve`.

**C. Full-agent local model — NOT with gen-2.** Revisit at macOS 27: AFM 3
Core Advanced (20B sparse) + `fm serve` + whatever context window Apple
ships. The registry-endpoint plumbing from P2 makes that a config change.

## Phased plan

- **P0 (done, this doc):** venv recipe + probe suite results recorded.
  Probes live in the session scratch (`fm_probe.py`) — move into
  `scripts/probe_apple_fm.py` in P1.
- **P1 — utility tier (~150 LOC + tests):** `models/apple_fm.py` adapter
  (text-only, options: temperature, max tokens, timeout); `ADK_CC_UTILITY_MODEL`
  schema var (`""`=session model, `apple-fm`); wire into SessionTitlePlugin,
  MemoryPlugin (capture/resolve), synth. Graceful degradation when
  unavailable (non-Apple platform, SDK missing, model off). Tests: adapter
  unit (fake SDK), live smoke behind `ADK_CC_APPLE_FM_LIVE=1` skip guard.
  Install story: optional dependency group (`uv pip install -e ".[applefm]"`),
  NOT a hard dep — the source build needs full Xcode.
- **P2 — pinnable lite endpoint (~150 LOC):** the shim server + registry
  entry + a "lite" prompt profile (minimal instruction, ≤8 tools, aggressive
  microcompaction) selected automatically when the pinned model's context
  budget is <8K (ContextGuard already knows per-model limits — feed it 4K).
  Value: offline/private quick Q&A sessions on a laptop.
- **P3 — macOS 27 upgrade path:** swap the shim for `fm serve`, evaluate AFM
  3 Core Advanced for Explore-class work (bigger window + 20B sparse), and
  the `fm` CLI for scripted checks. Decision point, not code, today.

## Risks / caveats

- `apple-fm-sdk` is **Alpha** (0.x): pin the exact version; the 0.2.1 build
  break (SDK-gap on `tokenCount`) shows Apple ships shims ahead of Xcode.
- In-process FFI inside the sidecar: a Swift-side crash kills uvicorn.
  Acceptable for P1's low-rate utility calls; P2's shim isolates it for
  chat-rate traffic.
- ~4K window is TOTAL (instructions+prompt+response): every P1 call site must
  clamp its input (memory capture already truncates the transcript to 6K
  chars) and set `maximum_response_tokens`.
- Guardrails exist even though none tripped: catch `GuardrailViolationError`
  and fall back to the session model per call.
- Desktop-only, Apple-silicon-only, macOS 26+ — everything must no-op
  cleanly elsewhere (same pattern as the protected-path floor's desktop gate).
- Language: works in Korean today, but Apple Intelligence keys off device
  language; if the primary language changes, re-verify.
