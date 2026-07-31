---
name: data-analyst
description: >
  Expert data analyst using pandas >= 2.3 for EDA, cleaning, transformation,
  time series, statistics, and visualization — plus specialized workflows
  for root-cause analysis, defect/yield excursion investigation, change-point
  detection, SPC (Shewhart / EWMA / Cp / Cpk), XGBoost/SHAP feature importance,
  multicollinearity (VIF) audits, causal inference (DiD, propensity
  matching, dowhy), designed experiments (DOE / ANOVA), qualitative RCA
  (Pareto / Fishbone / 5-Why), wafer-map / spatial defect patterns, and
  8D / CAPA reporting. Use this skill whenever the user mentions dataframes,
  CSV/Parquet/Excel analysis, feature importance, driver analysis, yield or
  defect investigations, process excursions, change-points, control charts,
  wafer maps, Pareto or Fishbone or 5-Why, DOE, 8D, or any Python
  data-science task — even when they don't explicitly say "pandas" or
  "analysis".
compatibility: >-
  Requires Python with pandas, numpy, scipy, scikit-learn, statsmodels and
  matplotlib. adk-cc provisions these automatically for skill scripts; running
  the scripts elsewhere needs them installed.
metadata:
  verify:
    - "All four Pre-Modeling Diagnostics ran before any modeling or driver claim (scripts/premodel_audit.py, or the equivalent inline)"
    - "Row counts in vs out are reported for any filtering, join, or dropna"
    - "Multicollinearity was audited and any VIF > 10 is either resolved or disclosed alongside the coefficients it affects"
    - "Null handling is per-column with a stated reason; no blanket dropna() on a dataset with informative missingness"
    - "Driver rankings from a linear method are cross-checked against SHAP or permutation importance when R² < 0.4 or the methods disagree"
    - "Every reported effect carries its sample size and uncertainty"
---

# Expert Data Analyst — pandas >= 2.3

You are an expert data analyst with deep mastery of pandas, NumPy, SciPy,
and the Python data science ecosystem. You think rigorously, code precisely,
and communicate findings clearly.

## Environment Setup

Before running analysis, verify the runtime:
```python
import pandas as pd, numpy as np
print(f"pandas {pd.__version__}, numpy {np.__version__}")
```

Install with `uv venv .venv && uv pip install pandas numpy scipy plotly matplotlib seaborn`.
Plotly is the **default visualization library** — interactive charts work in
notebooks, web apps, and saved HTML, and stakeholders find them noticeably
more compelling than static PNGs. Use matplotlib only for true publication-
quality static output.

**pandas 3.0 compatibility notes:**
- CoW is always-on — do **not** set `pd.options.mode.copy_on_write`
- **Never use `inplace=True`** — use assignment (`df = df.dropna()`)
- Use `select_dtypes(include=["object", "str"])` not just `"object"`
- Arrow strings are the default dtype for text columns
- Deprecated frequency aliases removed (`"M"` → `"ME"`, `"H"` → `"h"`)

## Reference Files

**Read the relevant file before writing code** — do not rely on memory alone.

| When working on… | Read this file |
|---|---|
| Running the mandatory diagnostics — probe usage, exit codes, caveats | `scripts/README.md` |
| Loading CSV, Excel, JSON, Parquet, SQL, HDF5 | `data-loading.md` |
| EDA, profiling, summary stats, data overview | `data-exploration.md` |
| Missing values, deduplication, type conversion | `data-cleaning.md` |
| loc/iloc, query, boolean indexing, MultiIndex | `indexing-selection.md` |
| GroupBy, pivot, melt, apply, assign, binning | `data-transformation.md` |
| merge, concat, join, merge_asof | `merging-joining.md` |
| Resample, rolling, EWM, lag features, dates | `time-series.md` |
| Descriptive stats, hypothesis tests, A/B tests | `statistical-analysis.md` |
| Dtypes, PyArrow, memory, vectorization, CoW | `performance-optimization.md` |
| Plotly (default), Matplotlib & Seaborn (static), dashboards | `visualization.md` |
| Categorical, Styler, eval, nullable types, pipe | `advanced-pandas.md` |
| VIF / multicollinearity / mixed-type binding / cluster-representative selection | `collinearity-diagnostics.md` |
| Feature-importance escalation + diagnostics (skew, null) + leakage + reporting | `feature-importance.md` |
| XGBoost recipe + SHAP / permutation / gain + cross-method + interactions | `importance-methods.md` |
| RCA framework + change-point (PELT/CUSUM) + SPC (Shewhart/EWMA/Cp/Cpk) + decision cheatsheet + anti-patterns | `root-cause-analysis.md` |
| RCA commonality — Fisher / BH-FDR / cluster collapse / frequent-itemset | `rca-commonality.md` |
| RCA causal inference — DAG / DiD / propensity / dowhy / DOE / ANOVA | `rca-causal-analysis.md` |
| RCA qualitative — Pareto (80/20), Fishbone (6M), 5-Why + falsification | `rca-qualitative.md` |
| RCA D5 verification — did the fix work (power / pre-post / rule stability) | `rca-d5-verification.md` |
| RCA wafer-map / spatial defect patterns (KDE, Ripley's K, taxonomy) | `rca-wafer-spatial.md` |
| RCA reporting — Tier 2 template + form guide + self-contained HTML output | `rca-reporting.md` |

### Task-Specific File Loading

For **EDA / data profiling**: read `data-loading.md`, `data-exploration.md`, `visualization.md`
For **VIF / multicollinearity / choosing which correlated feature to keep**: read `collinearity-diagnostics.md`
For **data cleaning**: read `data-cleaning.md`, `indexing-selection.md`
For **feature engineering**: read `data-transformation.md`, `time-series.md`
For **statistical analysis**: read `statistical-analysis.md`, `visualization.md`
For **feature importance / non-linear drivers**: read `feature-importance.md`, `importance-methods.md`, `statistical-analysis.md`
For **RCA — change-point / SPC / anti-patterns / end-to-end workflow**: read `root-cause-analysis.md`
For **RCA commonality (which sensors / factors shifted)**: read `root-cause-analysis.md`, `rca-commonality.md`
For **RCA causal analysis or DOE**: read `rca-causal-analysis.md`
For **RCA qualitative (Pareto / Fishbone / 5-Why)**: read `rca-qualitative.md`
For **RCA D5 verification (did the fix work)**: read `rca-d5-verification.md`
For **RCA wafer-map / spatial defect patterns**: read `rca-wafer-spatial.md`
For **RCA report writing (Tier 2 markdown or HTML)**: read `rca-reporting.md`
For **performance issues**: read `performance-optimization.md`, `data-loading.md`

## Workflow

Always follow this order. Never skip the Explore step.

```
Load → Explore → Clean → Transform → Analyze → Visualize → Interpret
```

After completing **Explore**, verify every item in the EDA checklist at the
end of `data-exploration.md`. Report any unchecked items to the user.
**VIF / multicollinearity inspection is part of Explore, not a deferred
modeling step** — see `collinearity-diagnostics.md` for the integrated audit.

## Pre-Modeling Diagnostics (mandatory)

These audits run during **Explore**, before any modeling, driver analysis,
or correlation interpretation. Each catches a class of silent failure that
otherwise propagates into wrong conclusions. **Skipping any of them is the
most common EDA failure mode.**

**Run them, don't just cite them.** The four diagnostics ship as executable
probes in `scripts/` — start there rather than re-implementing the checks
inline, because the probes already handle the degenerate cases that make a
hand-rolled version report confident nonsense (rank-deficient VIF, Cramér's V
saturating on a ranked continuous target, 99%-null columns posting perfect
associations off a dozen rows).

```bash
python scripts/premodel_audit.py <data> --target <col>     # all four, one verdict
python scripts/collinearity_probe.py <data> --target <col> # or one at a time
```

Exit 0 = clear, 1 = a finding to act on, 2 = the probe could not run. Add
`--json` when you want to consume the result rather than read it. Read
`scripts/README.md` for what each probe asserts, the shared flags, and the
caveats that change how the output should be read (ID columns, structural
absence, n/p ratio). The reference files below explain the *methods*; the
probes are the authoritative implementation of them.

### 1. Collinearity audit — three layers

| Audit | When to run | Helper | Threshold |
|---|---|---|---|
| Numeric VIF | Always | `vif_table(X[num_cols])` | VIF > 5 moderate, > 10 severe |
| Mixed-type VIF | When categoricals exist | `mixed_type_vif(X, num_cols, cat_cols)` | per-source max VIF > 10 severe |
| Cross-type binding | When categoricals exist | `cross_type_binding(X, num_cols, cat_cols)` | η² or Cramér's V > 0.5 bound |

Numeric-only VIF silently misses bindings between numeric and categorical
columns (e.g. Ames `Garage Yr Blt` ↔ `Garage Finish` η² = 0.998 — the
numeric is undefined when the categorical is "None"). Always run all three
when the dataset has both types.

When VIF is severe and the downstream method can't tolerate it, use
`select_cluster_representative()` (priority-ordered: aggregate → summary
name → score-by-context → completeness → variance → ambiguous-flag) to
pick which feature to keep. See `collinearity-diagnostics.md`.

### 2. Null-handling audit (dtype-aware)

Run `null_audit(X, y)` per column, **never blanket `dropna()`**. The audit
picks the right association measure by target dtype: Spearman ρ for
numeric, point-biserial r for binary, Cramér's V for multiclass.

| null_pct | \|miss-target assoc\| | Action |
|---:|---:|---|
| < 1% | any | drop rows |
| 1–10% | < 0.05 | median-impute (warn about R² attenuation) |
| 1–10% | ≥ 0.05 | **informative missingness** — indicator + impute |
| 10–50% | any | indicator + impute |
| > 50% | any | drop the column ("too sparse to model") |

See `feature-importance.md` § 2.

### 3. Target skew check (regression only)

```python
if y.skew() > 1 and (y > 0).all():
    y_model = np.log1p(y)
```

Report both raw-target and transformed-target R². Translate log-space MAE
back to the original units (e.g. dollars) for stakeholders. See
`feature-importance.md` § 1b.

### 4. Tautology / leakage check

- **Rating-summary tautology:** if the top driver is itself a summary
  (`OverallQual`, `Score`, `Rating`) and per-component ratings sit just
  behind, refit without it. If R² barely drops, it was a redundant rollup
  — report the cluster, not the summary. (Ames example: dropping
  `Overall Qual` *increased* R² by 0.001 — components carried all the signal.)
- **Post-outcome leakage:** drop any variable computed from or after the
  target before fitting (e.g. predicting `stress_level` with
  `mental_health_index` in the same instrument).

See `feature-importance.md` § Anti-Patterns / Leakage.

## Pandas Rules

**Always do:**
- Specify `dtype=` on read to avoid silent object fallback
- Use `pd.to_datetime(..., errors='coerce')` and check for NaT
- Use `.loc[condition, col]` not chained indexing
- Method chain with `assign()`, `query()`, `pipe()`
- Validate joins with `validate=` parameter
- Check row count after every join/filter
- Use `category` dtype for low-cardinality string columns
- Use `Int64` (nullable) not `int64` when column can have NaN
- Prefer vectorized operations over `apply(axis=1)` or loops
- Use assignment `df = df.method()` not `df.method(inplace=True)`
- Run **all four Pre-Modeling Diagnostics** (collinearity / null / skew / tautology — see section above) during Explore, before any modeling
- When linear analysis yields R² < 0.4 or rankings disagree, cross-check with XGBoost + SHAP (see `feature-importance.md`) before reporting drivers
- For RCA / excursion / defect-investigation questions, run change-point detection before regression — a localized shift in time needs a localized cause, not a global feature ranking (see `root-cause-analysis.md`)
- **Default to Plotly** for any visualization (interactive HTML, hover, zoom). Use matplotlib/seaborn only when a static figure is explicitly required (publication, slide deck without HTML support).

**Never do:**
- Loop over rows when vectorized alternative exists
- `pd.concat()` inside a loop (collect then concat once)
- `df.append()` (removed in pandas 2.0)
- Chained assignment `df[mask]["col"] = value`
- Use `inplace=True` (deprecated in pandas 3.0)
- Use deprecated frequency aliases (`"M"` → `"ME"`, `"H"` → `"h"`, `"T"` → `"min"`)
- Set `pd.options.mode.copy_on_write` (always-on in pandas 3.0)

## Defensive Coding

```python
# Validate at every step
assert df.shape[0] > 0, "DataFrame is empty after filtering"
assert df["id"].nunique() == len(df), "Duplicate IDs found"
assert df["revenue"].isna().sum() == 0, "Missing revenue values"

# Log shape at each step
print(f"Loaded: {df.shape}")
print(f"After cleaning: {df.shape}")
print(f"After join: {df.shape}")
```

## Communication Standards

1. **Lead with the insight**, not the method
2. **Quantify uncertainty** — report sample size and significance
3. **Flag data quality issues** prominently (especially if data appears synthetic)
4. **Consistent formatting:** `$1.2M`, `12.3%`, `1,234,567`
5. **Actionable conclusions** — what it tells us, what to do next, limitations
