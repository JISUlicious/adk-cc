# Business skills — Phase 3: implementation plan (built-in skill set)

2026-07-26. Follows p1 (taxonomy) and p2 (index of 302 profiled skills, 162
clean). Goal: ship adk-cc's first **built-in** skills so a founder / office
worker gets useful business capability out of the box.

## Correction to p1/p2 (verified against the installed ADK)

p1 asserted "every built-in costs tokens in the always-on tool listing, so keep
the set to 10–20." **That is wrong.** `SkillToolset` exposes **4 fixed tools**
(`list_skills`, `load_skill`, `load_skill_resource`, `run_skill_script`) plus
adk-cc's `skill_resource_search` — **5 total, constant regardless of skill
count**. Skills are *progressively disclosed*: the catalog (name +
description) is returned only when the model calls `list_skills`.

Measured catalog cost (mean description 120 chars):

| Skills | `list_skills` payload |
|---|---|
| 12 | ~1.0K tokens |
| 20 | ~1.5K tokens |
| 40 | ~2.7K tokens |
| 80 | ~4.8K tokens |
| 162 (all clean) | ~7.8K tokens |

So the binding constraint is **not** always-on tokens. It is:

1. **Selection precision** — the model picking the right skill out of a large
   XML blob. This degrades well before the token budget hurts.
2. **Per-call catalog cost** — paid on turns where `list_skills` fires.
3. **Maintenance surface** — every vendored skill is code we now own.

Revised target: **~20 built-ins** (~1.5K catalog), chosen for coverage breadth
rather than depth, with an opt-in pack mechanism for the long tail. Two other
verified facts shape the plan:

- **The install fallback is unconditional.** `_resolve_skills_dirs()` calls
  `_add(here)` on every invocation — built-ins always load, and project /
  `ADK_CC_SKILLS_DIR` skills override them **by name** (first-found wins,
  deduped before `SkillToolset` construction, so no duplicate-name crash).
  This is exactly the semantics we want. *The module docstring says otherwise
  ("used when no env var / no project skills are discovered") and is stale —
  fix it in P3.0.*
- **`make_skill_toolset` returns `None` when no skills exist.** Today most
  users have zero skills, so the toolset is absent and the coordinator has no
  skill tools. Adding built-ins makes those 5 tools **always present for every
  user** — a real behavior change to call out and test.

## Selection bars

A skill enters the built-in set only if it clears all six:

1. **Tier A, genuinely** — no vendor data provider, no API key (p2 flagged 27
   fakes; `tear-sheet`, `funding-digest`, `earnings-analysis`, `dcf-valuation`
   are all disqualified despite top scores).
2. **License-clean** — MIT / Apache-2.0, attribution preserved. Anthropic's
   `docx/pdf/pptx/xlsx` are source-available → **never vendored**.
3. **Complements, not duplicates** — nothing that re-implements adk-cc's own
   tools/plugins or the bundled `example-skills`.
4. **Universal** — useful to a solo founder *and* at enterprise scale.
5. **Liability-safe** — Tier C (legal/tax/HR-decision) skills ship only with
   explicit non-advice framing in the SKILL.md body.
6. **Earns a catalog line** — distinct trigger; no near-synonym pairs that
   confuse selection.

## The proposed built-in set (~20)

Two origins: **[A]dopt** (vendor + edit from the corpus) and **[W]rite**
(author ourselves, where the corpus is empty or weak).

### Finance (4)
| Skill | Origin | Note |
|---|---|---|
| `financial-model` | A — w95 `3-statements` | 3-statement model build; strip any provider hooks |
| `dcf-model` | A — w95 | self-contained valuation |
| `budget-forecast` | W | runway/burn/scenario for founders — corpus version is enterprise-shaped |
| `pricing-analysis` | W | unit economics + packaging; genuinely absent from the corpus |

### Operations (3)
`incident-postmortem` [A], `business-case-builder` [A], `sop-writer` [W —
process documentation from an observed workflow].

### Data & analysis (3)
`data-analysis` [A — merge of w95 `statistical-analysis` + `data-validation`],
`interactive-dashboard-builder` [A], `sql-queries` [A].
*Merging the two analysis skills avoids a near-synonym pair (bar 6).*

### Customers (3)
`competitive-analysis` [A — w95, appears in 2 repos], `customer-response` [A —
merge of `response-drafting` + `ticket-triage`], `proposal-rfp` [W — RFP /
security-questionnaire responses; high value, thin in corpus].

### People (2)
`hiring-kit` [W — JD + scorecard + structured loop; corpus versions are Tier C
shaped], `performance-review` [A, with non-advice framing].

### Legal & risk (2, both Tier C)
`contract-review` [A — w95, best in corpus], `nda-triage` [A]. Both get a
mandatory header: *not legal advice; have counsel review before signing.*

### Governance (2)
`strategic-planning` [A — OKRs], `board-update` [W — investor/board update from
repo + metrics context].

### Product & R&D (3) — **the authored differentiator**
p2 found only **4 clean candidates** in this domain across 1,185 skills, the
thinnest in the corpus, while adk-cc is strongest here.
| Skill | Origin | Why adk-cc wins |
|---|---|---|
| `prd-writer` | W | PRD grounded in the actual repo, not a blank template |
| `tech-due-diligence` | W | reads a real codebase: deps, licenses, test coverage, risk — no other skill pack can |
| `feasibility-study` | W | engineering estimate + cost model for a proposed feature |

## Packaging

```
agents/adk_cc/skills/                 # NEW — the built-in core (~20)
  <skill>/SKILL.md [+ references/ scripts/]
  ATTRIBUTION.md                      # per-skill upstream + license
packs/business-extended/              # opt-in long tail (not loaded by default)
```

Users opt into the long tail with `ADK_CC_SKILLS_DIR=<path to pack>` — already
supported, no new code.

**Kill switch**: add `ADK_CC_BUILTIN_SKILLS=0` to skip the install fallback,
for users who want a bare agent or full control. New schema row + `.env.example`
regeneration (the schema self-check test enforces this).

## Work breakdown

- **P3.0 — plumbing (small).** Create `agents/adk_cc/skills/`; fix the stale
  `_resolve_skills_dirs` docstring; add `ADK_CC_BUILTIN_SKILLS` (default on) +
  schema row; ensure packaging ships the dir (check `pyproject.toml`
  include/package-data — a wheel that drops non-`.py` files would silently
  ship zero skills).
- **P3.1 — adopt (11 skills).** Vendor from MIT sources, then EDIT: strip
  vendor hooks, normalize frontmatter (`name`, `description` with a real
  trigger), trim to adk-cc's tool vocabulary, add non-advice headers where
  Tier C. Record provenance in `ATTRIBUTION.md`.
- **P3.2 — author (9 skills).** Write the gap-fillers, R&D trio first — that's
  the differentiator and the one no competitor pack has.
- **P3.3 — tests.**
  - discovery unit: built-ins load with no project skills; a project skill of
    the same name **overrides** the built-in; `ADK_CC_BUILTIN_SKILLS=0` yields
    none; no duplicate-name crash.
  - catalog budget: `list_skills` payload stays under a pinned ceiling
    (~2K tokens) — a regression test against skill sprawl.
  - packaging: the installed package actually contains the SKILL.md files.
  - scripted-LLM harness: a turn that invokes one built-in end-to-end.
- **P3.4 — live UI verification** (per standing instruction). In the desktop
  app: `list_skills` shows the built-ins; run one *adopted* skill (contract
  review over a sample NDA) and one *authored* skill (`tech-due-diligence`
  over a real repo); screenshot both; confirm no console errors and that the
  Tier C output carries its non-advice header.
- **P3.5 — docs + memory.** README section, `ATTRIBUTION.md`, and a memory
  note recording the progressive-disclosure finding (it invalidates the
  "10–20 max" reasoning that would otherwise be re-derived later).

## Risks

| Risk | Mitigation |
|---|---|
| Skill sprawl degrades selection | Hard cap ~20 built-ins; catalog-budget test; long tail in opt-in pack |
| Vendored prose is mid-quality (p2: 66 low / 191 mid / 45 strong) | Adopt-then-edit, never bulk-copy; each skill reviewed against the 6 bars |
| Tier C liability | Mandatory non-advice header; verified in the live UI check |
| Packaging drops non-`.py` files | Explicit packaging test in P3.3 |
| Behavior change: 5 new tools for every user | Called out; kill switch; harness test that the coordinator still behaves |
| Upstream license drift | `ATTRIBUTION.md` pins source repo + commit + license per skill |

## Open question for the user (answer before P3.1)

**Scope of the first cut**: ship all ~20 at once, or land the **R&D trio +
finance core (7)** first as a vertical slice, prove it in the UI, then batch
the rest? Recommendation: **vertical slice first** — it proves plumbing,
packaging, and the live path with the differentiating skills, before we spend
effort editing eleven adopted ones.
