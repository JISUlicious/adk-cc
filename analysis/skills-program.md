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
| II | W1 runtime — uv-managed analysis env | ✅ 2026-07-27 (unit + API + UI verified) |
| II | W2 built-in skills plumbing | ✅ 2026-07-27 (incl. real wheel-packaging test) |
| II | W3 the built-in set (~21) | 🔨 6/21 — + legal pair under jurisdiction discipline |
| II | W4 agent/tool layer | ⬜ |
| II | W5 data layer (ingestion) | ⬜ |
| II | W6 UI/UX frontend | ⬜ |
| II | W7 verification | ⬜ |
| II | **W8 skill enable/disable from the UI** (all scopes) | ⬜ **unblocked — shippable now** |
| II | **W9 verification in the agentic loop** | 🔨 S0–S2 shipped 2026-07-27; S3+ pending measurement |

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

## W1 — Runtime: a uv-managed analysis environment ✅ DONE (2026-07-27)

**Shipped**: `sandbox/analysis_env.py` (uv-provisioned interpreter + tiered
packages, marker-cached on disk and in-process); `code_executor` now resolves a
managed interpreter instead of bare `python3`; `run_bash` puts the managed env
on `PATH` **only for commands that invoke Python** (`_with_managed_python`), so
ordinary shell commands are untouched and a broken env can never block them;
4 schema vars + `.env.example`; `tests/test_analysis_env.py` (8 tests incl. a
REAL provisioning test).

**Verified**: unit — 8/8, including real `uv` provisioning of Python 3.12.13 +
pandas 3.0.5 while host `/usr/bin/python3 import pandas` fails (rc=1).
API — gpt-5.4-mini analysed a CSV via `python - <<'PY'`, returning
`pandas 3.0.5` and correct group-bys. UI — same model pinned through the model
picker, 3 tool calls, `exit 0`, mean price by channel rendered, no console
errors. Counterfactual confirmed: the identical command under host python is
`ModuleNotFoundError: No module named 'pandas'`.

*Note for future work*: the injected PATH is invisible in recorded tool args —
ADK snapshots args before plugins mutate them — so verify by outcome
(does pandas resolve?), not by grepping the event log.

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

## W2 — Built-in skills plumbing ✅ DONE (2026-07-27)

**Shipped**: `agents/adk_cc/skills/` created (first built-in: `data-analyst`,
vendored from pd-skills with companions moved to `references/` so they load at
discovery as well as on demand); `ADK_CC_BUILTIN_SKILLS=0` kill switch + schema
row; `[tool.setuptools.package-data]` so the wheel actually ships SKILL.md;
`ATTRIBUTION.md`; the stale "install fallback" docstring corrected to describe
the base-layer semantics it always had.

**Verified**: 6 tests incl. a REAL `uv build` asserting the wheel contains
SKILL.md + 13 references (without package-data it shipped zero — the failure
mode that passes every other test). Live: gpt-5.4-mini called `load_skill`,
pulled 3 methodology references by bare filename, and produced a correct EDA
with hand-computed VIFs.

**Two W1 bugs surfaced by live testing** (unit tests with a fake backend could
not have caught either):
1. `uv venv` refuses an existing environment ("Use --clear to replace it") and
   `--clear` would discard installed tiers → escalation now skips creation.
2. Re-passing already-installed tiers over-constrained the resolver: asking for
   pandas+numpy+shap in one solve made uv select `numba 0.53.1` (Python <3.10
   only) and the build failed. Escalation now installs **only the delta**.
Both pinned by regression tests. Also: when the env can't be prepared, run_bash
now prefixes the reason to stderr instead of leaving a bare `exit 127`.

- Create `agents/adk_cc/skills/`; fix the stale `_resolve_skills_dirs`
  docstring.
- `ADK_CC_BUILTIN_SKILLS=0` kill switch (default on) + schema row +
  `.env.example` regen (the schema self-check test enforces this).
- **Packaging check**: ensure the wheel actually ships `SKILL.md` and
  companions — a package that drops non-`.py` files silently ships zero skills.
- `ATTRIBUTION.md`: upstream repo + commit + license per vendored skill.

## W3 — The built-in set (~21) 🔨 4 of 21 shipped

**Shipped (2026-07-27)**: `data-analyst` (adopted) + the authored R&D trio
`tech-due-diligence`, `feasibility-study`, `prd-writer`. Catalog cost ~342
tokens for 4 skills — well inside the 2K budget test.

The trio is **authored, not adopted**, because the corpus survey found only 4
clean candidates in this domain (thinnest of any) while it is exactly where
reading a real codebase is the advantage. Each is built around evidence from the
repo rather than a template: diligence cites command output and `file:line`;
feasibility calibrates estimates against *this* repo's comparable past changes
(`git log --stat`) instead of industry averages; the PRD reads existing code
before specifying.

**Live-verified**: `tech-due-diligence` run by gpt-5.4-mini against a real repo
loaded all 3 references and produced a structured report whose lead finding —
*no LICENSE file → all rights reserved* — is correct and consequential.
`feasibility-study` in the UI produced ground truth, an effort range, risks and
a recommendation; all 4 skills appeared in `list_skills` with usable
descriptions.

**Legal pair shipped (Tier C)**: `contract-review`, `nda-triage` — authored,
not adopted, under the jurisdiction discipline below.

### Authoring rule: context, never facts (`skills/AUTHORING.md`)

A built-in ships to everyone, so **no skill embeds jurisdiction-, entity- or
time-bound facts** — no statutory limits, rates, deadlines, notice periods or
"typical" terms. Those differ by country, entity form and year, and a skill
stating them is confidently wrong for most readers with no signal that it is
wrong. Skills encode instead: general knowledge of the field (methodology,
categories, failure modes), a **context-establishing first step** (jurisdiction,
entity type, size, industry, which side of the deal), and **live verification**
(`web_fetch` with a cited source and date) whenever a specific rule matters.

Enforced mechanically: `test_jurisdiction_sensitive_skills_ask_before_asserting`
requires the context step, the advice boundary and live-verification language;
`test_no_baked_in_jurisdiction_facts` greps every built-in for hardcoded
statutory numbers.

Live testing sharpened this. First run: the model caught every planted trap and
referred to counsel *where governed*, but silently proceeded without the user's
own jurisdiction. Forcing a question would delay a clear escalation, so both
skills now require an opening **context line** that names what was established
and what was not. Re-verified: output opens `Context — governing law: Delaware
(§5) · your side: mutual · your jurisdiction & entity: NOT ESTABLISHED —
analysis below is general, not localized.`

**Remaining 15**: finance (4), operations (3), data/BI (3), customers (3),
people (2 — jurisdiction-sensitive, same discipline), governance (2 — likewise
for entity/board topics).

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

## W8 — Skill enable/disable from the UI ⬜

Requirement: toggle skills on/off from the UI — **not only built-ins, but every
installed skill** (built-in, project, per-user, org/tenant).

**Independently shippable.** Unlike the rest of Part II this is not blocked by
W1/W2/W3: users can already install skills today (zip upload via
`/desktop/settings/skills`, `/auth/skills`, `/admin/skills`), and those skills
are all-or-nothing right now — the only way to stop one is to delete it.

### Verified constraints

- `_skills = make_skill_toolset()` is built **once at module import**
  (`agent.py:312`) and shared across sessions/users → a toggle **cannot** rebuild
  the toolset per user. **The filter must be dynamic, evaluated per call.**
- adk-cc already swaps ADK's skill tools for bounded variants in
  `_patch_skill_tools` → an established seam to add filtering, no fork.
- The catalog is produced by `ListSkillsTool._list_skills()` at `list_skills`
  time, so filtering there is naturally per-request.
- Today's APIs are **install/uninstall only** — there is no "installed but
  disabled" state anywhere in the model.

### Design

1. **Enablement state** — a persisted set of *disabled* skill names (deny-list,
   so newly added skills default to on, matching current behavior).
   Scopes, in precedence order: **session override** (like the model pin, for
   "not in this chat") → **user/global default** (Settings) → default on.
   Storage follows the existing patterns: desktop settings file for desktop,
   identity store for per-user in web.
2. **Enforcement — two layers, not one.**
   - `ListSkillsTool` → `_FilteredListSkillsTool`: disabled skills never enter
     the catalog. This *also* shrinks the `list_skills` payload, which is the
     real scarce resource (selection precision).
   - `LoadSkillTool` / `RunSkillScriptTool` → refuse a disabled skill by name.
     Defense in depth: a model that saw the name earlier in context must not be
     able to route around the toggle.
3. **API** — `GET /…/skills/catalog` returning every *discovered* skill with
   `name · source (built-in|project|user|org) · path · enabled · shadowed_by`;
   `PATCH /…/skills/{name}` `{enabled: bool}`. One shape, three mounts
   (desktop / auth / admin) mirroring the existing skill routes.
4. **UI** — extend the existing **Settings → Skills** tab (`UserSkillsSection`
   + `SkillsAdminTab` already exist; do not invent a new surface):
   - list every discovered skill grouped by source, each with a toggle;
   - show **shadowing** explicitly — a project skill overriding a built-in of
     the same name is invisible today and confusing when toggled;
   - show the resulting catalog size / token estimate, tying the control to the
     reason it matters;
   - admin scope can disable org-wide; users can disable for themselves.
5. **Invalidation** — none needed for the catalog (per-call). The existing
   `_invalidate_required_inputs()` hook covers install/uninstall.

### Tests
Unit: deny-list filtering; disabled skill absent from catalog **and** refused by
`load_skill`/`run_skill_script`; session override beats user default; unknown
name is a no-op. API: three mounts, authz (a user cannot disable org skills for
others). **Live UI**: toggle a skill off in Settings → `list_skills` no longer
lists it → toggle on → it returns; screenshot.

## W9 — Verification in the agentic loop ⬜

Verification is not a skills feature. The recurring failure this session was
**claims made without evidence** — eval round 1 produced three "fixed" claims
that a real reproduction contradicted, and none of them involved a skill. The
prompt rule I shipped (executed reproduction before claiming a fix) took that
to zero in round 2, which is the proof that the *claim* is the right unit of
verification, not the skill.

So: verify **results, whenever they are asserted**. Skills are one input among
several, not the trigger.

Today `verification` is a real, working sub-agent (139 authored events in the
session store, including a genuine **FAIL**, so it is not rubber-stamping) —
but it fires only when the coordinator elects to call it: never in round 1,
twice in round 2. Discretionary verification is not a loop.

### What triggers verification

Any of these, evaluated at turn end (all cheap — none needs a model call):

1. **A claim without evidence** *(primary)* — the answer asserts a result
   ("fixed", "passing", "works", "deployed", "verified") while the turn ran no
   command, test, or reproduction that could support it. This is the automated
   form of the executed-reproduction rule.
2. **Material change** — files mutated at/above a threshold. Already computable
   for free: desktop keeps a shadow-git snapshot per turn
   (`service/desktop_checkpoint.snapshot`) and `desktop_files` already parses
   `git status --porcelain`.
3. **Risk class** — irreversible or outward-facing effects: deletes, migrations,
   deploys, publishes, sends, credential changes. The command-safety classifier
   already tiers commands, so this signal exists too.
4. **A declared contract** — a skill's `x-adk-cc/verify`, or a plan's success
   criteria.
5. **The user asked.**

### What it verifies against — acceptance criteria, in priority order

The verifier's weakest point is deciding what "working" means. Give it a target,
from the best available source:

1. **Skill contract** (`x-adk-cc/verify`: `mode`, `checks`, `commands`) — the
   author knows the criteria best.
2. **The current plan** — plan mode already writes success criteria and
   `read_current_plan` already exists.
3. **The user's original request** — did it do what was asked, not what was
   easy?
4. **Repo-native signals** — the project's own test/build/lint commands.
5. **The agent's own claims** — the universal fallback: whatever it asserted,
   check that. Nothing else is needed for this to work on any turn.

### The ladder (soft → hard; each rung usable alone)

**S0 — Self-verification in the prompt (soft, shipped in part).** The
coordinator already must reproduce before claiming a fix. Extend the same
discipline to result claims generally, and give each step-based skill a Verify
step naming skill-specific evidence.

**S1 — Declarative contracts.** `x-adk-cc/verify` on skills (frontmatter
`metadata` is a dict; `x-adk-cc/*` is an established namespace, so no new
plumbing) and success criteria from the plan.

**S2 — Soft nudge (plugin, no extra model call).** At turn end, if a trigger
fired and no supporting evidence exists, inject the relevant acceptance criteria
as a reminder before the answer is finalized. Advisory. The TaskReminderPlugin
pattern.

**S3 — Hard gate (plugin).** For high-risk triggers (risk class, or a contract
demanding `verifier`), force one `verification` pass carrying the acceptance
criteria, before the user-facing answer. `FAIL` must be addressed, not
narrated. Bounded to one pass per turn; skipped when the turn changed nothing.

**S4 — Mid-workflow gates.** Long work fails *before* the end — a diligence
report assembled from a README is already lost by the time it is written. Gate
the transition that matters (evidence gathered → conclusions written), not just
the finish.

**S5 — Surface it.** Verdict as a first-class part of the answer — verified /
failed / unverified — in the UI, not buried in the transcript.

### Cost controls (non-negotiable — verification doubles work)

`ADK_CC_VERIFY=off|soft|hard`; hard reserved for risk-class and opted-in
contracts; **one pass per turn**; skipped entirely when the turn produced no
artifacts and made no claim. The nudge (S2) costs nothing extra and should
carry most of the value.

### Tests

Trigger detection (claim-without-evidence, change threshold, risk class) as
pure functions over a turn's events — fast and deterministic. Contract parsing
incl. malformed metadata. S2 fires once, only when a trigger fired. S3 forces
exactly one transfer, none on a no-op turn, and a `FAIL` blocks the answer
(scripted-LLM harness). Kill switch honored at each level. Live: a deliberately
false claim ("I fixed it" with no change) must be caught; a clean turn must not
pay for a verification pass.

### Status (2026-07-27): S0–S2 shipped

`verification/signals.py` (pure detectors), `verification/contract.py`
(`x-adk-cc/verify`), `plugins/verify_nudge.py`, `ADK_CC_VERIFY=off|soft|hard`
(default soft), and verify contracts on all 6 built-in skills (4 checks each).
16 tests.

**Live testing corrected the design twice** — neither was reachable by unit
tests:

1. **Detector gaps.** A real turn answered a bare `"Done."` (claim missed) and
   verified with `python - <<PY` (evidence missed). The second is the worse
   bug: it would have nudged a turn that *did* check. Both patterns added.
2. **A timing flaw in S2 itself.** `claim_without_evidence` can never fire at
   `before_model` time, because the claim only exists in the response being
   generated. Fixed with a **predictive** signal — "changed something, checked
   nothing" — which is what is knowable beforehand. The reactive claim signal
   is retained for measurement and for S3.

### Measurement (2026-07-27) — S2 alone is NOT sufficient

Paired experiment, `scripts/measure_verify_nudge.py`: 5 identical mutate-then-
report prompts under `ADK_CC_VERIFY=off` and `=soft`, gpt-5.4-mini, fresh
session each, workspace reset between arms, scored with the same detectors the
nudge uses.

| Arm | verified | unverified claims |
|---|---|---|
| `off` | 0/5 | **5/5** |
| `soft` | 1/5 | **4/5** |

**Conclusion: the advisory nudge moved one case out of five.** The model
receives the reminder before composing its answer and mostly proceeds anyway.
That is a real effect but nowhere near sufficient, and it is the measurement
the plan demanded before spending on the expensive rung — **S3 (the hard gate)
is now justified by data rather than by speculation.**

Caveat, stated plainly: the prompts *instructed* the model to "just tell me
it's done", which is adversarial by design and inflates the claim rate above
normal use. The comparison between arms is still valid (both arms got the same
prompts); the absolute rate is not a general-population figure.

Two detector corrections came out of the same run, both from real answers
rather than imagination: completion claims are dominantly `"Done — …"`,
`"Done. Added …"`, `"Created X …"` — a whole-message match caught only a bare
`"Done."`, so claim detection is now anchored to the *start* of the answer;
and the first attempt scored five empty sessions as "clean" because the harness
leaked `ADK_CC_API_KEY=x` into the sidecar (every turn died with
AuthenticationError). The harness now fails loudly on a turn that did nothing —
a measurement that cannot distinguish "clean" from "broken" is worse than none.

### Sequencing

S0/S1 first (free). Then **S2, and measure**: does the nudge alone move the
unverified-claim rate? Round 1 → round 2 went 3 → 0 on a prompt change alone,
so the cheap rung may well be sufficient. Deploy S3 only where S2 measurably
fails — that ordering is the whole point.

## Sequencing

S0 ships with each remaining skill (free). S1+S2 together (small, safe,
immediately useful). S3 after S2 has shown the nudge is insufficient — deploy
the expensive mechanism only where the cheap one demonstrably failed. S4/S5
last.

## Sequencing

| Order | Work | Why here |
|---|---|---|
| 1 | **W1 runtime** | Nothing analytic works if `import pandas` fails |
| 2 | **W2 plumbing + W3 slice-1** (`data-analyst` + R&D trio + finance core) | Proves packaging and the live path with the *differentiating* skills |
| 3 | **W6.1 chart rendering** | Turns output into a visible product moment |
| 4 | **W5 ingestion** | A way in besides the file system |
| 5 | **W3 remainder** (adopted 11) | Bulk editing after the path is proven |
| 6 | **W6.2–6.7, W4, W5 polish** | Depth once the loop works |

**W8 runs out of band** — it is unblocked today and does not wait on W1–W3.
Landing it early is also a hedge: it is the control users need once the built-in
set starts growing.

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
