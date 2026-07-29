# Feature Importance — Computation Methods

The compute layer for driver analysis: fit an XGBoost baseline, then read
importance through three complementary lenses and reconcile them. The
*diagnostics* that must run first (VIF, null-handling, skew, leakage) and
the reporting discipline stay in `feature-importance.md`.

## Contents

- XGBoost baseline recipe (regression / binary / categorical encoding)
- Three importance lenses (Gain, permutation, SHAP)
- Cross-method agreement check (rank correlation across lenses)
- Interaction detection (SHAP dependence + interaction values)

Read this file when working on: fitting an XGBoost model, computing SHAP
/ permutation / gain importance, reconciling disagreeing rankings, or
detecting feature interactions. Prerequisite diagnostics and the
reporting template are in `feature-importance.md`.

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
