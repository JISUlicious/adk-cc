# Memory System — Fix Plan

Status: in progress. Cost (model rate limit) is **not** a constraint for this
plan — the LLM is the default resolver/synthesizer wherever it improves quality.
Correctness guardrails (merge verification) and reliability fallbacks
(deterministic on failure) stay, because those are about correctness/uptime, not
cost.

## Problems
| # | Problem | Status |
|---|---|---|
| 1 | Under-merge — same fact, different slugs (fragmentation) | observed |
| 2 | Over-merge — different facts, colliding slug → silent overwrite | latent + risk of any merge fix |
| 3 | Over-capture — domain/wiki content stored as user memory | observed |
| 4 | Dead corroboration — confidence stuck at 0.5 | observed (consequence of #1) |
| 5 | Unbounded episodic — consolidated episodics never pruned | code-confirmed |
| 6 | Degraded recall — dupes/misses; brute-force scan, no index | consequence |

Root cause of #1+#2: the topic slug (free-text LLM label) is an unreliable
identity/dedup key — too specific → fragments, too generic → collides.

Current state (pre-fix): memory has **no index** (list/search are filesystem
scans, `docstore/filesystem.py:154,179`) and **no changelog** (only inline
`supersedes` per semantic item). The docstore KV layer exists and the wiki uses
it for index/changelog; memory does not.

## Fixes (sequenced)

### Fix G — memory changelog + light topic index  (infra, FIRST)
- Changelog via docstore `append`/`kv_get`: every mutation logged
  (`capture|semantic_create|semantic_supersede|semantic_corroborate|status:*|revert`)
  with before→after. `read_changelog(user)`.
- Maintained per-user topic index in KV (`topic → {id, summary, confidence,
  status, updated}`) — compact view for the resolver; begins to close the
  no-index gap.
- `revert_semantic(user, topic)` — restore prior value from `supersedes` (the
  rollback for a wrong merge).
- Addresses: observability + reversibility for #2; audit generally.
- Where: `memory/store.py`. Test: model-free.

### Fix A — LLM identity resolution at capture  → #1, mitigates #2, revives #4
- Two stages: extract (scoped) → resolve. Resolver gets the user's existing
  topics (G index) and classifies each new fact CORROBORATE/UPDATE/NEW + target.
  Identity becomes semantic, not string-match.
- Deterministic fallback: slug + `memory/canonicalize.py`. Decisions → changelog.
- Where: new `memory/resolve.py`; wire into `plugins/memory.py` capture loop.

### Fix D — merge verification (adversarial)  → contains #2
- Each proposed CORROBORATE/UPDATE gets an independent verify call (best-of-N
  optional); reject on disagreement; verdict → changelog. `supersedes` + G make
  it reversible.

### Fix B — capture scoping + relevance filter  → #3
- Tighten `_CAPTURE_PROMPT` (user/project only, negative examples) + per-fact
  classifier dropping domain noise.

### Fix C — prune-on-consolidate  → #5
- After promotion, reversibly archive consolidated episodics past age/cap; prune
  → changelog; provenance in `semantic.sources`. Keeps the scan cheap (#6).

### Fix F — periodic LLM memory-compaction  → residual #1/#2
- Scheduler runs whole-memory re-resolution per user, verified (D), logged (G).

### Consolidation default → LLM synth (deterministic fallback).

### Fix E — embedding recall/index (optional, later) → #6 at scale.

## Sequencing
1. G (changelog + index)  2. A + D  3. B  4. C  5. F + LLM-synth  6. E (opt)

## Verification
- Unit (model-free): changelog ops + rollback; deterministic fallback; prune;
  under/over-merge boundaries on fixed inputs.
- Live probe: synonym pair merges at write (A); distinct-but-similar NOT merged
  (D); domain facts not captured (B); changelog shows the trail.
- Long e2e: semantic ≪ episodic, confidence > 0.5, zero false merges, audit intact.

## Accepted tradeoffs
- Non-determinism in promotion (price of semantic identity).
- False merge possible → D (verify) mandatory; G (changelog) makes it auditable/reversible.
- LLM steps always have deterministic fallbacks.

## Decisions taken
- A default-on; G ships changelog + index; resolver is two-stage (extract→resolve).

## Progress
- [x] **G** — changelog + topic index + `revert_semantic` (`memory/store.py`). Tests: `test_memory_changelog.py` (6). Live-confirmed.
- [x] **A** — `memory/resolve.py` resolver, wired into `plugins/memory.py` capture. Tests: `test_memory_resolve.py` (7).
- [x] **D** — verify gate in `resolve._verify` (reject→NEW). Covered by resolve tests + live.
- [x] **B** — capture prompt scoped to user/project facts (`plugins/memory.py:_CAPTURE_PROMPT`). Live-verify pending.
- [x] **C** — prune-on-consolidate (`ADK_CC_MEMORY_EPISODIC_CAP`, `consolidate.py`). Tests: `test_memory_prune.py` (2).
- [ ] **F** — periodic LLM memory-compaction in the scheduler.
- [ ] consolidation **LLM-synth default**.
- [ ] live: re-run long e2e (semantic ≪ episodic, confidence>0.5); probe Fix B (domain facts gone).

Live G+A+D result: 3 restatements of one fact folded at write → episodic 5 / semantic 3
(was 4/4), corroboration revived (n_support=3 → conf 0.7), distinct facts stayed NEW
(no false merge), full changelog audit present.
