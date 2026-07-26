# Data science in adk-cc — full-stack plan (skill → runtime → UI)

2026-07-26. Triggered by: *"are data science skills on the list? if not, start
from `JISUlicious/pd-skills`, and plan for all stacks of adk-cc, from the skill
itself up to UI/UX frontend."*

## Answer: no, data science is NOT on the list

Searching the 302-profile index (`business-skills-index.json`) for ML /
notebook / feature-engineering / model-evaluation terms returns **4 hits, 3 of
which are false positives** (`Email Classifier`, `nda-triage`,
`legal-risk-assessment` — matched on the word "classify"). The only real one is
`csv-data-summarizer`.

The `data` domain in p2 is **BI/analytics, not data science**: SQL, dashboards,
charts, descriptive stats, data validation. Nothing covers modeling, feature
importance, causal inference, experiment design, or diagnostics.

So data science is an even larger gap than `product_rnd` (4 clean candidates)
— and it is the one gap where a **first-party skill already exists**.

## pd-skills: profiled

`JISUlicious/pd-skills` — pandas ≥2.3 expert data-analyst skill, EN + KO.

| Property | Value |
|---|---|
| Structure | `SKILL.md` (8.9KB) + **13 companion `.md` files at skill root**, 195KB total |
| Depth | RCA 43.8KB · feature-importance 30.3KB · data-exploration 30.0KB |
| Method coverage | 9-step EDA w/ point-mass guard · **VIF + mixed-type collinearity** (η²/Cramér's V) · null-handling decision tree · XGBoost + permutation + **SHAP** w/ cross-method agreement · change-point (**PELT, CUSUM**) · SPC (Shewhart, EWMA, Cp/Cpk) · Fisher exact + lift + Bonferroni · causal (DAG, DiD, propensity, **dowhy** refutation) · DOE/ANOVA |
| Output default | **Plotly interactive HTML**, matplotlib/seaborn fallback |
| Quality vs. corpus | Far above median. Corpus was 66 low / 191 middling / 45 strong, "a page of prose"; this is a 14-file methodology with anti-pattern catalogs |

**Verified against adk-cc's real loader** (not assumed):

- ✅ Discovery works — both variants found, frontmatter parsed.
- ✅ The root-level layout is **already handled**: `load_skill_resource`
  resolved `references/root-cause-analysis.md`, bare `root-cause-analysis.md`,
  and even the wrong-folder guess `scripts/feature-importance.md` — adk-cc's
  lenient basename fallback is exactly the feature this layout needs.
- ✅ Large files are **paginated, not truncated** (`total_lines`,
  `next_offset`, continue hint) — the 44KB RCA file is fully reachable.
- ⚠️ `resources` load as 0 at discovery (root-level files aren't
  `references/`), so everything arrives through the on-demand path. Works, but
  means the model must know the filenames — **SKILL.md already names all 13**.

**Conclusion: the skill layer needs no changes.** It drops in as-is.

## The actual blocker is the runtime

Skill scripts execute via `python3 <file>` inside the active sandbox backend
(`sandbox/code_executor.py`). On desktop that is `NoopBackend` → the host:

```
/usr/bin/python3  →  Python 3.9.6
pandas False · numpy False · scipy False · plotly False · matplotlib False
sklearn False · xgboost False · shap False · statsmodels False
```

**pd-skills fails on its own first instruction** (`import pandas as pd`).
Meanwhile adk-cc's *own* `.venv` has pandas/numpy/scipy/sklearn/matplotlib/
seaborn/pyarrow — but that is the agent process env, and using it would defeat
the sandbox boundary the code executor exists to enforce.

Missing everywhere for pd-skills' headline features:
**plotly** (default output), **xgboost + shap** (feature importance),
**statsmodels** (VIF, ANOVA), **ruptures** (PELT), **dowhy** (causal).

## Stack-by-stack plan

### S1 — Skill layer (small)
- Vendor `data-analyst` (EN) as the first built-in analysis skill under
  `agents/adk_cc/skills/`; keep `-ko` as an opt-in pack (Korean users get it
  via `ADK_CC_SKILLS_DIR`, and it doubles the catalog otherwise).
- Since it is first-party, **no license friction** — record provenance in
  `ATTRIBUTION.md` anyway for consistency.
- Add a short "adk-cc runtime" section to the vendored copy: how to request the
  analysis env, where to write artifacts, how to surface a chart (S6).
- Optional: move companions into `references/` in the vendored copy so they
  also load at discovery. *Not required* — verified working as-is.

### S2 — Runtime layer (**the critical path**)
A **managed analysis environment**, provisioned on demand, never the agent's
own venv.

**Root cause is one line**: `sandbox/code_executor.py:110` hardcodes
`cmd = f"python3 {file}"`. Inside `NoopBackend` that resolves to
`/usr/bin/python3` — system Python 3.9.6, no packages, and not even a version
pandas ≥2.3 targets. **Fix: never invoke a bare interpreter; always resolve a
uv-managed one** (user's direction, and the only way to control the version).
`uv 0.10.11` is present, and `uv python install` can supply the interpreter
itself, so neither the version nor the packages depend on what the host
happens to ship.

- New `sandbox/analysis_env.py`: resolve-or-create a per-workspace env with
  `uv python install <pinned>` + `uv venv --python <pinned>` +
  `uv pip install -r <pinned reqs>`; cache by requirement-set hash; return its
  interpreter path. Pin the Python version explicitly (e.g. 3.12) rather than
  inheriting the host's.
- `code_executor` takes the interpreter from that resolver instead of the
  literal `python3`; when no analysis env applies it still must resolve
  deliberately (uv-managed default) rather than falling through to system
  python.
- Pinned requirement tiers so we don't pay for everything up front:
  - **core** (always): pandas≥2.3, numpy, scipy, pyarrow, matplotlib, plotly
  - **modeling** (on demand): scikit-learn, xgboost, shap
  - **stats/causal** (on demand): statsmodels, ruptures, dowhy
- `SandboxBackedCodeExecutor` uses that interpreter instead of bare `python3`.
- Per backend: Noop/desktop → local uv venv; Docker/local-container → bake the
  core tier into the image, install extras at first use; Daytona/E2B → same
  contract via their exec; SSH → remote venv under the workspace.
- Config: `ADK_CC_ANALYSIS_ENV=auto|off|<path>`, `ADK_CC_ANALYSIS_TIERS`.
- Failure mode must be explicit: if provisioning fails, the tool result says
  *why* and how to fix it — never a bare `ModuleNotFoundError`.

### S3 — Agent/tool layer (small)
- Teach the skill (and `run_skill_script`) the artifact convention: analysis
  outputs go to a known workspace path so the UI can find them.
- Dataset size guard: refuse/-sample above a threshold with a clear message
  rather than OOM-ing the sandbox.
- Reuse the existing `search_skill_resource` (`query` arg) so the model can
  grep the 44KB RCA file instead of paging it linearly.

### S4 — Data layer
- **Ingestion**: today a dataset must already be in the workspace. Add upload
  (desktop: file picker → workspace; web: multipart → tenant workspace),
  with type/size limits and a visible "datasets" location.
- Formats: csv/tsv/parquet/xlsx/json — pyarrow is already present.
- Privacy: datasets are user data; keep them inside the workspace boundary and
  out of prompts except as sampled schema/head.

### S5 — Service/backend layer
- Reuse the existing artifact path (`save_as_artifact`, artifact fetch API) as
  the transport for charts and reports — no new service needed.
- Consider an `analysis` artifact kind so the UI can group outputs of a run.

### S6 — UI/UX frontend (**where this becomes a product**)
adk-cc already has `HtmlArtifactPreview` + `SandboxedHtml` (sandboxed iframe)
and an artifacts panel — so **interactive Plotly HTML renders today**. Build on
that rather than inventing a viewer:

1. **Chart-first rendering** — a Plotly/HTML artifact from an analysis run
   previews inline in the thread and expands in the side panel. (Substrate
   exists; wire the trigger.)
2. **Dataset browser** — the Files panel gains a dataset affordance: shape,
   dtypes, null counts, head — the things an analyst checks first, without
   asking the agent to print them.
3. **Analysis run view** — group a run's outputs (EDA report, VIF table, SHAP
   plot, RCA timeline) instead of scattering files.
4. **Table rendering** — dataframes as real tables in chat (sortable, sticky
   header), not markdown blobs.
5. **Env status chip** — surface analysis-env state (provisioning / ready /
   tier missing) next to the model chip; make S2 failures legible.
6. **KO/EN parity** — if the Korean variant ships, the surfaced skill name and
   descriptions should follow the UI locale.

### S7 — Verification (per standing practice)
- Unit: analysis-env resolution + tier install (mocked), dataset guard.
- Integration: run pd-skills' own `analysis/*.py` probes (the repo ships
  diagnostics like `null_collinearity_probe.py`, `mixed_type_vif_test.py`) as
  the acceptance suite — **first-party tests already exist, use them**.
- **Live UI e2e**: load a real CSV → ask for EDA → assert a Plotly artifact
  renders in the panel, VIF/SHAP output appears, no console errors;
  screenshot. Same bar as the durable-runs and F8/F9 work.

## Sequencing

| Step | Why first |
|---|---|
| **S2 runtime** | Nothing else matters if `import pandas` fails |
| **S1 skill** | Trivial once the runtime exists |
| **S6.1 chart rendering** | Turns output into a visible product moment |
| **S4 ingestion** | Users need a way in besides the file system |
| **S6.2–6.4 dataset/table/run views** | Depth once the loop works |
| **S3/S5 polish, S6.5–6.6** | After the happy path is proven |

## Open questions

1. **Scope of the analysis env** — ship the core tier only (pandas/plotly), or
   the full stack incl. xgboost/shap/dowhy (large install, minutes on first
   use)? Recommend core-by-default with on-demand tiers.
2. **KO variant** — ship both built-in (doubles catalog entries, hurts
   selection precision) or Korean as an opt-in pack? Recommend the pack.
3. **Does this precede or fold into the p3 business-skills set?** The p3 plan
   proposed ~20 built-ins; `data-analyst` is a strong candidate for the first
   vertical slice precisely because it is first-party and battle-tested.
