# Business skills for adk-cc — Phase 1: topic taxonomy

2026-07-26. Goal (user): grow adk-cc from a coding agent into a capable agent
for **office workers and people running a company** — solo founder through
enterprise. Three phases: **(1) organize the topics** ← this doc,
(2) profile + index existing skills per topic, (3) cherry-pick a built-in set.

## What "built-in" will mean (the phase-3 constraint, established up front)

adk-cc already loads **Anthropic's SKILL.md format** (`tools/skills.py`) from,
in precedence order: `ADK_CC_SKILLS_DIR` → project `.adk-cc/skills/` or
`.claude/skills/` → **install fallback `<install>/adk_cc/skills/`**. That last
directory **does not exist yet** — creating it is exactly what "merge as
built-in skills" means. No new loader work is required; phase 3 is curation +
authoring, not plumbing.

Two constraints that follow:

- **Licensing gates what can be vendored.** MIT (`w95/awesome-claude-corporate-skills`,
  `claude-office-skills/skills`) and Apache-2.0 (Composio, most `anthropics/skills`)
  are vendorable with attribution. Anthropic's `docx/pdf/pptx/xlsx` skills are
  **source-available, NOT open source** — reference them, do not copy them in.
- **Built-ins are always-on surface area.** Every built-in skill costs tokens in
  the tool listing and competes for the model's attention. The right built-in
  set is ~10–20 high-leverage skills, not 166. Breadth belongs in an optional
  *pack* users opt into; only the universal core ships built-in.

## The organizing axes

Three axes, because function alone doesn't decide what to build.

1. **Function** — the department/domain (below).
2. **Company stage** — solo/startup vs. mid-market vs. enterprise. The same
   topic differs wildly: "financial planning" is a runway spreadsheet for a
   founder and a driver-based FP&A model with variance commentary at scale.
3. **Agent fit** — what adk-cc can actually do well. This is the axis that
   decides phase 3, so it gets tiers:

   - **Tier A — self-contained.** Produces a work product from documents +
     reasoning + web. Needs no live system access. *This is where a workspace
     agent is genuinely excellent and where built-ins should concentrate.*
   - **Tier B — needs data plumbing.** Requires live business-system data
     (CRM, ledger, HRIS). Feasible — adk-cc already has per-user MCP servers
     and per-user secrets — but skill quality is capped by the integration, so
     these belong in optional packs tied to specific MCP servers.
   - **Tier C — judgment/liability heavy.** Legal opinions, tax filings,
     regulatory submissions, termination decisions. The agent may draft,
     checklist, and organize; it must never present output as authoritative
     advice. Built-ins here need explicit "not legal/tax advice, get a
     professional" framing baked into the skill.

## The taxonomy

Nine domains, each with sub-topics and a fit read. "Stage" flags where a topic
mostly matters: **S**=solo/startup, **M**=mid-market, **E**=enterprise.

### 1. Direction & governance
| Sub-topic | Fit | Stage |
|---|---|---|
| Strategy, vision, OKR/goal setting | A | S M E |
| Board decks & investor updates | A | S M E |
| Fundraising collateral, data room prep, diligence checklists | A | S M |
| Cap table & equity basics (options, vesting, dilution math) | A/C | S M |
| M&A screening, diligence question sets, integration plans | A | M E |
| Corporate structure, entity setup, governance calendar | C | S M E |

### 2. Money (finance & accounting)
Largest ecosystem category (42 of 166 skills in the corporate repo) — and the
one with the clearest agent leverage, because most of it is *modeling and
document production*.
| Sub-topic | Fit | Stage |
|---|---|---|
| Budgeting, forecasting, runway & burn analysis | A | S M E |
| Financial modeling (3-statement, unit economics, cohort, scenario) | A | S M E |
| Valuation (DCF, comps, LBO) | A | M E |
| Pricing & packaging analysis, discount policy | A | S M E |
| Invoicing, expense reports, AR/AP hygiene | A/B | S M |
| Bookkeeping, reconciliation, month-end close | B | M E |
| Tax planning, sales-tax nexus, R&D credit prep | C | S M E |
| Fundraise/loan readiness packages | A | S M |

### 3. Customers — marketing
| Sub-topic | Fit | Stage |
|---|---|---|
| Positioning, ICP, messaging architecture | A | S M E |
| Content strategy, SEO **and AEO** (LLM-citation optimization) | A | S M E |
| Campaign briefs, ad copy, landing-page critique | A | S M E |
| Competitive & market research (web-driven) | A | S M E |
| Brand guidelines & asset consistency | A | M E |
| Lifecycle/email/CRM campaign ops | B | M E |
| Analytics & attribution reporting | B | M E |

### 4. Customers — sales & success
| Sub-topic | Fit | Stage |
|---|---|---|
| Proposals, quotes, **RFP/RFI/security-questionnaire responses** | A | S M E |
| Prospect/account research & call prep briefs | A | S M E |
| Sales collateral, objection handling, battlecards | A | S M E |
| Pipeline hygiene, forecast rollups, CRM ops | B | M E |
| Onboarding plans, QBR decks, renewal/churn analysis | A/B | M E |
| Support macros, escalation & incident comms | A | M E |

### 5. Product & R&D
| Sub-topic | Fit | Stage |
|---|---|---|
| PRDs, specs, roadmaps, prioritization frameworks | A | S M E |
| User research synthesis (interviews → themes) | A | S M E |
| Experiment design & result interpretation (A/B, stats) | A | M E |
| R&D project planning, technical feasibility studies | A | M E |
| Technical due diligence, architecture decision records | A | M E |
| IP strategy: invention disclosures, prior-art searching | A/C | M E |
| Grant/subsidy applications (R&D funding) | A/C | S M |

### 6. People (HR)
| Sub-topic | Fit | Stage |
|---|---|---|
| Job descriptions, scorecards, structured interview loops | A | S M E |
| Offer letters, onboarding plans, 30/60/90s | A | S M E |
| Performance reviews, leveling frameworks, comp bands | A/C | M E |
| Policy & employee handbook drafting | A/C | M E |
| Org design, headcount planning, workforce modeling | A | M E |
| HRIS operations (time off, directory) | B | M E |

### 7. Legal, risk & compliance
Highest care required — Tier C dominates. Enormous *drafting and triage* value,
zero authority.
| Sub-topic | Fit | Stage |
|---|---|---|
| Contract review vs. a playbook/redline checklist (MSA, NDA, SOW, DPA) | A/C | S M E |
| Contract drafting from templates | A/C | S M E |
| Privacy & data governance (GDPR/CCPA mapping, DPIA prep) | C | M E |
| Compliance frameworks (SOC 2, ISO 27001, HIPAA) — evidence & gap tracking | A | M E |
| Regulatory research & change monitoring (industry-specific) | A/C | M E |
| Risk register, incident response runbooks, postmortems | A | M E |
| Employment-law basics, IP assignment, licensing hygiene | C | S M E |

### 8. Operations & supply
| Sub-topic | Fit | Stage |
|---|---|---|
| SOP authoring & process documentation | A | S M E |
| Root-cause analysis, kaizen/continuous improvement | A | M E |
| Project & program management artifacts (charters, status, RAID) | A | S M E |
| Vendor evaluation, RFP issuance, scorecards, renewals | A | M E |
| Procurement policy, spend analysis | A/B | M E |
| Business continuity & disaster recovery plans | A | M E |

### 9. Cross-cutting: communication & documents
Not a department — the *substrate* every domain above depends on, and where
document skills (docx/xlsx/pptx/pdf) actually live.
| Sub-topic | Fit | Stage |
|---|---|---|
| Meeting agendas, notes, decisions, action tracking | A | S M E |
| Internal comms: updates, newsletters, announcements, FAQs | A | M E |
| External comms & PR, crisis comms | A | M E |
| Executive-ready deck and one-pager production | A | S M E |
| Spreadsheet/report generation & analysis | A | S M E |
| Email drafting, triage, classification | A/B | S M E |

## Ecosystem landscape (sets up phase 2)

| Source | Size | License | Relevance |
|---|---|---|---|
| `w95/awesome-claude-corporate-skills` | 166, 14 role categories | MIT (per-skill sources vary) | Closest match to this taxonomy; tags provenance |
| `claude-office-skills/skills` | 136+, office/business | MIT | Strong on legal/finance/document tasks; some need external services |
| `ComposioHQ/awesome-claude-skills` | 50+ | Apache-2.0 | Mostly SaaS-integration (Tier B) automations |
| `anthropics/skills` | official | Apache-2.0; **docx/pdf/pptx/xlsx source-available** | Document skills = reference only, do not vendor |
| Directories (awesomeclaude.ai, claudedirectory.org, awesomeskills.dev) | aggregators | n/a | Discovery for phase 2 |

Notable gap already visible: the ecosystem is thin on **R&D/engineering-adjacent
business work** (feasibility studies, technical diligence, grant applications,
IP prep) — which is precisely where adk-cc's existing coding competence gives it
an edge no generic office-skill pack has.

## Proposed phase-2 method (next step)

For each domain above, collect candidate skills into an index with a fixed
profile per skill: `name · source · license · domain · fit tier · stage ·
external deps · quality signals (structure, scripts, examples) · overlap with
adk-cc built-ins`. Prefer breadth per domain now, judgment later — phase 3 is
where cherry-picking happens, against these bars:

1. **Tier A first** — no live-system dependency.
2. **Complements, never duplicates** adk-cc's existing tools and plugins.
3. **License clean** for vendoring (MIT/Apache-2.0), attribution preserved.
4. **Self-contained** — no mandatory external SaaS to be useful.
5. **Liability-safe** — Tier C skills carry explicit non-advice framing.
6. **Earns its tokens** — universal enough to justify always-on surface.
