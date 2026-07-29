# RCA Commonality Analysis

Sensor-cluster shift analysis for time-localized excursions. Complement
to `root-cause-analysis.md` § 1 (CPD): commonality answers **which**
sensors / factors shifted; CPD answers **when**.

## Contents

- Collapse multicollinear sensor clusters first
- Per-categorical-factor commonality (Fisher's exact + BH-FDR)
- Multi-factor commonality (frequent-itemset mining)
- Multiplicity scope warning (BH-FDR across full sweep, not per-factor)

Read this file when working on: "which sensors moved when Y shifted",
"which lot / recipe / tool level over-represents in defects", or
"multi-factor combinations that predict failure". Pair with
`root-cause-analysis.md` § 1 for the pre/post window definition.

## Collapse multicollinear sensor clusters first

In manufacturing data, adjacent sensors measuring the same physical state
(chamber temp ↔ wall temp ↔ chuck temp; recipe pressure ↔ measured
pressure) are nearly perfectly correlated. **Without collapsing first,
your commonality table reports 8 members of one physical cluster as 8
separate findings** — useless for an engineer reading the report.

Use the framework in `collinearity-diagnostics.md` (the 5-priority
`select_cluster_representative()` helper), with one critical override:

> **Priority 3 override for RCA:** rank cluster members by **|shift
> magnitude at the change point|** in σ-units of the pre-period —
> *not* by overall target correlation. The most predictive sensor
> overall and the sensor that moved most when the excursion happened
> are often different. SECOM verified this empirically: SHAP top-10
> and CPD-commonality top-10 overlapped by 1 of 10.

```python
def shift_score(pre_df, post_df, col):
    """RCA-context Priority 3: |Δ| in σ-units of the pre-period."""
    pre = pre_df[col].dropna()
    post = post_df[col].dropna()
    if len(pre) < 30 or pre.std() == 0:
        return 0.0
    return abs(post.mean() - pre.mean()) / pre.std()

# Per cluster — pre_df / post_df defined by the change-point window
for cluster in find_clusters(X[sensor_cols], rho_threshold=0.85):
    keep, drop, reason = select_cluster_representative(
        X, y=None, cluster_cols=cluster,
        score_fn=lambda Xf, _y, c: shift_score(pre_df, post_df, c),
    )
    if keep is None:
        print(f"⚠ AMBIGUOUS cluster — flag for engineer review: {cluster}")
        continue
    print(f"cluster {cluster} → represented by {keep} ({reason})")
    sensor_cols = [c for c in sensor_cols if c not in drop]
```

Report cluster-level findings in the RCA narrative:

> "**Cluster of 8 chamber-state sensors** (s406, s540, s268, s405, s539,
> s267, s058, s007) shifted at the change point. Cluster represented by
> `s406` (Δ = +5.15, normalized +4.5σ). Treat as a single physical
> phenomenon when investigating root cause."

— *not* "8 individual sensors shifted," which dilutes the engineer's
attention across redundant measurements of the same physical event.

## Per-categorical-factor commonality

Once collinear sensor clusters are collapsed, run commonality on the
remaining set. For "*which categorical level appears in all bad lots but
few good lots?*" — set-overlap, not regression.

```python
from scipy.stats import fisher_exact

def commonality(df, factor_col, defect_col, defect_value=1, alpha=0.01):
    """Per-level Fisher's exact + odds ratio + lift. Bonferroni-corrected.

    Returns levels whose presence among defects vs. non-defects is
    significantly elevated.
    """
    rows = []
    levels = df[factor_col].dropna().unique()
    n_tests = len(levels)
    for lvl in levels:
        is_lvl = (df[factor_col] == lvl)
        is_def = (df[defect_col] == defect_value)
        a = int((is_lvl & is_def).sum())             # bad with this level
        b = int((is_lvl & ~is_def).sum())            # good with this level
        c = int((~is_lvl & is_def).sum())            # bad without
        d = int((~is_lvl & ~is_def).sum())           # good without
        if a + b == 0:
            continue
        odds, p = fisher_exact([[a, b], [c, d]], alternative="greater")
        # Lift: P(level | defect) / P(level)
        lift = (a / (a + c)) / ((a + b) / (a + b + c + d)) if (a + c) else 0
        rows.append({
            "level": lvl, "n_with_defect": a, "n_without_defect": b,
            "defect_rate_in_level": a / (a + b) if (a + b) else 0.0,
            "odds_ratio": float(odds), "lift": round(lift, 2),
            "p_fisher": float(p),
            "p_bonferroni": min(1.0, float(p) * n_tests),
        })
    return (pd.DataFrame(rows)
              .sort_values("p_bonferroni")
              .query(f"p_bonferroni < {alpha}")
              .reset_index(drop=True))

# Sweep across all categorical factors:
all_rows = []
for factor in categorical_cols:
    sig = commonality(df, factor, "is_defective", alpha=1.0)   # collect raw p-values
    if len(sig):
        sig["factor"] = factor
        all_rows.append(sig)

# Apply BH-FDR across the FULL sweep, not per-factor
if all_rows:
    from statsmodels.stats.multitest import multipletests
    full = pd.concat(all_rows, ignore_index=True)
    full["p_bh_fdr"] = multipletests(full["p_fisher"], method="fdr_bh")[1]
    print(full.query("p_bh_fdr < 0.05")
              .sort_values("p_bh_fdr")
              [["factor", "level", "defect_rate_in_level", "lift", "p_bh_fdr"]]
              .to_string(index=False))
```

**Multiplicity scope warning:** the `p_bonferroni` returned by `commonality()`
is **within-factor only** (factor's number of levels). For a wide sweep
across many factors — SECOM with 590 sensor categoricals, for example —
the right correction is BH-FDR applied to **all p-values across all
factors** as shown above, not the within-factor Bonferroni.

## Multi-factor commonality (frequent-itemset mining)

For "which *combination* of tool + recipe + lot appears in defects", use
frequent-itemset mining:

```python
# pip install mlxtend
from mlxtend.frequent_patterns import apriori, association_rules

# Encode each factor=level as a boolean column
encoded = pd.get_dummies(df[categorical_cols].astype(str), dtype=bool)
encoded["is_defective"] = (df["defect"] == 1)

freq = apriori(encoded, min_support=0.01, use_colnames=True)
rules = association_rules(freq, metric="lift", min_threshold=2.0)
defect_rules = rules[rules["consequents"].astype(str).str.contains("is_defective")]
print(defect_rules[["antecedents", "support", "confidence", "lift"]].head(10))
```
