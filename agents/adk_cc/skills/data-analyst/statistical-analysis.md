# Pandas Statistical Analysis — Expert Skill

You are an expert data analyst. Apply the following statistical techniques
precisely using pandas >= 2.3 and scipy/numpy where needed.

## Contents

- Descriptive statistics
- Aggregation
- Distribution analysis (moments, normality, fit, ECDF)
- Correlation analysis (Pearson / Spearman / Kendall)
- Hypothesis testing
- A/B test analysis
- Cohort analysis
- Ranking and percentiles
- Summary statistics table (report-ready)

## Descriptive Statistics

```python
# Full summary
df.describe()                                         # count, mean, std, min, Q1-Q3, max
df.describe(percentiles=[.05, .1, .25, .5, .75, .9, .95])
df.describe(include="all")                            # includes object/category cols

# Per-column statistics
df["revenue"].mean()
df["revenue"].median()
df["revenue"].std()
df["revenue"].var()
df["revenue"].sem()           # standard error of mean
df["revenue"].mad()           # mean absolute deviation (pandas < 2.0)

# Pandas 2.0+ robust alternatives to mad()
(df["revenue"] - df["revenue"].median()).abs().median()   # MAD
(df["revenue"] - df["revenue"].mean()).abs().mean()       # Mean Absolute Deviation

df["revenue"].skew()          # skewness (0 = symmetric)
df["revenue"].kurt()          # excess kurtosis (0 = normal)
df["revenue"].quantile([.25, .5, .75])
df["revenue"].mode()[0]       # most frequent value
df["revenue"].nunique()       # count distinct values
```

## Aggregation

```python
# Multiple statistics at once
df["revenue"].agg(["mean", "median", "std", "min", "max", "count"])

# Custom functions in agg
df["revenue"].agg(
    mean="mean",
    p50="median",
    p90=lambda x: x.quantile(0.9),
    p99=lambda x: x.quantile(0.99),
    cv=lambda x: x.std() / x.mean(),         # coefficient of variation
    iqr=lambda x: x.quantile(0.75) - x.quantile(0.25),
)

# GroupBy aggregation
df.groupby("segment").agg(
    n=("user_id", "count"),
    revenue_total=("revenue", "sum"),
    revenue_avg=("revenue", "mean"),
    revenue_med=("revenue", "median"),
    revenue_p90=("revenue", lambda x: x.quantile(0.9)),
    revenue_std=("revenue", "std"),
)
```

## Distribution Analysis

```python
import numpy as np
from scipy import stats

col = df["revenue"].dropna()

# Moments
print(f"Mean:     {col.mean():.2f}")
print(f"Median:   {col.median():.2f}")
print(f"Std:      {col.std():.2f}")
print(f"Skew:     {col.skew():.3f}")   # >0 = right-skewed, <0 = left-skewed
print(f"Kurtosis: {col.kurt():.3f}")   # >0 = heavy tails

# Normality tests
stat, p = stats.shapiro(col.sample(min(len(col), 5000)))  # Shapiro-Wilk
print(f"Shapiro-Wilk: stat={stat:.4f}, p={p:.4f}")
# p < 0.05 → reject normality

stat, p = stats.normaltest(col)   # D'Agostino-Pearson
print(f"Normaltest: stat={stat:.4f}, p={p:.4f}")

# Fit a distribution
mu, std = stats.norm.fit(col)
print(f"Best-fit Normal: μ={mu:.2f}, σ={std:.2f}")

# Empirical CDF
ecdf = col.rank(pct=True)    # percentile rank of each value

# Percentile ranks
df["revenue_percentile"] = df["revenue"].rank(pct=True) * 100
```

## Correlation Analysis

```python
# Pearson (linear relationships)
corr_matrix = df[numeric_cols].corr(method="pearson")

# Spearman (monotonic, robust to outliers)
corr_matrix = df[numeric_cols].corr(method="spearman")

# Kendall (small samples, ordinal data)
corr_matrix = df[numeric_cols].corr(method="kendall")

# Correlation with target
df[numeric_cols].corrwith(df["target"], method="spearman").sort_values()

# Correlation between two specific series
r, p = stats.pearsonr(df["x"].dropna(), df["y"].dropna())
rho, p = stats.spearmanr(df["x"].dropna(), df["y"].dropna())
print(f"Pearson r={r:.3f}, p={p:.4f}")

# Point-biserial (continuous vs binary)
r, p = stats.pointbiserialr(df["binary_col"], df["continuous_col"])
```

When fitting OLS on a correlation matrix with several |ρ| > 0.7 pairs,
run a VIF audit before interpreting coefficients (`feature-importance.md`
§ Pre-Modeling Diagnostics). High inter-predictor correlation
makes individual β values unstable — the cross-method check there is
the safe path.

## Hypothesis Testing

### Two-Sample Tests

```python
from scipy import stats

group_a = df.loc[df["variant"] == "A", "revenue"].dropna()
group_b = df.loc[df["variant"] == "B", "revenue"].dropna()

# Welch t-test — tests difference in MEANS (robust to non-normality at n>30 via CLT)
t_stat, p_value = stats.ttest_ind(group_a, group_b, equal_var=False)
print(f"Welch t-test: t={t_stat:.3f}, p={p_value:.4f}  ← null: mean(A)==mean(B)")

# Mann-Whitney U — tests STOCHASTIC DOMINANCE (P(A > B) == 0.5), not means
u_stat, p_value = stats.mannwhitneyu(group_a, group_b, alternative="two-sided")
print(f"Mann-Whitney: U={u_stat:.0f}, p={p_value:.4f}  ← null: P(A>B)==0.5")

# IMPORTANT: pick the test that matches the quantity you report.
# If you report "+X% lift on the mean", use Welch (or bootstrap on means).
# If you report a median lift / win rate, use Mann-Whitney.
# Mixing — reporting mean lift but testing Mann-Whitney — is a foot-gun.

# Effect size (Cohen's d, properly pooled for unequal sample sizes)
def cohens_d(a, b):
    n_a, n_b = len(a), len(b)
    s_a, s_b = a.std(ddof=1), b.std(ddof=1)
    pooled_var = ((n_a - 1) * s_a**2 + (n_b - 1) * s_b**2) / (n_a + n_b - 2)
    return (a.mean() - b.mean()) / np.sqrt(pooled_var)

d = cohens_d(group_b, group_a)
print(f"Cohen's d: {d:.3f}")  # rough rules of thumb: 0.2 small, 0.5 medium, 0.8 large
# Effect-size thresholds are domain-dependent. d=0.2 is huge in marketing,
# modest in psychology. Report d alongside p-value; interpret in context.
```

### Chi-Square Test (categorical vs categorical)

```python
contingency = pd.crosstab(df["variant"], df["converted"])
chi2, p, dof, expected = stats.chi2_contingency(contingency)
print(f"Chi-square: χ²={chi2:.3f}, df={dof}, p={p:.4f}")

# Cramér's V (effect size)
n = contingency.sum().sum()
cramers_v = np.sqrt(chi2 / (n * (min(contingency.shape) - 1)))
print(f"Cramér's V: {cramers_v:.3f}")
```

### One-Way ANOVA (multiple groups)

```python
groups = [group["revenue"].dropna().values for _, group in df.groupby("segment")]
f_stat, p_value = stats.f_oneway(*groups)
print(f"ANOVA: F={f_stat:.3f}, p={p_value:.4f}")

# Kruskal-Wallis (non-parametric ANOVA)
h_stat, p_value = stats.kruskal(*groups)
print(f"Kruskal-Wallis: H={h_stat:.3f}, p={p_value:.4f}")
```

### Categorical × Numeric Screen (Kruskal-Wallis across all pairs)

When you want to quickly learn **which categorical columns actually explain
variation in numeric columns** — and conversely, which categoricals are
effectively noise — run a Kruskal-Wallis across every (categorical, numeric)
pair. Non-parametric, robust to non-normal or clipped distributions, and
cheap on millions of rows.

```python
from scipy import stats

cat_cols = df.select_dtypes(include=["category", "object", "str"]).columns
num_cols = df.select_dtypes(include="number").columns

rows = []
for cat in cat_cols:
    if df[cat].nunique() < 2 or df[cat].nunique() > 50:
        continue
    for num in num_cols:
        groups = [g[num].dropna().values
                  for _, g in df.groupby(cat, observed=True)]
        if sum(len(g) > 0 for g in groups) < 2:
            continue
        h, p = stats.kruskal(*groups)
        rows.append((cat, num, h, p))

screen = (pd.DataFrame(rows, columns=["cat", "num", "H", "p"])
            .sort_values("p"))
print("Significant (p < 0.01):")
print(screen.query("p < 0.01").to_string(index=False))

# Categoricals that don't separate any numeric column — likely noise/bookkeeping
uninformative = (screen.groupby("cat")["p"].min()
                 .loc[lambda s: s > 0.05].index.tolist())
print(f"\nCategoricals with no significant effect on any numeric: {uninformative}")
```

## A/B Test Analysis

```python
def ab_test_report(df, variant_col, metric_col, control="A", treatment="B",
                   n_boot=10_000, seed=42):
    """A/B test report. Tests difference in MEANS (matches the lift number).

    Returns Welch t-test for inference, bootstrap CI for uncertainty,
    Mann-Whitney as a supplementary check on stochastic dominance.
    """
    ctrl  = df.loc[df[variant_col] == control,   metric_col].dropna().to_numpy()
    treat = df.loc[df[variant_col] == treatment, metric_col].dropna().to_numpy()

    # Summary
    print(f"Control   n={len(ctrl):,}  mean={ctrl.mean():.4f}  median={np.median(ctrl):.4f}")
    print(f"Treatment n={len(treat):,}  mean={treat.mean():.4f}  median={np.median(treat):.4f}")
    lift = (treat.mean() - ctrl.mean()) / ctrl.mean()
    print(f"Relative lift on mean: {lift:+.2%}")

    # Primary inference — Welch t-test on MEANS (matches the lift reported)
    t_stat, p_welch = stats.ttest_ind(treat, ctrl, equal_var=False)
    print(f"Welch t-test p={p_welch:.4f} {'✓ SIGNIFICANT' if p_welch < 0.05 else '✗ not significant'}")

    # Bootstrap 95% CI for difference in means (reproducible)
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        diffs[i] = (rng.choice(treat, len(treat), replace=True).mean()
                  - rng.choice(ctrl,  len(ctrl),  replace=True).mean())
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    print(f"95% bootstrap CI for mean diff: [{lo:+.4f}, {hi:+.4f}]")

    # Supplementary — Mann-Whitney on stochastic dominance (different null)
    _, p_mw = stats.mannwhitneyu(treat, ctrl, alternative="two-sided")
    print(f"Mann-Whitney (P(treat>ctrl)==0.5) p={p_mw:.4f}")
    if (p_welch < 0.05) != (p_mw < 0.05):
        print("  ⚠ Welch and Mann-Whitney disagree — the metric distribution shifts")
        print("    differently in mean vs. in rank. Investigate which matters.")

ab_test_report(df, "variant", "revenue_per_user")
```

**Multiplicity warning:** if you run multiple A/B tests on the same data
(metric × segment × cohort), the per-test 5% alpha doesn't hold. Apply BH-FDR
across the full test family via `statsmodels.stats.multitest.multipletests`,
or pre-register the primary hypothesis and budget α-spending.

## Cohort Analysis

```python
# Assign cohort by first activity date
df["cohort"] = df.groupby("user_id")["date"].transform("min").dt.to_period("M")
df["period_number"] = (
    df["date"].dt.to_period("M") - df["cohort"]
).apply(lambda x: x.n)

# Retention table
cohort_pivot = df.groupby(["cohort", "period_number"])["user_id"].nunique().reset_index()
cohort_size = cohort_pivot[cohort_pivot["period_number"] == 0].set_index("cohort")["user_id"]
cohort_table = cohort_pivot.pivot(index="cohort", columns="period_number", values="user_id")
retention = cohort_table.divide(cohort_size, axis=0)
print(retention.round(3))
```

## Ranking and Percentiles

```python
# Rank (ties handled by method)
df["revenue_rank"]  = df["revenue"].rank(ascending=False, method="min")
df["revenue_dense"] = df["revenue"].rank(ascending=False, method="dense")
df["revenue_pct"]   = df["revenue"].rank(pct=True)  # 0–1 percentile

# Rank within groups
df["rank_in_segment"] = df.groupby("segment")["revenue"].rank(ascending=False, method="min")

# Percentile bins
df["decile"] = pd.qcut(df["revenue"], q=10, labels=False) + 1    # 1–10
df["quintile"] = pd.qcut(df["revenue"], q=5, labels=["Q1","Q2","Q3","Q4","Q5"])
```

## Summary Statistics Table (Report-Ready)

```python
def summary_table(df, group_col, metric_col):
    """Produce a publication-quality summary table."""
    return (
        df.groupby(group_col)[metric_col]
        .agg(
            n="count",
            mean="mean",
            median="median",
            std="std",
            p25=lambda x: x.quantile(0.25),
            p75=lambda x: x.quantile(0.75),
            min="min",
            max="max",
        )
        .round(2)
        .reset_index()
    )

print(summary_table(df, "region", "revenue").to_string(index=False))
```
