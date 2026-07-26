# Pandas Data Exploration & EDA — Expert Skill

You are an expert data analyst. Apply this structured EDA methodology whenever
exploring an unfamiliar dataset with pandas >= 2.3.

## Step 0 — First Contact: Schema Discovery

When loading an unknown dataset, do a two-pass load: sample first to discover
the schema, then reload with optimized dtypes.

```python
# Pass 1: load a small sample with no dtype to discover schema
sample = pd.read_csv("file.csv", nrows=1000)
print(sample.dtypes)
print(sample.nunique())

# Decide categoricals (low-cardinality string columns)
str_cols = sample.select_dtypes(include=["object", "str"]).columns
cat_cols = [c for c in str_cols if sample[c].nunique() < 50]

# Pass 2: reload the full file with optimized dtypes
dtype_map = {c: "category" for c in cat_cols}
df = pd.read_csv("file.csv", dtype=dtype_map, low_memory=False)
print(f"Loaded: {df.shape[0]:,} rows × {df.shape[1]} cols, "
      f"{df.memory_usage(deep=True).sum()/1e6:.1f} MB")
```

For very large files (> 1GB), use `chunksize=` and aggregate iteratively
(see `data-loading.md`).

## Step 1 — Structural Overview

```python
# Shape and memory
print(f"Shape: {df.shape}")                  # (rows, cols)
print(f"Memory: {df.memory_usage(deep=True).sum() / 1e6:.1f} MB")

# Column types
print(df.dtypes)
print(df.info(memory_usage="deep"))          # compact full overview

# First / last rows
df.head(10)
df.tail(5)
df.sample(5, random_state=42)               # random rows (reproducible)
```

## Step 2 — Missing Data Audit

```python
missing = df.isnull().sum()
missing_pct = (missing / len(df) * 100).round(2)
missing_report = pd.DataFrame({
    "count": missing,
    "pct": missing_pct
}).query("count > 0").sort_values("pct", ascending=False)
print(missing_report)

# Visual pattern (requires missingno library, optional)
# import missingno as msno; msno.matrix(df)

# Which rows have ANY missing value?
df[df.isnull().any(axis=1)]
```

## Step 3 — Duplicates

```python
n_dupes = df.duplicated().sum()
print(f"Duplicate rows: {n_dupes} ({n_dupes/len(df)*100:.1f}%)")

# Duplicates on key subset
df.duplicated(subset=["user_id", "date"]).sum()

# Inspect duplicates
df[df.duplicated(keep=False)].sort_values(["user_id", "date"])
```

## Step 3.5 — Data-Quality Signature

Report a neutral readout of quality signals. These are **informative**, not a
binary synthetic-or-real verdict: well-curated real datasets (cleaned research
data, fact tables, feature stores) can hit several of these. When multiple
indicators fire, just verify provenance before generalizing conclusions.

```python
num = df.select_dtypes(include="number")
indicators = {
    "no missing values":       df.isnull().sum().sum() == 0,
    "no duplicates":           df.duplicated().sum() == 0,
    "no datetime column":      len(df.select_dtypes(include=["datetime64","datetimetz"]).columns) == 0,
    "integer-bounded numerics": len(num.columns) > 0 and (num.min() % 1 == 0).all() and (num.max() % 1 == 0).all(),
    "unique-per-row numerics": any(df[c].nunique() == len(df) for c in num.columns),
}
for key, val in indicators.items():
    print(f"  [{'x' if val else ' '}] {key}")
if sum(indicators.values()) >= 3:
    print("  → Verify provenance before generalizing statistical conclusions.")
```

## Step 4 — Descriptive Statistics

```python
# Numeric columns
df.describe()                    # count, mean, std, min, quartiles, max
df.describe(percentiles=[.05, .25, .5, .75, .95])  # custom percentiles

# Categorical / object columns
df.describe(include=["object", "str", "category"])

# All columns
df.describe(include="all")

# Per-column stats
df["amount"].agg(["mean", "median", "std", "skew", "kurt"])

# Value counts for categoricals
for col in df.select_dtypes(include=["object", "str", "category"]).columns:
    print(f"\n{col}:")
    print(df[col].value_counts(dropna=False).head(10))
    print(f"  Unique: {df[col].nunique()}")
```

## Step 5 — Distribution Analysis

### Step 5a — Point-mass / clipping detection (run BEFORE outlier checks)

Many real columns — revenue (non-purchasers), counts, time-censored values,
bounded surveys — have large fractions of rows pinned at min or max. On these
columns **IQR and moment-based outlier stats are misleading** (they flag the
right tail of a truncated distribution as "outliers"). Always check for point
masses first and skip IQR where present.

```python
numeric_cols = df.select_dtypes(include="number").columns
point_mass = {}
for col in numeric_cols:
    vmin, vmax = df[col].min(), df[col].max()
    at_min = (df[col] == vmin).mean()
    at_max = (df[col] == vmax).mean()
    if at_min > 0.01 or at_max > 0.01:
        point_mass[col] = {"at_min": at_min, "at_max": at_max}
        print(f"  {col}: {at_min:.1%} at min({vmin:.3g}), "
              f"{at_max:.1%} at max({vmax:.3g})")
```

### Step 5b — Skew & kurtosis

```python
import numpy as np
dist = pd.DataFrame({
    "skew": df[numeric_cols].skew(),
    "kurt": df[numeric_cols].kurt(),
})
print(dist.sort_values("skew", key=lambda s: s.abs(), ascending=False).round(3))
```

### Step 5c — IQR outliers (skipping point-mass columns)

```python
def iqr_bounds(s):
    q1, q3 = s.quantile([0.25, 0.75])
    iqr = q3 - q1
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr

for col in numeric_cols:
    if col in point_mass:
        continue   # IQR misleading on clipped distributions
    lo, hi = iqr_bounds(df[col])
    n_out = ((df[col] < lo) | (df[col] > hi)).sum()
    if n_out:
        print(f"{col}: {n_out} outliers (IQR method)")

# Z-score outliers (same caveat — skip point-mass columns)
from scipy import stats
ok_cols = [c for c in numeric_cols if c not in point_mass]
z = np.abs(stats.zscore(df[ok_cols].dropna()))
print(f"Rows with |z| > 3 in any non-clipped column: {(z > 3).any(axis=1).sum()}")
```

For distribution-shape testing (Shapiro, KS, D'Agostino-Pearson), see
`statistical-analysis.md`.

## Step 6 — Correlation Analysis

**Prefer Spearman by default** for EDA. It's rank-based, so it handles
skewed, clipped, or zero-inflated columns without distortion. Use Pearson
only when you have evidence the relationships are linear.

```python
# Spearman pairs with |rho| > 0.3
corr = df[numeric_cols].corr(method="spearman")
mask = np.triu(np.ones(corr.shape), k=1).astype(bool)
pairs = (corr.where(mask).stack()
    .reset_index()
    .rename(columns={"level_0": "a", "level_1": "b", 0: "rho"}))
strong = pairs.loc[pairs["rho"].abs() > 0.3].sort_values(
    "rho", key=lambda s: s.abs(), ascending=False
)
print(strong.round(3))

# Correlation with a target variable (when the user specified one)
df[numeric_cols].corrwith(df["target"], method="spearman").sort_values()
```

### Uncorrelated columns (neutral report)

Columns that show |ρ| < 0.05 against **every** other numeric column deserve a
second look. They may be IDs, independent factors, the target itself, or
noise. Report them — do **not** auto-drop.

```python
self_masked = corr.abs().where(~np.eye(len(corr), dtype=bool))
loners = self_masked.max()[self_masked.max() < 0.05].index.tolist()
if loners:
    print(f"Uncorrelated with all others: {loners}")
    print("  → verify whether these are IDs, independent factors, or targets")
```

### Multicollinearity audit (VIF) — run on every dataset

VIF (Variance Inflation Factor) is **not optional** and **not deferred to
modeling time**. Run it as part of every Explore step, before any
correlation interpretation or driver analysis. Pairwise ρ misses
multi-way redundancy: feature A may have ρ < 0.3 with every other feature
individually but still be a perfect linear combination of three of them
(VIF = ∞). The Ames Housing example: `Gr Liv Area` had moderate pairwise
ρ values but VIF = ∞ because it equals `1st Flr SF + 2nd Flr SF + Low
Qual Fin SF` exactly.

```python
import numpy as np

def vif_table(X: pd.DataFrame) -> pd.DataFrame:
    """VIF_j = 1 / (1 - R²_j), R²_j from regressing column j on the others."""
    Xv = X.to_numpy(dtype=np.float64)
    rows = []
    for j, col in enumerate(X.columns):
        y = Xv[:, j]
        Xrest = np.delete(Xv, j, axis=1)
        Xd = np.column_stack([np.ones(len(Xrest)), Xrest])
        beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        yhat = Xd @ beta
        ss_tot = ((y - y.mean()) ** 2).sum()
        if ss_tot == 0:
            r2, vif = 1.0, float("inf")
        else:
            r2 = 1 - ((y - yhat) ** 2).sum() / ss_tot
            vif = float("inf") if r2 >= 0.9999 else 1 / (1 - r2)
        rows.append({"feature": col, "R²_on_others": round(float(r2), 3),
                     "VIF": round(float(vif), 2)})
    return pd.DataFrame(rows).sort_values("VIF", ascending=False).reset_index(drop=True)

vif = vif_table(df[numeric_cols].dropna())
print(vif.to_string(index=False))
severe   = vif.query("VIF > 10")["feature"].tolist()
moderate = vif.query("5 < VIF <= 10")["feature"].tolist()
if severe:
    print(f"⚠ SEVERE multicollinearity: {severe}")
    print("  → drop one of each redundant pair OR plan to use RidgeCV / LassoCV")
elif moderate:
    print(f"⚠ Moderate multicollinearity: {moderate}")
    print("  → trust SHAP/permutation rankings over OLS β if you model later")
```

Always **report VIF as part of the EDA findings**, even if no modeling
follows — VIF tells stakeholders whether two metrics they think are
distinct are actually redundant. The Pre-Modeling Diagnostics section in
`feature-importance.md` covers Ridge/Lasso fallback patterns and the full
decision rules for VIF tiers.

### Mixed-type collinearity — when categoricals exist

Numeric-only VIF is **incomplete on real datasets**. Many numeric columns
are *bound* to categorical columns: when a categorical encodes "feature
present?" and a related numeric encodes "feature magnitude," they carry
the same binary signal even though their pairwise ρ looks moderate.

The Ames Housing case: `Garage Yr Blt` (numeric) and `Garage Finish`
(categorical) have **η² = 0.998** — practically deterministic. Both are
"absent" together when there's no garage. Numeric-only VIF rates
`Garage Yr Blt` at VIF ≈ 1, completely missing this. With one-hot
expansion the design-matrix VIF jumps to 1614.

Run **two checks** to cover the gap:

#### A. Mixed-type VIF — design matrix with one-hot dummies

```python
def mixed_type_vif(X, num_cols, cat_cols, max_cardinality=15):
    """VIF on the full design matrix (numeric + one-hot dummies of categoricals).

    drop_first=True is required — without it dummies of one categorical sum
    to 1 and are perfectly collinear by construction.

    High-cardinality categoricals (> max_cardinality levels) bloat the design
    matrix and rarely yield interpretable per-dummy VIFs; skip them and use
    cross_type_binding() for those instead.

    Returns:
      per_source:  DataFrame with [source, max_VIF, mean_VIF, n_features]
      per_feature: DataFrame with [feature, source, R²_on_others, VIF]
    """
    cat_low = [c for c in cat_cols if X[c].nunique() <= max_cardinality]
    cat_skip = [c for c in cat_cols if c not in cat_low]
    if cat_skip:
        print(f"Skipped {len(cat_skip)} high-cardinality cats from VIF "
              f"(use cross_type_binding instead): {cat_skip[:5]}"
              f"{'...' if len(cat_skip) > 5 else ''}")

    X_design = pd.get_dummies(X[num_cols + cat_low], columns=cat_low,
                              drop_first=True, dtype="float64")
    per_feature = vif_table(X_design)

    source_map = {c: c for c in num_cols}
    for src in cat_low:
        prefix = f"{src}_"
        for c in X_design.columns:
            if c.startswith(prefix):
                source_map[c] = src
    per_feature["source"] = per_feature["feature"].map(source_map)

    per_source = (per_feature.groupby("source")
                  .agg(max_VIF=("VIF", "max"),
                       mean_VIF=("VIF", "mean"),
                       n_features=("feature", "count"))
                  .sort_values("max_VIF", ascending=False)
                  .reset_index())
    return per_source, per_feature

per_source, per_feature = mixed_type_vif(df, numeric_cols, categorical_cols)
print(per_source.head(20).round(2).to_string(index=False))
new_severe = per_source.query("max_VIF > 10")["source"].tolist()
print(f"\nSources with severe design-matrix VIF: {new_severe}")
```

**Caveat — VIF=∞ from shared structural-absence levels:** when several
categoricals share a "None" level for the same physical absence (e.g.
`Bsmt Qual_None`, `Bsmt Cond_None`, `BsmtFin Type 1_None` in Ames are
all 1 for properties with no basement), their dummies become perfectly
collinear by construction. Report this as "X features carry the
same 'is absent?' signal" rather than as N independent multicollinear
sources.

#### B. Cross-type binding — η² and Cramér's V

For high-cardinality categoricals (skipped from VIF) and as a complement
that's easier to interpret across types, compute pairwise associations:

```python
from scipy import stats

def cross_type_binding(X, num_cols, cat_cols, threshold=0.5):
    """Detect strong bindings between columns, including across types.

    Returns DataFrame with columns [a, b, score, kind] for pairs above
    threshold:
      - num↔cat:  score = η²  (variance in num explained by cat groups)
      - cat↔cat:  score = Cramér's V
      - num↔num:  score = ρ²  (Spearman, only if > threshold)
    """
    rows = []

    # numeric ↔ categorical: η²
    for n in num_cols:
        for c in cat_cols:
            d = pd.DataFrame({"x": X[n], "g": X[c]}).dropna()
            if len(d) < 5 or d["g"].nunique() < 2:
                continue
            grand = d["x"].mean()
            ss_tot = ((d["x"] - grand) ** 2).sum()
            if ss_tot == 0:
                continue
            ss_btw = (d.groupby("g", observed=True)["x"]
                       .apply(lambda s: len(s) * (s.mean() - grand) ** 2)
                       .sum())
            eta2 = float(ss_btw / ss_tot)
            if eta2 > threshold:
                rows.append({"a": n, "b": c, "score": round(eta2, 3),
                             "kind": "η² (num↔cat)"})

    # categorical ↔ categorical: Cramér's V
    for i, a in enumerate(cat_cols):
        for b in cat_cols[i + 1:]:
            d = pd.DataFrame({"a": X[a], "b": X[b]}).dropna()
            if len(d) < 5 or d["a"].nunique() < 2 or d["b"].nunique() < 2:
                continue
            ct = pd.crosstab(d["a"], d["b"])
            chi2, *_ = stats.chi2_contingency(ct)
            n = ct.sum().sum()
            denom = n * max(min(ct.shape) - 1, 1)
            v = float(np.sqrt(chi2 / denom)) if denom > 0 else 0.0
            if v > threshold:
                rows.append({"a": a, "b": b, "score": round(v, 3),
                             "kind": "Cramér's V (cat↔cat)"})

    # numeric ↔ numeric: ρ² (only if exceeds threshold)
    for i, a in enumerate(num_cols):
        for b in num_cols[i + 1:]:
            rho = X[[a, b]].corr(method="spearman").iloc[0, 1]
            if pd.notna(rho) and rho * rho > threshold:
                rows.append({"a": a, "b": b, "score": round(rho * rho, 3),
                             "kind": "ρ² (num↔num)"})

    return (pd.DataFrame(rows)
              .sort_values("score", ascending=False)
              .reset_index(drop=True))

bindings = cross_type_binding(df, numeric_cols, categorical_cols, threshold=0.5)
print(bindings.head(20).to_string(index=False))
```

#### Decision rule

| Situation | Use |
|---|---|
| Numeric-only dataset | `vif_table(X)` is sufficient |
| Mixed numeric + low-cardinality categorical | `mixed_type_vif()` for source-level VIF |
| High-cardinality categoricals present (>15 levels) | `cross_type_binding()` for those |
| Quick "is there cross-type redundancy at all?" check | `cross_type_binding(threshold=0.5)` — single call |
| Production diagnostic | All three; report any source with max VIF > 10 OR any binding score > 0.7 |

**Rule of thumb:** η² > 0.5 between a numeric and a categorical means the
numeric carries no signal beyond the categorical group means — drop one
side before modeling. η² > 0.9 means deterministic binding (e.g. Ames
`Pool Area` ↔ `Pool QC` at η² = 0.94 — Pool Area is 0 exactly when
Pool QC is "None").

### Selecting cluster representatives — when you must drop one

When VIF flags a multicollinear cluster and the downstream method can't
tolerate it (OLS coefficient interpretation, Lasso feature selection,
RCA commonality reporting), you have to pick one representative and drop
the rest. **Picking arbitrarily is the most common silent error** — the
"kept" feature determines what story the analysis tells.

Apply this priority order:

| Priority | Rule | Why |
|---|---|---|
| 1 | If one column ≈ Σ(others) with R² ≥ 0.99, drop that aggregate, keep components | Components carry strictly more granular information |
| 2 | If a column name matches summary patterns (`overall`, `total`, `index`, `score`, `rating`, `summary`) and per-component features exist, drop the summary | Avoid the rating-tautology trap (Ames Overall Qual case) |
| 3 | **Context-specific (override below):** rank by `|ρ(target)|` on training fold, keep highest | Most useful for the analysis goal |
| 4 | Tiebreak: fewer nulls | Less imputation noise |
| 5 | Final tiebreak: higher coefficient of variation | More dynamic range, more information |
| 6 | If still tied within 5%, FLAG for human review — do not auto-decide | Domain knowledge required |

**Priority 3 changes per analysis context:**

| Context | Score by |
|---|---|
| Feature importance / driver analysis | `|ρ(target)|` on training fold (default) |
| RCA / change-point / commonality | `|Δ at change point|` in σ-units of the pre-period |
| Pure EDA without a target | Variance after standardization |

```python
import numpy as np
import pandas as pd

SUMMARY_PATTERNS = ("overall", "total", "index", "score", "rating", "summary")


def find_aggregate(X, cluster_cols, threshold=0.99):
    """Return the cluster member best predicted by the rest (R² ≥ threshold).

    When multiple members satisfy the threshold (e.g. the perfect-sum case
    `agg = a + b + c` where any of the four can be predicted from the
    other three), prefer the one with the **largest mean magnitude** —
    aggregates tend to be sums and are numerically larger than their
    components. This produces the more interpretable choice for human
    readers.
    """
    Xc = X[cluster_cols].dropna()
    if len(Xc) < len(cluster_cols) + 1:
        return None
    candidates = []
    for col in cluster_cols:
        others = [c for c in cluster_cols if c != col]
        Xrest = Xc[others].to_numpy(dtype=np.float64)
        y = Xc[col].to_numpy(dtype=np.float64)
        Xd = np.column_stack([np.ones(len(Xrest)), Xrest])
        beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        ss_tot = ((y - y.mean()) ** 2).sum()
        if ss_tot == 0:
            continue
        r2 = 1 - ((y - Xd @ beta) ** 2).sum() / ss_tot
        if r2 >= threshold:
            candidates.append((col, abs(float(Xc[col].mean()))))
    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[1])             # largest magnitude first
    return candidates[0][0]


def select_cluster_representative(
    X, y, cluster_cols, *,
    fold_mask=None,
    score_fn=None,
):
    """Returns (keep_list, drop_list, reason).

    score_fn(X_fold, y_fold, col) → float — higher is better.
    Default: |ρ(target)| via Spearman; falls back to variance if y is None.
    """
    Xf = X.loc[fold_mask] if fold_mask is not None else X
    yf = (y.loc[fold_mask] if (y is not None and fold_mask is not None)
          else (y if y is not None else None))

    # Priority 1: aggregate-vs-components
    agg = find_aggregate(Xf, cluster_cols)
    if agg is not None:
        components = [c for c in cluster_cols if c != agg]
        return components, [agg], f"aggregate dropped: {agg} ≈ Σ({components})"

    # Priority 2: summary names
    summaries = [c for c in cluster_cols
                 if any(p in c.lower() for p in SUMMARY_PATTERNS)]
    components = [c for c in cluster_cols if c not in summaries]
    if summaries and components:
        return components, summaries, f"summary names dropped: {summaries}"

    # Priority 3: context-aware score
    if score_fn is None:
        if yf is None:
            score_fn = lambda Xf, _y, c: float(Xf[c].std())
        else:
            score_fn = lambda Xf, yf, c: abs(
                float(Xf[c].corr(yf, method="spearman"))
            )

    scores = (pd.Series({c: score_fn(Xf, yf, c) for c in cluster_cols})
                .sort_values(ascending=False))
    if len(scores) == 1:
        return [scores.index[0]], [], "single member"

    top = scores.iloc[0]
    runner_up = scores.iloc[1]

    # Priority 4-5: tiebreak when top-2 within 5%
    if top > 0 and (top - runner_up) / top < 0.05:
        candidates = scores[scores >= runner_up * 0.95].index.tolist()
        tie = pd.Series({
            c: (1 - X[c].isna().mean())
               * (X[c].std() / (abs(X[c].mean()) + 1e-9))
            for c in candidates
        }).sort_values(ascending=False)
        if len(tie) > 1 and tie.iloc[0] > 0 \
           and (tie.iloc[0] - tie.iloc[1]) / tie.iloc[0] < 0.05:
            return None, None, f"AMBIGUOUS — manual review needed: {candidates}"
        keep = tie.index[0]
    else:
        keep = scores.index[0]

    drop_list = [c for c in cluster_cols if c != keep]
    return [keep], drop_list, f"kept {keep} (score={scores[keep]:.3f})"
```

**Workflow** — identify clusters (group features with pairwise |ρ| ≥ 0.85),
call the selector per cluster, then re-audit VIF on what remains:

```python
from scipy.cluster.hierarchy import linkage, fcluster

def find_clusters(X, rho_threshold=0.85):
    """Hierarchical clustering on |corr|. Returns list of column lists."""
    corr = X.corr(method="spearman").abs().fillna(0)
    dist = 1 - corr
    Z = linkage(dist.values[np.triu_indices_from(dist.values, k=1)], method="average")
    labels = fcluster(Z, t=1 - rho_threshold, criterion="distance")
    clusters = [list(corr.columns[labels == lbl]) for lbl in set(labels)]
    return [c for c in clusters if len(c) > 1]

for cluster in find_clusters(X[numeric_cols], rho_threshold=0.85):
    keep, drop, reason = select_cluster_representative(X, y, cluster)
    if keep is None:
        print(f"⚠ AMBIGUOUS cluster — flag for review: {cluster}")
        continue
    print(f"cluster {cluster} → keep {keep} ({reason})")
    numeric_cols = [c for c in numeric_cols if c not in drop]

print(vif_table(X[numeric_cols]))                  # re-audit; expect VIF < 5 throughout
```

After dropping, **re-run `vif_table` to confirm** the kept features' VIFs
returned below 5. If a feature still shows VIF > 5, the cluster wasn't
captured — extend the cluster (lower `rho_threshold`) and retry.

## Step 6.5 — Derived-Column / Target-Leakage Check

A categorical column that is strictly a function of some numeric column is a
common form of **target leakage** in supervised pipelines — and a common
source of silly model-accuracy claims. Detect it by binning each numeric
column to match the categorical's cardinality and measuring row-level purity
of the resulting cross-tab.

```python
cat_cols = df.select_dtypes(include=["category", "object", "str"]).columns
derived = []
for cat in cat_cols:
    card = df[cat].nunique()
    if card < 2 or card > 20:
        continue
    for nc in numeric_cols:
        try:
            bins = pd.qcut(df[nc], q=card, labels=False, duplicates="drop")
        except ValueError:
            continue
        purity = (pd.crosstab(bins, df[cat], normalize="index")
                  .max(axis=1).mean())
        if purity > 0.90:
            derived.append((cat, nc, round(float(purity), 3)))

for cat, nc, p in sorted(derived, key=lambda x: -x[2]):
    print(f"  {cat} ≈ f({nc})   row-purity={p}")
```

If any pair shows row-purity > 0.9, flag it: the categorical is (nearly) a
deterministic function of the numeric — do not use it as a feature alongside
the numeric when predicting anything downstream of that numeric.

## Step 7 — Temporal Overview (if dates present)

```python
# Detect date columns
date_cols = df.select_dtypes(include=["datetime64", "datetimetz"]).columns

for col in date_cols:
    print(f"\n{col}:")
    print(f"  Range: {df[col].min()} → {df[col].max()}")
    print(f"  Span:  {df[col].max() - df[col].min()}")
    print(f"  NaT:   {df[col].isnull().sum()}")

# Time-based record counts
if len(date_cols) > 0:
    date_col = date_cols[0]
    print(df.set_index(date_col).resample("ME").size())   # monthly counts
```

## Step 8 — Cardinality & Type Audit

```python
cardinality = df.nunique().sort_values()
print(cardinality)

# Columns where all values are the same (useless)
constant_cols = cardinality[cardinality == 1].index.tolist()
print(f"Constant columns (drop candidates): {constant_cols}")

# Columns with very high cardinality (likely IDs)
id_like = cardinality[cardinality == len(df)].index.tolist()
print(f"Unique-per-row columns (ID candidates): {id_like}")
```

## Step 8.5 — Memory Optimization

After profiling, optimize dtypes before heavier analysis. This pays off
substantially on million-row datasets.

```python
mem_before = df.memory_usage(deep=True).sum() / 1e6

# 1. Convert low-cardinality string columns to category
for col in df.select_dtypes(include=["object", "str"]).columns:
    if df[col].nunique() / len(df) < 0.05:        # < 5% unique
        df[col] = df[col].astype("category")

# 2. Downcast numeric columns where range permits
for col in df.select_dtypes(include="integer").columns:
    df[col] = pd.to_numeric(df[col], downcast="integer")
for col in df.select_dtypes(include="float").columns:
    df[col] = pd.to_numeric(df[col], downcast="float")

mem_after = df.memory_usage(deep=True).sum() / 1e6
print(f"Memory: {mem_before:.1f} MB → {mem_after:.1f} MB "
      f"({(1 - mem_after/mem_before):.0%} reduction)")
```

## Step 9 — Generating a One-Shot EDA Script

When facing a new dataset, **generate a tailored EDA script** that runs
every step above in order. Do not cherry-pick steps; the most common
failure mode is forgetting to check for point masses before computing
outliers, or forgetting to validate the target distribution.

A reusable template:

```python
def eda_report(df: pd.DataFrame, target: str | None = None) -> dict:
    """Run the full EDA workflow above. Returns dict with shape, memory,
    missing_cols, duplicates, point_mass, iqr_outliers, spearman_pairs,
    cardinality, target_analysis, and a checklist of completed steps."""
    findings = {}
    # 1. Structure (Step 1)
    findings["shape"] = df.shape
    findings["memory_mb"] = df.memory_usage(deep=True).sum() / 1e6
    # 2. Missing (Step 2) — store columns with > 0 nulls
    findings["missing_cols"] = df.columns[df.isnull().any()].tolist()
    # 3. Duplicates (Step 3)
    findings["duplicates"] = int(df.duplicated().sum())
    # 3.5. Quality signature (Step 3.5)
    # 4. describe() (Step 4)
    # 5a. Point-mass detection (CRITICAL — must run before 5c)
    # 5b. Skew/kurtosis (Step 5b)
    # 5c. IQR outliers (Step 5c — skip point_mass columns!)
    # 6. Spearman correlations > 0.3 (Step 6)
    # 6.5. Derived-column check (Step 6.5)
    # 7. Temporal overview if datetime present (Step 7)
    # 8. Cardinality + ID detection (Step 8)
    # Target-specific analysis when provided
    if target and target in df.columns:
        findings["target_corr"] = (
            df.select_dtypes("number")
              .corrwith(df[target], method="spearman")
              .sort_values(key=abs, ascending=False)
        )
    findings["checklist"] = {...}        # boolean per step
    return findings

findings = eda_report(df, target="my_target_column")
```

Ask the user for the target column when it isn't obvious. Do **not** guess
it from column names — domain-specific targets (`conversion`, `churned`,
`nps`) defeat name heuristics and guessing wastes a pass.

For an HTML report with histograms, the optional `ydata-profiling` library
still works: `ProfileReport(df).to_file("eda.html")`.

## Pandas Built-in Plotting for EDA

```python
import matplotlib.pyplot as plt

# Distribution of all numeric columns
df[numeric_cols].hist(bins=30, figsize=(14, 8))
plt.tight_layout(); plt.show()

# Correlation heatmap
import seaborn as sns
sns.heatmap(corr, annot=True, fmt=".2f", cmap="coolwarm", center=0)

# Box plots for outlier detection
df[numeric_cols].plot(kind="box", subplots=True, figsize=(14, 6))

# Value counts bar chart
df["category"].value_counts().plot(kind="bar")
```

## EDA Checklist

Whether you ran each step manually or via a generated `eda_report`, verify
every item below before handing results back to the user. The most common
EDA failure modes are skipping the point-mass check (which makes IQR
outliers misleading) and forgetting to analyze the target's distribution.

- [ ] Shape / memory / dtypes reported
- [ ] Missing values quantified per column
- [ ] Duplicate rows counted
- [ ] Descriptive stats for numeric columns
- [ ] **Point-mass columns reported (before outlier detection)**
- [ ] Outliers reported (IQR, skipping point-mass columns)
- [ ] Skew / kurtosis reported
- [ ] Pairwise Spearman correlations > 0.3 reported
- [ ] **VIF audit run on numeric features; VIF > 5 flagged, VIF > 10 severe**
- [ ] **Mixed-type collinearity audit when categoricals present** — `mixed_type_vif()` per-source max VIF, `cross_type_binding()` η²/Cramér's V; pairs with η² > 0.5 or score > 0.7 reported
- [ ] Cardinality checked (constants and ID-like columns)
- [ ] Target variable analyzed (if one was provided)

If any item isn't checked, return to that step before reporting findings.
