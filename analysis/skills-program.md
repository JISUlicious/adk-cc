# adk-cc skills program — research + unified implementation plan

2026-07-26. Single source of truth. Supersedes and absorbs
`business-skills-p1-taxonomy.md`, `business-skills-p2-index.md`,
`business-skills-p3-plan.md`, and `data-science-fullstack-plan.md` (git history
retains them). The machine-readable corpus stays at
`analysis/business-skills-index.json` (302 profiles).

**Goal**: grow adk-cc from a coding agent into a capable agent for office
workers and people running a company — solo founder through enterprise —
without diluting what it is already good at.

## Status at a glance

Legend: ✅ done · 🔨 in progress · ⬜ not started

| Part | Item | Status |
|---|---|---|
| I | 1. Topic taxonomy (10 domains + cross-cutting, 3 axes) | ✅ 2026-07-26 |
| I | 2. Corpus survey — 1,185 mined, 302 profiled, 162 clean | ✅ 2026-07-26 |
| I | — machine-readable index (`business-skills-index.json`) | ✅ 2026-07-26 |
| I | — pd-skills profiled + verified against the real loader | ✅ 2026-07-26 |
| I | 3. Verified platform facts (discovery, progressive disclosure, runtime) | ✅ 2026-07-26 |
| II | W1 runtime — uv-managed analysis env | ⬜ **critical path** |
| II | W2 built-in skills plumbing | ⬜ |
| II | W3 the built-in set (~21) | ⬜ |
| II | W4 agent/tool layer | ⬜ |
| II | W5 data layer (ingestion) | ⬜ |
| II | W6 UI/UX frontend | ⬜ |
| II | W7 verification | ⬜ |

**Nothing in Part II is implemented yet** — no code has been written for this
program. Part I is complete research, and its findings are what shaped Part II.
Three open decisions (bottom) should be answered before W3 starts.

---

# Part I — Research

## 1. Topic taxonomy ✅

Three axes, because function alone doesn't decide what to build: **function**
(below), **company stage** (S=solo/startup, M=mid-market, E=enterprise), and
**agent fit** — the axis that actually decides scope:

- **Tier A — self-contained.** A work product from documents + reasoning + web.
  No live system access. *Where a workspace agent genuinely excels.*
- **Tier B — needs data plumbing.** Live business-system data (CRM, ledger,
  HRIS). Feasible via adk-cc's per-user MCP servers + secrets, but quality is
  capped by the integration → belongs in opt-in packs.
- **Tier C — judgment/liability heavy.** Legal, tax, regulatory, termination
  decisions. May draft/checklist/organize; must never sound authoritative.

| # | Domain | Representative sub-topics | Fit | Stage |
|---|---|---|---|---|
| 1 | **Direction & governance** | strategy/OKRs, board decks & investor updates, fundraising & data room, cap table math, M&A diligence, entity/governance | A (C for entity) | S M E |
| 2 | **Money** | budget/forecast/runway, 3-statement & unit-economics models, valuation (DCF/comps/LBO), pricing & packaging, invoicing/AR-AP, close & reconciliation, tax | A (B for ledger, C for tax) | S M E |
| 3 | **Marketing** | positioning/ICP/messaging, content + SEO **and AEO**, campaign briefs & ad copy, competitive research, brand, lifecycle ops, attribution | A (B for ad platforms) | S M E |
| 4 | **Sales & customer success** | proposals/quotes, **RFP/RFI/security questionnaires**, account research & call prep, battlecards, pipeline/forecast, onboarding/QBR/churn, support & escalation | A (B for CRM) | S M E |
| 5 | **Product & R&D** | PRDs/specs/roadmaps, user-research synthesis, experiment design, R&D planning & feasibility, technical due diligence, ADRs, IP & prior art, grant applications | A (C for IP/grants) | S M E |
| 6 | **People** | JDs/scorecards/interview loops, offers & onboarding, performance & leveling, comp bands, policy & handbook, org design & headcount | A (C for reviews/policy) | S M E |
| 7 | **Legal, risk & compliance** | contract review vs. playbook (MSA/NDA/SOW/DPA), drafting, privacy (GDPR/CCPA/DPIA), frameworks (SOC 2/ISO/HIPAA), regulatory monitoring, risk register & incident response | **C throughout** | S M E |
| 8 | **Operations & supply** | SOPs & process docs, RCA/kaizen, PM artifacts (charter/status/RAID), vendor eval & RFP issuance, procurement & spend, business continuity | A | S M E |
| 9 | **Data & analytics** | metric/KPI definition, SQL, dashboards, reporting, validation | A (B for warehouses) | S M E |
| 10 | **Data science** *(added after the p2 gap analysis)* | EDA & diagnostics, feature importance, causal inference & RCA, experiment/DOE, SPC & change-point, forecasting | A | S M E |
| — | **Communication & documents** *(cross-cutting substrate)* | meetings & notes, internal comms, external/PR, decks & one-pagers, spreadsheets/reports, email | A | S M E |

## 2. Corpus survey ✅

Method: **cloned the source repos and mined actual `SKILL.md` frontmatter and
bodies** — not blog summaries. 1,185 skills found.

| Source | Found | License | Note |
|---|---|---|---|
| `w95/awesome-claude-corporate-skills` | 166 | MIT | 14 role dirs; closest match to the taxonomy |
| `claude-office-skills/skills` | 137 | MIT | office/business, flat layout |
| `anthropics/skills` | 18 | Apache-2.0 (**docx/pdf/pptx/xlsx source-available**) | official |
| `ComposioHQ/awesome-claude-skills` | 864 | Apache-2.0 | **832 are SaaS wrappers** |
| `JISUlicious/pd-skills` | 2 (14 files, 195KB) | first-party | data science, EN+KO |

**Three funnel steps changed the picture:**

1. 832 of Composio's 864 are `composio-skills/` SaaS integration wrappers —
   Tier B by construction and largely **redundant with adk-cc's existing MCP
   support**. Effective pool: **353**.
2. Dedupe across repos → **302 unique** (35 multi-source; provenance kept).
3. **Hidden vendor lock-in**: grepping bodies flagged **27 "Tier A" skills that
   are not** — they need Bloomberg, Capital IQ, PitchBook, FactSet, Common
   Room, or an API key. This hit the *highest-scoring finance skills*
   (`tear-sheet`, `funding-digest`, `earnings-analysis`, `dcf-valuation`) —
   enterprise-partner demos, unusable for a normal user.

**Genuinely clean pool: 162** (Tier A, no vendor dep, not already native).

| Domain | Clean | Read |
|---|---|---|
| finance | 35 | Deep supply, but famous names are vendor-locked; modeling/templating remains |
| comms_docs | 23 | Mostly conversion; docx/pdf/pptx/xlsx already available **and** source-available → no slots here |
| operations | 19 | Genuinely self-contained (postmortems, business cases, SOPs) |
| sales_cs | 17 | Support cluster unusually good |
| governance | 13 | Strategy/competitive solid; board prep is Tier C |
| marketing | 13 | Ad-platform skills are Tier B in disguise; copy/content clean |
| people | 12 | Reviews, comp benchmarking, hiring kits |
| data (BI) | 11 | SQL, viz, dashboards, stats, validation |
| **product_rnd** | **4** | **Thinnest domain in a 1,185-skill corpus** |
| legal | **0 clean** (14 total) | Uniformly Tier C by nature |
| **data science** | **~0** | 4 keyword hits, 3 false positives ("classify") |

Quality distribution: 66 low / 191 middling / 45 strong → **adopt-then-edit,
never bulk-copy.**

**The two real gaps — `product_rnd` and data science — are exactly where
adk-cc is strongest** (it reads real codebases; it has a first-party DS skill).
That is the differentiation thesis of this program.

## 3. Verified platform facts ✅

Checked against the installed ADK and adk-cc source, not assumed:

- **Skill format & discovery**: adk-cc loads Anthropic's SKILL.md format via
  `ADK_CC_SKILLS_DIR` → project `.adk-cc/skills/` or `.claude/skills/` →
  install fallback `<install>/adk_cc/skills/`. **That last dir does not exist
  yet — creating it *is* "built-in".**
- **The install fallback is unconditional** (`_add(here)` always runs).
  Built-ins always load; project skills **override by name** (first-found
  wins, deduped before `SkillToolset` construction → no duplicate-name crash).
  *The module docstring says otherwise and is stale — fix it.*
- **Progressive disclosure** (this corrected an earlier assumption of mine):
  `SkillToolset` exposes **4 fixed tools** + adk-cc's search = **5, constant
  regardless of skill count**. The catalog is returned only when the model
  calls `list_skills`. Measured: 12 skills ≈1.0K tokens, 20 ≈1.5K, 40 ≈2.7K,
  162 ≈7.8K. **The binding constraint is selection precision and maintenance,
  not always-on tokens** — target ~20 built-ins, not 12–16.
- **`make_skill_toolset` returns `None` when no skills exist.** Adding
  built-ins makes those 5 tools appear for **every user** — a real behavior
  change needing a kill switch and tests.
- **Lenient resource loading works**: pd-skills' 13 root-level companions
  resolve through `load_skill_resource` even from wrong-folder guesses
  (`references/…`, `scripts/…`) via basename fallback. Large files are
  **paginated** (`next_offset` + hint), not truncated.
- **Runtime is broken for analysis**: `sandbox/code_executor.py:110` hardcodes
  `python3 <file>`. Under `NoopBackend` that is `/usr/bin/python3` →
  **Python 3.9.6 with pandas/numpy/scipy/plotly/sklearn/xgboost/shap/
  statsmodels all absent**. pd-skills fails on its own first import.

---

# Part II — Unified implementation plan

Seven workstreams. **W1 is the critical path**: several skills are inert
without it.

## W1 — Runtime: a uv-managed analysis environment ⬜ **critical path**

Never invoke a bare interpreter — it routes to system python (user's
direction, and the only way to control the version). `uv 0.10.11` is present
and `uv python install` supplies the interpreter itself, so neither version nor
packages depend on what the host ships.

- New `sandbox/analysis_env.py`: resolve-or-create a per-workspace env —
  `uv python install <pinned>` → `uv venv --python <pinned>` →
  `uv pip install -r <pinned reqs>`; cache by requirement-set hash; return the
  interpreter path. **Pin the Python version explicitly** (e.g. 3.12).
- `SandboxBackedCodeExecutor` takes the interpreter from that resolver instead
  of the literal `python3`; the no-analysis-env path must still resolve
  deliberately rather than falling through to system python.
- Tiers so first use isn't a multi-minute install:
  **core** (pandas≥2.3, numpy, scipy, pyarrow, matplotlib, plotly) ·
  **modeling** (scikit-learn, xgboost, shap) ·
  **stats/causal** (statsmodels, ruptures, dowhy).
- Per backend: Noop/desktop → local uv venv; Docker/local-container → bake core
  into the image, extras on first use; Daytona/E2B → same contract via their
  exec; SSH → remote venv under the workspace.
- Config: `ADK_CC_ANALYSIS_ENV=auto|off|<path>`, `ADK_CC_ANALYSIS_TIERS`.
- **Explicit failure**: provisioning errors say what failed and how to fix it —
  never a bare `ModuleNotFoundError`.

## W2 — Built-in skills plumbing ⬜

- Create `agents/adk_cc/skills/`; fix the stale `_resolve_skills_dirs`
  docstring.
- `ADK_CC_BUILTIN_SKILLS=0` kill switch (default on) + schema row +
  `.env.example` regen (the schema self-check test enforces this).
- **Packaging check**: ensure the wheel actually ships `SKILL.md` and
  companions — a package that drops non-`.py` files silently ships zero skills.
- `ATTRIBUTION.md`: upstream repo + commit + license per vendored skill.

## W3 — The built-in set (~21) ⬜

Selection bars — all six must pass: **(1)** genuinely Tier A (no vendor data
provider/API key); **(2)** license-clean MIT/Apache-2.0 with attribution
(**never** vendor Anthropic's source-available docx/pdf/pptx/xlsx);
**(3)** complements rather than duplicates adk-cc's tools/plugins/bundled
example-skills; **(4)** universal across stage; **(5)** liability-safe (Tier C
ships with non-advice framing); **(6)** earns a catalog line (no near-synonym
pairs).

**[A]dopt-and-edit · [W]rite ourselves**

| Domain | Skills |
|---|---|
| **Data science** | `data-analyst` **[A — first-party pd-skills]** |
| Finance (4) | `financial-model` [A: w95 `3-statements`] · `dcf-model` [A] · `budget-forecast` [W] · `pricing-analysis` [W] |
| Operations (3) | `incident-postmortem` [A] · `business-case-builder` [A] · `sop-writer` [W] |
| Data/BI (3) | `data-analysis` [A: merge `statistical-analysis`+`data-validation`] · `interactive-dashboard-builder` [A] · `sql-queries` [A] |
| Customers (3) | `competitive-analysis` [A] · `customer-response` [A: merge `response-drafting`+`ticket-triage`] · `proposal-rfp` [W] |
| People (2) | `hiring-kit` [W] · `performance-review` [A, framed] |
| Legal (2, Tier C) | `contract-review` [A] · `nda-triage` [A] — both carry *"not legal advice; have counsel review"* |
| Governance (2) | `strategic-planning` [A] · `board-update` [W] |
| **Product & R&D (3)** | `prd-writer` [W] · `tech-due-diligence` [W] · `feasibility-study` [W] |

Merging near-synonyms (bar 6) is deliberate: two similar catalog lines cost
selection precision, which is the real constraint.

**Long tail** → opt-in packs (`packs/business-extended/`, `packs/data-analyst-ko/`)
reachable via the existing `ADK_CC_SKILLS_DIR`. No new code.

## W4 — Agent/tool layer ⬜

- Artifact convention: analysis outputs land at a known workspace path the UI
  can find.
- Dataset size guard: refuse/sample above a threshold with a clear message
  instead of OOM-ing the sandbox.
- Point skills at `search_skill_resource` (arg: `query`) to grep large
  companions rather than paging linearly.

## W5 — Data layer ⬜

- **Ingestion**: today a dataset must already be in the workspace. Add upload
  (desktop: picker → workspace; web: multipart → tenant workspace) with
  type/size limits and a visible datasets location.
- Formats: csv/tsv/parquet/xlsx/json (pyarrow already present).
- Privacy: datasets stay inside the workspace boundary; only sampled
  schema/head enters prompts.

## W6 — UI/UX frontend ⬜

The substrate exists — `HtmlArtifactPreview` + `SandboxedHtml` (sandboxed
iframe) + artifacts panel — so **Plotly interactive HTML renders today**.
Build on it rather than inventing a viewer.

1. **Chart-first rendering** — analysis HTML artifacts preview inline and
   expand in the side panel *(wire the trigger)*.
2. **Dataset browser** — Files panel shows shape, dtypes, null counts, head:
   what an analyst checks first, without asking the agent.
3. **Analysis run view** — group a run's outputs (EDA report, VIF table, SHAP
   plot, RCA timeline) instead of scattering files.
4. **Table rendering** — dataframes as real tables (sortable, sticky header),
   not markdown blobs.
5. **Env status chip** — analysis-env state (provisioning / ready / tier
   missing) beside the model chip, so W1 failures are legible.
6. **Skill discoverability** — surface available skills in the UI; today the
   only path is the model calling `list_skills`.
7. **KO/EN parity** — if the Korean variant ships, follow the UI locale.

## W7 — Verification ⬜

- **Unit**: analysis-env resolution + tier install (mocked); dataset guard;
  discovery (built-ins load; project skill of the same name overrides;
  `ADK_CC_BUILTIN_SKILLS=0` yields none; no duplicate-name crash).
- **Budget regression**: `list_skills` payload stays under a pinned ceiling
  (~2K tokens) — the guard against skill sprawl.
- **Packaging**: the installed package really contains the SKILL.md files.
- **Acceptance**: run pd-skills' own diagnostics probes
  (`null_collinearity_probe.py`, `mixed_type_vif_test.py`, …) — **first-party
  tests already exist; use them.**
- **Live UI e2e** (standing practice): load a real CSV → ask for EDA → assert a
  Plotly artifact renders, VIF/SHAP output appears, no console errors,
  screenshot. Plus one adopted skill (contract review over a sample NDA,
  confirming the non-advice header) and one authored skill
  (`tech-due-diligence` over a real repo).

## Sequencing

| Order | Work | Why here |
|---|---|---|
| 1 | **W1 runtime** | Nothing analytic works if `import pandas` fails |
| 2 | **W2 plumbing + W3 slice-1** (`data-analyst` + R&D trio + finance core) | Proves packaging and the live path with the *differentiating* skills |
| 3 | **W6.1 chart rendering** | Turns output into a visible product moment |
| 4 | **W5 ingestion** | A way in besides the file system |
| 5 | **W3 remainder** (adopted 11) | Bulk editing after the path is proven |
| 6 | **W6.2–6.7, W4, W5 polish** | Depth once the loop works |

## Risks

| Risk | Mitigation |
|---|---|
| Skill sprawl degrades selection | ~20 cap; catalog-budget test; long tail in opt-in packs |
| Vendored prose is mid-quality (66/191/45) | Adopt-then-edit against the six bars; never bulk-copy |
| Tier C liability | Mandatory non-advice header, verified in the live UI check |
| Packaging drops non-`.py` files | Explicit packaging test in W7 |
| 5 new tools appear for every user | Kill switch + harness test that the coordinator still behaves |
| Analysis env install is slow/large | Tiered installs; core-only by default; cached by hash |
| Upstream license drift | `ATTRIBUTION.md` pins repo + commit + license |

## Open decisions

1. **Analysis env default** — core tier only, or full stack incl.
   xgboost/shap/dowhy on first use? *Recommend core + on-demand tiers.*
2. **Korean variant** — built-in or opt-in pack? *Recommend pack* (doubles
   catalog entries, hurts selection precision).
3. **First slice contents** — `data-analyst` + R&D trio + finance core (7–8
   skills), or all ~21 at once? *Recommend the slice.*
