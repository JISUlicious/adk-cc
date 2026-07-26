# Business skills — Phase 2: profile & index of existing skills

2026-07-26. Follows `business-skills-p1-taxonomy.md`. Method: **cloned the
source repos and mined the actual `SKILL.md` frontmatter + bodies** — not blog
summaries. Machine-readable output: `analysis/business-skills-index.json`
(302 deduped profiles).

Per-skill profile fields: `name · src · license · domain · fit · q (quality
score) · clean_A · vendor_dep · also_in · dupes · has_scripts · has_refs ·
bytes · path · desc`.

## Corpus

| Source | SKILL.md found | License | Note |
|---|---|---|---|
| `w95/awesome-claude-corporate-skills` | 166 | MIT | 14 role dirs; the closest match to our taxonomy |
| `claude-office-skills/skills` | 137 | MIT | office/business, flat one-dir-per-skill |
| `anthropics/skills` | 18 | Apache-2.0 (docx/pdf/pptx/xlsx **source-available**) | official |
| `ComposioHQ/awesome-claude-skills` | 864 | Apache-2.0 | **832 are SaaS wrappers** (see below) |
| **Total mined** | **1,185** | | |

### Three funnel steps that changed the picture

1. **832 of Composio's 864 are `composio-skills/` SaaS integration wrappers**
   (HubSpot, Salesforce, Stripe, …). They are Tier B by construction, and
   largely *redundant with adk-cc's existing per-user MCP server support* — if
   a user connects a HubSpot MCP server, a "HubSpot skill" adds little.
   Effective pool: **353**.
2. **Dedupe across sources** → **302 unique** (35 appeared in >1 repo; w95
   aggregates from others, so provenance is tracked in `also_in`).
3. **Hidden vendor lock-in**: grepping bodies for data-provider and credential
   requirements flagged **27 skills classified Tier A that are not** — they
   need Bloomberg, S&P Capital IQ, PitchBook, FactSet, Common Room, or an API
   key. This silently hit the *highest-scoring finance skills*
   (`tear-sheet`, `funding-digest`, `earnings-analysis`, `dcf-valuation`) —
   they are enterprise-partner demos, unusable for a normal user.

**Genuinely clean pool: 162** — Tier A, no vendor dependency, and not already
native to adk-cc (its own tools, plugins, and the bundled `example-skills`).

## Coverage: clean candidates per domain

| Domain | Clean | Read |
|---|---|---|
| finance | 35 | Deepest supply, but the best-known names are vendor-locked; what remains is modeling/templating |
| comms_docs | 23 | Mostly format conversion; the flagship docx/pdf/pptx/xlsx are **already available** to adk-cc |
| operations | 19 | Strong, genuinely self-contained (postmortems, business cases, SOPs) |
| sales_cs | 17 | Customer-support cluster is unusually good and self-contained |
| governance | 13 | Strategy/competitive analysis solid; board prep is Tier C |
| marketing | 13 | Ad-platform skills are Tier B in disguise; copy/content ones are clean |
| people | 12 | Reviews, comp benchmarking, JD/interview kits |
| data | 11 | SQL, viz, dashboards, statistical analysis, validation |
| product_rnd | 4 | **Thinnest in the entire corpus** |
| legal | 0 clean (14 total, all Tier C) | Every legal skill is judgment/liability-bound by nature |

## Strongest clean candidates (phase-3 shortlist input)

| Domain | Skill | Src | q | Why it stands out |
|---|---|---|---|---|
| governance | `competitive-analysis` | w95 | 13 | Industry-agnostic framework, reference material, appears in 2 repos |
| finance | `3-statements` | w95 | 13 | Complete 3-statement model build; templates included |
| finance | `dcf-model` | w95 | 12 | Real DCF with scripts, no data-provider dependency |
| data | `data-context-extractor` | w95 | 12 | Scripts + references; feeds every other analysis skill |
| data | `sql-queries` / `data-visualization` / `interactive-dashboard-builder` / `statistical-analysis` / `data-validation` | w95 | 11 | Coherent analysis chain, all self-contained |
| sales_cs | `ticket-triage`, `response-drafting`, `escalation`, `customer-research` | w95 | 11 | Support cluster; pure reasoning over the user's own docs |
| operations | `incident-postmortem`, `business-case-builder` | w95 | 9 | Blameless RCA + ROI cases; universal across company sizes |
| governance | `strategic-planning`, `kpi-dashboard` | w95 | 9 | OKR/strategy scaffolding |
| people | `performance-review-assistant`, `compensation-benchmarking` | w95 | 9 | Recurring, high-stakes, template-driven |
| legal (C) | `contract-review`, `nda-triage`, `legal-risk-assessment` | w95 | 11 | Best-in-corpus legal triage — **must ship with non-advice framing** |

## What the data says (findings, not vibes)

1. **The p1 hypothesis is confirmed and sharpened**: `product_rnd` has only
   **4** clean candidates — the thinnest domain in a 1,185-skill corpus, while
   finance has 35. R&D/technical-business work (feasibility studies, technical
   due diligence, grant applications, IP prep) is a genuine ecosystem gap, and
   it is exactly where adk-cc's coding competence is an unfair advantage.
   **This is where we should author rather than adopt.**
2. **Quality is mid**: the score distribution is 66 low (0–4), 191 middling
   (5–9), 45 strong (10+). Most community skills are a page of prose with no
   scripts or references. Adoption should be *selective and edited*, not bulk.
3. **Legal is uniformly Tier C** — 14 skills, 0 clean. Not a reason to skip
   the domain (contract triage is high-value); a reason to require explicit
   non-advice framing in any built-in.
4. **Document skills are already covered.** `docx/pdf/pptx/xlsx` are the
   corpus's highest-quality items and adk-cc already reaches them via the
   bundled Anthropic set — plus they're source-available, so vendoring is off
   the table anyway. Do not spend built-in slots here.
5. **Marketing's ad-platform skills are Tier B in disguise** (Google/Meta Ads
   need accounts + credentials). The clean marketing subset is copy/content.

## Phase 3 preview (next step)

Shortlist ≈ 20 built-ins from the 162 (see `business-skills-p3-plan.md`; the
earlier 12–16 figure came from a token-cost assumption that p3 disproved),
biased toward: universal across
company stage, self-contained, complementary to adk-cc's existing tools, and
license-clean (MIT/Apache-2.0 with attribution). Expect the built-in set to be
**part adopted, part authored** — adopted for finance/ops/support/data where
supply is good, authored for `product_rnd` where supply is absent and adk-cc
is strongest.
