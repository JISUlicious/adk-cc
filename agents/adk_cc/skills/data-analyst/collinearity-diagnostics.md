# Collinearity Diagnostics — VIF, Mixed-Type, Cluster Selection

The multicollinearity machinery of the Explore step. Pairwise correlation
misses multi-way redundancy; these audits catch it before it corrupts any
driver analysis or correlation interpretation. **Not optional, not deferred
to modeling time** — run during Explore.

## Contents

- Multicollinearity audit (VIF) — numeric, run on every dataset
- Mixed-type collinearity — when categoricals exist (η² / Cramér's V)
- Selecting cluster representatives — the 5-priority chooser

Read this file when working on: VIF / multicollinearity, "which of these
correlated features do I keep", mixed numeric+categorical redundancy, or
the Pre-Modeling collinearity audit that `SKILL.md` requires. Called from
`data-exploration.md` Step 6 and `rca-commonality.md`.

**To run the audit rather than read about it**, use the shipped probes —
they implement everything below and already guard the degenerate cases:

```bash
python scripts/collinearity_probe.py <data> --target <col>      # all three layers
python scripts/cluster_representative.py <data> --target <col>  # resolve a cluster
```

The code in this file is the reference implementation and the explanation of
*why* each layer exists. Reach for it when you need to adapt the method —
a custom score function, a different clustering rule, embedding the check
inside a larger pipeline. For the standard audit, run the probe.

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

