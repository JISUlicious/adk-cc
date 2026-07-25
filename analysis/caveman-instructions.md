# Caveman-compressed instructions — feasibility test + plan

2026-07-25. Question (user): can adk-cc ship "caveman"-style compressed
instruction variants so the agent fits small token windows (AFM's ~4K,
cheap/free endpoints)?

Technique refs: `github.com/wilpel/caveman-compression` (input-side: strip
predictable grammar — articles, conjunctions, hedging — keep numbers, names,
constraints; claims 40–58% on system prompts) and
`github.com/juliusbrussee/caveman` (output-side Claude Code skill, 65% avg
output reduction, honest-caveats README).

## Measured (live A/B on the on-device AFM ~3B — the worst-case model)

Hand-compressed two REAL adk-cc prompts, then ran identical tasks against
full vs caveman variants (tool calling included, temperature=0):

| Prompt | Full | Caveman | Saved |
|---|---|---|---|
| `EXPLORE_INSTRUCTION` | ~497 tok | ~185 tok | **63%** |
| memory `_CAPTURE_PROMPT` | ~209 tok | ~124 tok | **41%** |
| `COORDINATOR_INSTRUCTION` (extrapolated at ~55%) | ~3,380 tok | ~1.5K tok | — |

Behavior:

- **Explore-style task (with live SDK tools): parity.** Both variants chose
  the same tool with the same (naively literal) grep pattern and failed the
  same way — the 3B model is the ceiling, not the instruction length. The
  caveman variant was FASTER (1.0s vs 2.1s — half the prefill).
- **Read-only compliance probe: both weak** (full deflected instead of
  refusing; cave hit a one-off SDK generation error). Model-bound again.
- **Capture extraction: small real cost on 3B.** temp=0, 4 samples each:
  full = 3 well-formed `TOPIC: x | fact` lines every time; cave = 2 + an
  input echo (still parseable — `_parse_facts` drops echo lines). On larger
  models this gap should close (grammar reconstruction is what they're good
  at) — treat 3B as the lower bound.
- **Incidental P1 finding:** AFM utility calls MUST set `temperature=0`
  (+ `maximum_response_tokens`); default sampling produced malformed output
  in earlier probes that temp-0 fully eliminated.

## What the arithmetic now allows

Caveman coordinator (~1.5K) + slim toolset (≤8 tools, ~0.8K schemas) + ~1.5K
history/response budget ≈ **fits AFM's 4K window** — the "lite profile" from
`analysis/apple-fm-model-plan.md` P2 goes from impossible to plausible.
Secondary win: on per-request-billed, non-cached endpoints (OpenRouter free
tier etc.), instruction compression saves its delta on EVERY turn.

## Recommendations

1. **Hand-maintained companion texts, not runtime auto-compression.**
   `prompts.py` gains `*_CAVEMAN` variants for EXPLORE, VERIFY, and a CORE
   coordinator subset — deterministic, reviewable, diffable. A drift test
   asserts every enforceable rule in the full text has a counterpart in the
   caveman text (checklist by rule keyword, not prose match).
2. **Selection = profile, not flag soup.** `ADK_CC_PROMPT_PROFILE=full|caveman`
   (default full), plus AUTO: pick caveman when the active model's context
   budget (ContextGuard already knows per-model limits) is below ~8K.
3. **Compress rule-lists, keep (or consciously drop) rhetoric.** VERIFY's
   anti-rationalization prose is load-bearing psychology for big models but
   compresses terribly; the caveman VERIFY should keep the failure-pattern
   list as facts and drop the rhetoric — measure PASS/FAIL honesty before
   trusting it.
4. **Don't compress the utility prompts** (capture/synth/title): they already
   fit any window, and the 3B A/B showed extraction fidelity is where
   compression actually costs.
5. Validation before shipping: A/B the caveman EXPLORE on a real paid model
   (mini) over 3–5 scripted exploration tasks — same-tools/same-conclusion
   parity bar, like the AFM A/B.

## Phasing (folds into the Apple-FM plan)

- **C1** (with Apple-FM P1): none — utility prompts stay full.
- **C2** (with Apple-FM P2 lite profile): caveman EXPLORE + CORE coordinator
  + profile selection + drift test. This is what makes the 4K lite agent fit.
- **C3**: caveman VERIFY after the honesty A/B; extend AUTO selection to any
  small-window endpoint.
