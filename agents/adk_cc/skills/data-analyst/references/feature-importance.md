# Feature Importance & SHAP — Expert Skill

You are an expert data analyst. Apply the following methodology when linear
analysis (Spearman ρ, standardized OLS β + ΔR²) is insufficient and you need
**non-linear importance**, **interaction detection**, or **per-row
explanation** of a target variable.

This file is a **complement** to `statistical-analysis.md`, not a
replacement. Always run linear analysis first — it is faster, more
interpretable, and often sufficient. Escalate to ML importance only when
the conditions below are met.

## When to Escalate

> **If the question is *what caused the shift?* (not *what predicts the
> level?*), see `root-cause-analysis.md` first.** Feature importance is
> associational; root-cause needs change-point detection, commonality, and
> causal-inference tools that this file does not cover.

Escalate from linear → tree-based importance when **any** of these hold:

| Trigger | Why linear is inadequate |
|---|---|
| Linear-model R² < 0.4 with all plausible factors included | Non-linear or interaction effects are dominant |
| Residual plot vs. a predictor shows curvature, U-shape, or discontinuity | Linear β collapses non-linear signal into a single slope |
| Domain reason to suspect interactions (e.g. "X matters more when Y is high") | Additive linear models cannot represent interactions |
| Stakeholder asks "why is *this individual* high/low?" | β is population-level; SHAP is per-row |
| Predictors are heavily skewed, zero-inflated, or clipped | OLS coefficient inference is biased; trees are robust |
| Spearman ρ ranking and standardized β ranking disagree | The two views need a third lens to arbitrate |

If none of these hold, **stop and report the linear result** — adding ML
adds dependency cost and interpretation overhead with no insight gain.

## Setup — Optional Dependencies

`xgboost`, `shap`, and `scikit-learn` are not part of the base
data-analyst environment. Always guard imports and provide a fallback
message. **Catch `Exception`, not just `ImportError`** — xgboost on macOS
raises `XGBoostError` at import time when `libomp` is missing, which is
not an `ImportError`:

```python
try:
    import xgboost as xgb
    import shap
    from sklearn.model_selection import train_test_split
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import r2_score, mean_absolute_error
    HAS_ML = True
except Exception as e:
    HAS_ML = False
    print(f"ML stack unavailable ({type(e).__name__}: {e}).")
    print("Install with: uv pip install xgboost shap scikit-learn")
    print("On macOS, xgboost also needs OpenMP: brew install libomp")
```

If `HAS_ML` is False, fall back to the linear analysis from
`statistical-analysis.md` and tell the user explicitly that the ML
cross-check was skipped.

## Pre-Modeling Diagnostics

Two checks before fitting any importance model. They take seconds and
prevent the most common silent-failure modes: ranking driven by collinear
predictors, and ranking driven by which rows survived null-dropping.

### 1. Multicollinearity audit (VIF)

VIF (Variance Inflation Factor) for column j is `1 / (1 − R²_j)` where
`R²_j` is from regressing column j on every other predictor. Interpretation:

| VIF | Meaning | Action |
|---:|---|---|
| 1 | independent | none |
| 1–5 | mild | none |
| 5–10 | moderate | OLS β unstable; rely on SHAP/permutation cross-check |
| > 10 | severe | drop one of the redundant pair, OR switch to Ridge/Lasso |

```python
import numpy as np
import pandas as pd

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
            vif = float("inf")
        else:
            r2 = 1 - ((y - yhat) ** 2).sum() / ss_tot
            vif = float("inf") if r2 >= 0.9999 else 1 / (1 - r2)
        rows.append({"feature": col, "R²_on_others": round(float(r2), 4),
                     "VIF": round(float(vif), 2)})
    return pd.DataFrame(rows).sort_values("VIF", ascending=False).reset_index(drop=True)

vif = vif_table(X)
print(vif.to_string(index=False))

severe   = vif.query("VIF > 10")["feature"].tolist()
moderate = vif.query("5 < VIF <= 10")["feature"].tolist()
if severe:
    print(f"⚠ SEVERE multicollinearity (VIF > 10): {severe}")
    print("  → drop one of each redundant pair OR switch to RidgeCV / LassoCV")
elif moderate:
    print(f"⚠ Moderate multicollinearity (VIF 5-10): {moderate}")
    print("  → linear β is unstable; trust SHAP & permutation rankings over β")
```

When VIF > 10, prefer regularized regression for the linear baseline:

```python
from sklearn.linear_model import RidgeCV
from sklearn.preprocessing import StandardScaler

scaler = StandardScaler()
Xz = scaler.fit_transform(X)
yz = (y - y.mean()) / y.std()
ridge = RidgeCV(alphas=[0.01, 0.1, 1.0, 10.0, 100.0]).fit(Xz, yz)
ridge_coefs = pd.Series(ridge.coef_, index=X.columns).sort_values(key=abs, ascending=False)
print(f"Best α: {ridge.alpha_}, R²: {ridge.score(Xz, yz):.4f}")
print(ridge_coefs)
```

Always **report VIF in the output** — it is the most concise way to tell a
reader whether the OLS β values are interpretable as independent effects.

### 1a. Picking the representative when you must drop

Use the framework in `data-exploration.md` § 6 (5-priority selector +
`select_cluster_representative()` helper).

For feature-importance work, **Priority 3 is the default `|ρ(target)|`
score**. Compute it on the **training fold only** to avoid soft target
leakage when the dropped/kept choice subsequently propagates into a
cross-validated R²:

```python
from sklearn.model_selection import train_test_split
X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42)
fold_mask = X.index.isin(X_tr.index)

for cluster in find_clusters(X[numeric_cols]):
    keep, drop, reason = select_cluster_representative(
        X, y, cluster, fold_mask=fold_mask
    )
    # apply drops on both folds, then refit
```

After dropping, refit and verify three things:
1. R² didn't drop noticeably (kept feature absorbed the dropped signal)
2. The kept feature's std-β / SHAP magnitude inflated to fill the gap
3. VIF for the kept feature returned to < 5 (cluster fully captured)

If R² dropped > 0.05 on the holdout, the cluster contained genuinely
distinct signal — don't drop, switch to RidgeCV instead.

### 1b. Target skew check (regression only)

Before fitting a regression, check the target's distribution. Heavy
right-skew (common for prices, counts, durations, revenues) inflates
the influence of high-tail observations on OLS β and produces
heteroscedastic residuals.

**Decision rule by target type:**
- Positive-only, right-skewed (`skew > 1`, `y > 0`) → `log1p` or **Box-Cox**
  (Box-Cox picks the best λ; `log` is Box-Cox at λ=0)
- Counts (Poisson-like) → Poisson regression / `XGBRegressor(objective="count:poisson")`
- Bounded proportions in [0, 1] → logit-transform
- Left-skewed → Yeo-Johnson (handles negative values too) or square transform
- Multimodal / fat-tailed → consider **quantile regression**
  (`XGBRegressor(objective="reg:quantileerror", quantile_alpha=0.5)`) for the median

```python
from scipy.stats import skew, boxcox
print(f"Target skew: {skew(y):+.2f}")

if skew(y) > 1 and (y > 0).all():
    # Default: log1p. For more flexibility, use Box-Cox:
    #   y_model, lam = boxcox(y + 1e-9); print(f"Box-Cox λ = {lam:.3f}")
    print("  → heavy right-skew; using log1p(y) for modeling")
    y_model = np.log1p(y)
elif skew(y) < -1:
    from sklearn.preprocessing import PowerTransformer
    pt = PowerTransformer(method="yeo-johnson").fit(y.values.reshape(-1, 1))
    y_model = pt.transform(y.values.reshape(-1, 1)).ravel()
    print(f"  → heavy left-skew; Yeo-Johnson applied, new skew {skew(y_model):+.2f}")
else:
    y_model = y

print(f"Skew after transform: {skew(y_model):+.2f}")
```

Always report **both** raw-target and transformed-target R² so the
reader can see the predictive gain. When you log-transform, translate
model errors **back per-row** for stakeholders — do not use a single
multiplicative-error approximation:

```python
# WRONG — only valid for small mae_log (Taylor expansion):
# mae_dollars = y_raw.median() * (np.exp(mae_log) - 1)

# RIGHT — back-transform predictions per row, then compute MAE in original units:
pred_orig = np.expm1(pred)              # if you used log1p
y_orig    = np.expm1(y_test)
mae_dollars = mean_absolute_error(y_orig, pred_orig)
print(f"MAE in $: ${mae_dollars:,.0f} (median-priced item: ${y_orig.median():,.0f})")
```

### 2. Null-handling policy for modeling

The data-cleaning skill (`data-cleaning.md`) covers fillna / dropna /
interpolation in general. For **modeling specifically**, three additional
rules apply that are easy to violate:

```python
def null_audit(X: pd.DataFrame, y: pd.Series) -> pd.DataFrame:
    """Per-feature null fraction + association of missingness with target.
    Non-zero |miss-target assoc| means missingness itself carries signal —
    informative missingness — and a missing-indicator column is required.
    Picks the right association measure based on target dtype."""
    from scipy import stats
    is_num   = pd.api.types.is_numeric_dtype(y) and not pd.api.types.is_bool_dtype(y)
    is_bin   = pd.api.types.is_bool_dtype(y) or (is_num and y.nunique() == 2)
    rows = []
    for col in X.columns:
        miss_pct = float(X[col].isna().mean())
        if miss_pct == 0:
            rows.append({"feature": col, "null_pct": 0.0, "miss_target_assoc": 0.0})
            continue
        is_miss = X[col].isna().astype(int)
        if is_bin:                                  # binary target → point-biserial r
            r, _ = stats.pointbiserialr(is_miss, pd.Series(y).astype(int))
            assoc = float(r)
        elif is_num:                                # continuous numeric target → Spearman ρ
            assoc = float(is_miss.corr(y, method="spearman"))
        else:                                       # multiclass categorical → Cramér's V
            ct = pd.crosstab(is_miss, y)
            chi2, *_ = stats.chi2_contingency(ct)
            n = ct.sum().sum()
            denom = n * max(min(ct.shape) - 1, 1)
            assoc = float(np.sqrt(chi2 / denom)) if denom > 0 else 0.0
        rows.append({"feature": col, "null_pct": round(miss_pct, 4),
                     "miss_target_assoc": round(assoc, 4)})
    return pd.DataFrame(rows).sort_values("null_pct", ascending=False)

audit = null_audit(X, y)
print(audit.to_string(index=False))
```

`miss_target_assoc` is Spearman ρ for numeric targets, point-biserial r
for binary, and Cramér's V for multiclass. All three live on a comparable
[-1, 1] / [0, 1] scale, so the same 0.05 threshold below applies.

**Threshold note:** 0.05 is a heuristic chosen for typical n ≈ 1k–100k.
At n = 1M, |assoc| of 0.05 is highly significant statistically but the
practical effect is small — consider raising the threshold to ~0.10 for
very large n. At n < 200, raise to ~0.20 (the estimate is noisy). The
threshold encodes **practical** significance, not statistical
significance.

**Decision rules per feature (apply per column, not whole DataFrame):**

| null_pct | \|miss_target_assoc\| | Recommended action |
|---:|---:|---|
| 0% | — | none |
| < 1% | any | drop those rows |
| 1–10% | < 0.05 | XGBoost: pass through (handles NaN natively); Linear: median-impute, **warn about R² attenuation** |
| 1–10% | ≥ 0.05 | **informative missingness** — add `{col}_is_missing` indicator column, then median-impute the original |
| 10–50% | < 0.05 | indicator + median-impute (same as above; the missingness is large enough that the indicator helps even if MAR) |
| 10–50% | ≥ 0.05 | indicator + median-impute, and call out the strong missingness signal in the report |
| > 50% | any | drop the column entirely; report it as "too sparse to model" |

**Imputation attenuation bias (linear regression only):** mean/median
imputation shrinks β toward zero by approximately `null_pct × σ_imputed / σ_observed`.
For a column with 10% nulls, expect ~5–10% attenuation in its β. Always
report R² before and after imputation:

```python
# Before imputation (drop NaN rows)
clean_mask = X.notna().all(axis=1) & y.notna()
r2_clean = fit_linear(X[clean_mask], y[clean_mask])

# After imputation (keep all rows)
X_imp = X.fillna(X.median(numeric_only=True))
r2_imp = fit_linear(X_imp, y)

print(f"R² before imputation: {r2_clean:.4f}  (rows: {clean_mask.sum():,})")
print(f"R² after  imputation: {r2_imp:.4f}    (rows: {len(X):,})")
print(f"Attenuation:          {(r2_clean - r2_imp):.4f}")
```

If attenuation > 0.05, **and** missingness is informative
(`miss_target_assoc` ≥ 0.05), use the indicator+impute pattern — the
indicator captures the missingness signal that imputation erases.

For purely random missingness (`miss_target_assoc` ≈ 0), naive median
imputation is usually fine; indicator+impute can slightly *worsen* the
original column's β because the indicator absorbs variance, although it
preserves R² better. Pick the pattern that matches what's actually
informative in the data — don't add indicators reflexively.

**XGBoost native NaN handling:** XGBoost (>= 1.6) learns a "default
direction" at each split for NaN values. Pass NaN through directly; do
**not** impute before fitting unless you also add a missing-indicator
column. This is one of the few places linear and tree paths legitimately
diverge — the linear path needs imputation, the tree path does not.

```python
# XGBoost path: pass NaN through
model = xgb.XGBRegressor(...).fit(X_train_with_nans, y_train)

# Linear path: indicator + impute
for col in cols_with_nulls:
    X[f"{col}_is_missing"] = X[col].isna().astype("int8")
X = X.fillna(X.median(numeric_only=True))
```

## XGBoost Baseline Recipe

Use sensible defaults — do not hyperparameter-tune unless explicitly
asked. The goal is feature importance, not a production model.

### Regression target

```python
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import r2_score, mean_absolute_error

# X: numeric features only (encode categoricals first — see below)
# y: numeric target
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42
)

model = xgb.XGBRegressor(
    n_estimators=400,
    max_depth=6,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    reg_lambda=1.0,
    random_state=42,
    tree_method="hist",      # fast on large data
    n_jobs=-1,
)
model.fit(
    X_train, y_train,
    eval_set=[(X_test, y_test)],
    verbose=False,
)

pred = model.predict(X_test)
print(f"R²  on holdout: {r2_score(y_test, pred):.4f}")
print(f"MAE on holdout: {mean_absolute_error(y_test, pred):.4f}")
```

### Binary classification target

```python
model = xgb.XGBClassifier(
    n_estimators=400,
    max_depth=6,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    eval_metric="logloss",
    random_state=42,
    tree_method="hist",
    n_jobs=-1,
)
model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

from sklearn.metrics import roc_auc_score, average_precision_score
proba = model.predict_proba(X_test)[:, 1]
print(f"ROC-AUC: {roc_auc_score(y_test, proba):.4f}")
print(f"PR-AUC : {average_precision_score(y_test, proba):.4f}")
```

### Categorical encoding before XGBoost

XGBoost (>= 1.6) handles categoricals natively with `enable_categorical=True`,
but for portability use explicit encoding:

```python
# Low-cardinality (< 20 unique) → one-hot
X = pd.get_dummies(df[features], columns=cat_cols, drop_first=False, dtype="int8")

# High-cardinality → ordinal (target encoding only inside CV folds)
from sklearn.preprocessing import OrdinalEncoder
oe = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
X[high_card_cols] = oe.fit_transform(X[high_card_cols])
```

Never label-encode an *unordered* categorical — XGBoost will treat the
codes as numeric and infer false orderings.

## Three Importance Lenses

Always compute **at least two**. Reporting only one is malpractice.

### 1. Gain (XGBoost split-importance)

Fast, free, biased. Reports total loss reduction attributed to each
feature across all splits in the ensemble.

```python
gain = pd.Series(model.feature_importances_, index=X.columns) \
         .sort_values(ascending=False)
print(gain.head(15))
```

**Bias to know:** Gain inflates importance for high-cardinality and
continuous features (more split candidates = more chances to win a split).
A 10-level categorical can dominate a binary feature even if the binary
feature is the true driver. **Never report gain in isolation.**

### 2. Permutation importance (sklearn — model-agnostic)

A model-agnostic baseline. Measures the drop in holdout score when a
single feature's values are randomly shuffled.

```python
from sklearn.inspection import permutation_importance

perm = permutation_importance(
    model, X_test, y_test,
    n_repeats=10,                            # 10–30 recommended; 5 is too noisy
    random_state=42,
    n_jobs=-1,
    scoring="r2",                            # see below — pick by target shape
)
perm_df = (pd.DataFrame({
        "feature": X_test.columns,
        "importance_mean": perm.importances_mean,
        "importance_std":  perm.importances_std,
    })
    .sort_values("importance_mean", ascending=False)
    .reset_index(drop=True))
print(perm_df.head(15).round(4))
```

**`scoring=` by target shape:**
- Symmetric numeric target → `"r2"` (default)
- Heavy-tailed / log-transformed target → `"neg_mean_absolute_error"`
  (R² is dominated by the long tail; MAE represents the typical row)
- Binary classification, balanced → `"roc_auc"`
- Binary classification, imbalanced (positive < 10%) → `"average_precision"` (PR-AUC)

Compute on the **holdout set**, not training data. On training data,
permutation importance is meaningless (the model has memorized).

**Known bias with correlated features (Strobl 2007, Hooker–Mentch 2021):**
when X₁ and X₂ are correlated, permuting X₁ leaves the model able to recover
the signal from X₂, so *both* look weak — importance gets diluted across
the cluster. Always pair permutation with the VIF / cluster-collapse step
from § 1; otherwise correlated features systematically rank below their
independent peers regardless of true effect.

For very large holdouts (> 100k rows), subsample to 50k for speed —
permutation runs the model `n_repeats × n_features` times.

### 3. SHAP (TreeExplainer)

Gold standard for tree models. Each feature's contribution to each
individual prediction, with theoretical guarantees (Shapley values from
cooperative game theory). Use for both global (mean |SHAP|) and per-row
explanations.

**`feature_perturbation=` choice matters for correlated features:**
- `"tree_path_dependent"` (default) — observational SHAP, fast, no
  background needed. With correlated features, attributes some of the
  partner's effect to each feature in a cluster.
- `"interventional"` — causal/interventional SHAP, requires a background
  dataset, slower. Cleaner for causal interpretation. Required when
  using SHAP to argue about feature *effects* rather than predictions.

```python
import shap

# CRITICAL: sample before SHAP on large data — TreeSHAP is O(n × trees × leaves²)
X_shap = X_test.sample(min(50_000, len(X_test)), random_state=42)

# Observational SHAP (default — fast, fine for prediction explanation)
explainer = shap.TreeExplainer(model)

# OR interventional SHAP (slower, cleaner for effect interpretation):
# X_bg = X_train.sample(min(1000, len(X_train)), random_state=42)
# explainer = shap.TreeExplainer(model, X_bg,
#                                feature_perturbation="interventional")

shap_values = explainer.shap_values(X_shap)
# shap_values shape: (n_rows, n_features) for regression / binary

# Global importance: mean |SHAP|
shap_global = (pd.Series(np.abs(shap_values).mean(axis=0), index=X_shap.columns)
                 .sort_values(ascending=False))
print(shap_global.head(15).round(4))

# Visual summary (do this in notebooks; for scripts, save to file)
import matplotlib.pyplot as plt
shap.summary_plot(shap_values, X_shap, show=False)
plt.tight_layout()
plt.savefig("shap_summary.png", dpi=120, bbox_inches="tight")
plt.close()
```

**Sampling rules of thumb:**

| Dataset rows | SHAP sample size | Expected runtime |
|---:|---:|---:|
| ≤ 10,000 | full | < 30 s |
| 10k–100k | 20,000 | 1–3 min |
| 100k–1M | 50,000 | 3–10 min |
| > 1M | 50,000 (≤ 5%) | 5–15 min |

Larger samples rarely change the ranking; they only tighten the visual
density of `summary_plot`.

## Cross-Method Agreement Check

The signal that matters is **all three methods agreeing**. Disagreement
is itself a finding — report it, do not silently pick the winner.

```python
def rank_compare(*, gain, perm_mean, shap_mean, spearman_rho):
    """Build a side-by-side rank table. Inputs are pd.Series indexed by feature."""
    df = pd.DataFrame({
        "spearman_|rho|": spearman_rho.abs(),
        "xgb_gain":       gain,
        "perm_importance": perm_mean,
        "shap_|mean|":     shap_mean,
    })
    ranks = df.rank(ascending=False, method="min").astype(int)
    ranks.columns = [f"rank_{c}" for c in df.columns]
    out = pd.concat([df.round(4), ranks], axis=1).sort_values("rank_shap_|mean|")
    return out

table = rank_compare(
    gain=gain,
    perm_mean=perm_df.set_index("feature")["importance_mean"],
    shap_mean=shap_global,
    spearman_rho=df[features].corrwith(df[target], method="spearman"),
)
print(table.head(15).to_string())

# Quantify agreement: Spearman correlation across the four ranking columns
agreement = (table.filter(like="rank_").corr(method="spearman")
                .round(3))
print("\nRank-method agreement (Spearman):")
print(agreement)
```

Interpretation:
- **Off-diagonal > 0.8:** methods agree → ranking is robust to method choice.
- **0.5–0.8:** mostly agree → report the consensus top-K, flag disagreements.
- **< 0.5:** methods disagree → suspect non-linearity, feature interaction,
  or one of the methods is misled. Investigate before publishing a ranking.

**What consensus does and doesn't tell you:** strong agreement across the
four lenses means the **ranking** is robust to method choice — not that
the underlying relationship is causal. All four methods are different
views of the same fitted model on the same data; they are correlated
witnesses, not independent. To claim causation, see
`root-cause-analysis.md` § 4 (DAG, DiD, refutation).

## Interaction Detection

When SHAP and linear β disagree on a feature's importance, the most common
cause is an interaction effect. Check with SHAP dependence plots:

```python
# Top feature dependence — does its SHAP value depend on another feature?
top_feature = shap_global.index[0]
shap.dependence_plot(top_feature, shap_values, X_shap, show=False)
plt.tight_layout(); plt.savefig(f"shap_dep_{top_feature}.png", dpi=120); plt.close()
```

For an explicit interaction-strength matrix (only on small data):

```python
# WARNING: O(n × features²) — use only when features ≤ 30 and rows ≤ 10,000
if X_shap.shape[1] <= 30 and len(X_shap) <= 10_000:
    shap_inter = explainer.shap_interaction_values(X_shap)
    # off-diagonal magnitude = pairwise interaction strength
    inter_mat = np.abs(shap_inter).mean(axis=0)
    np.fill_diagonal(inter_mat, 0)
    pairs = (pd.DataFrame(inter_mat, index=X_shap.columns, columns=X_shap.columns)
             .stack().reset_index()
             .rename(columns={"level_0":"a","level_1":"b",0:"interaction"})
             .query("a < b").sort_values("interaction", ascending=False))
    print(pairs.head(10).round(4))
```

For larger data, infer interactions qualitatively from `dependence_plot`
color spread, then verify on a 10k subsample.

## Anti-Patterns / Leakage

These are the failures that make tree-based "importance" results
worthless. Check for each one before reporting.

| Anti-pattern | Symptom | Fix |
|---|---|---|
| Using post-outcome variables as predictors | Importance dominated by one variable that's a derived/composite of the target (e.g. predicting `stress_level` with `mental_health_index` in it) | Drop variables that are computed from or after the target |
| No train/test split | "R² = 0.99" but new data scores 0.30 | Always hold out 20% before fitting |
| SHAP fit on training data | SHAP plot looks "too clean" — everything matters perfectly | SHAP must be computed on the holdout / held-out sample |
| Label-encoding nominal categorical | Tree splits invent ordinal relationships | Use `get_dummies` for nominal; only ordinal-encode true ordinals |
| Reporting gain alone | Continuous features dominate; binary features look unimportant | Always include permutation or SHAP cross-check |
| No `random_state` | Re-running gives a different ranking | Set `random_state=` on split, model, and SHAP sample |
| Importance on a model that hasn't converged | Wild swings between runs | Confirm training error has stabilized; raise `n_estimators` or check for class imbalance |
| Mean/median imputation in linear regression without warning | β attenuated toward zero (~5–15% per imputed column) | Add `{col}_is_missing` indicator before imputing, OR report R² before vs. after imputation |
| Dropping rows with nulls when missingness correlates with target | Sample becomes biased; β reflects only respondents who answered | Run `null_audit()`; if `miss_target_rho >= 0.05`, use indicator+impute, never drop |
| Reporting OLS β when VIF > 10 | β values flip sign or change magnitude across re-runs | Drop one of the redundant pair, OR switch to RidgeCV |
| "Most important feature" is itself a rating-summary | Top driver is `OverallQual`/`Score`/`Rating` while per-component ratings (`KitchenQual`, `ExterQual`, …) sit just behind | Surface the redundancy: report the rating cluster as one composite, or run an importance pass with the summary feature removed to see what fills its place |
| Reporting numeric-only VIF when categoricals exist | A numeric column with VIF ≈ 1 is silently bound to a categorical (e.g. Ames `Garage Yr Blt` ↔ `Garage Finish` at η² = 0.998) — its β looks meaningful but is uninterpretable | Run `mixed_type_vif()` and `cross_type_binding()` from `data-exploration.md` § 6; treat any source with max design-matrix VIF > 10 OR pairwise η² > 0.7 as collinear |

The **first** anti-pattern is the most common in EDA: when the target is
a survey scale or composite (stress, satisfaction, NPS, mental health
index), other survey scales in the same instrument are often
mathematically related. Drop them before fitting.

## Reporting Template

When the cross-check is done, deliver a single rank-comparison table plus
a one-paragraph interpretation. The table is the artifact:

```
| Feature             | Spearman ρ | std β | Perm. Imp. | SHAP \|mean\| | Rank consensus |
|---------------------|-----------:|------:|-----------:|--------------:|---------------:|
| financial_stress    | +0.453     | +0.47 | 0.214      | 0.79          | 1 (all 4)      |
| exam_pressure       | +0.444     | +0.46 | 0.087      | 0.71          | 2 (all 4)      |
| family_expectation  | +0.336     | +0.35 | 0.118      | 0.52          | 3 (all 4)      |
| sleep_hours         | -0.254     | -0.26 | 0.066      | 0.31          | 4 (all 4)      |
| physical_activity   | -0.167     | -0.17 | 0.029      | 0.18          | 5 (all 4)      |
| study_hours_per_day | +0.340     |  0.00 | 0.001      | 0.04          | 9 → ρ misled by collinearity with exam_pressure |
```

Required prose elements:
1. **Headline finding:** the consensus #1 driver, with std β + SHAP magnitudes.
2. **Method-agreement summary:** rank-correlation across the four lenses.
3. **Disagreements called out by name:** any feature whose linear-rank and
   SHAP-rank differ by ≥ 3 — explain why (collinearity, non-linearity, or
   interaction).
4. **Interactions found:** top SHAP dependence pairs, if any.
5. **Holdout R² (or ROC-AUC):** the model's predictive power on unseen data.
6. **Caveats:** sample size for SHAP, dropped-leakage variables, optional
   library versions.

## Performance Cheatsheet

| Issue | Fix |
|---|---|
| XGBoost training slow on millions of rows | `tree_method="hist"`, `subsample=0.5`, fewer `n_estimators` |
| OOM during SHAP | Sample to 20k–50k rows; use `TreeExplainer` (not `KernelExplainer`) |
| SHAP plot crowded | Set `max_display=20` in `shap.summary_plot` |
| `permutation_importance` slow | Drop `n_repeats` to 3; subsample test set to 20k |
| Different importance ranks across runs | Set `random_state=` everywhere; check that the model converged |

## Decision Cheatsheet

```
PRE-FIT — always run these first:
   1. vif_table(X)        — any VIF > 10? drop redundant feature OR use RidgeCV.
   2. null_audit(X, y)    — apply per-column rules (drop / impute / indicator+impute / remove).
   3. Drop post-outcome leakage variables (composites of the target).

Linear analysis done (Spearman ρ + std β + ΔR²)?
   ↓ no  → run that first (statistical-analysis.md)
   ↓ yes
R² ≥ 0.4 and rankings stable?
   ↓ yes → STOP. Report linear result. ML adds no value here.
   ↓ no
Run XGBoost + permutation + SHAP cross-check.
   ↓
Methods agree (Spearman rank-corr ≥ 0.8)?
   ↓ yes → Report consensus ranking, note ML confirmed linear analysis.
   ↓ no
Investigate disagreement:
   - Collinearity?  → drop redundant feature, rerun.
   - Non-linearity? → SHAP dependence plot, report shape.
   - Interaction?   → SHAP interaction values on subsample, report pair.
Report all three rankings + the interpretation that resolves them.
```
