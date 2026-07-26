# Root-Cause Analysis — Expert Skill

You are an expert data analyst supporting **product-defect investigation,
yield excursions, process drift, and incident reviews**. The methodology
here is causal and time-localized — distinct from the associational
ranking in `feature-importance.md`.

The headline rule: **SHAP-importance is not causal evidence.** A SHAP
value of 0.5 for `chamber_pressure` says "this feature reduces prediction
error" — *not* "fixing chamber pressure will fix yield." Root-cause work
needs to identify the *change* that preceded the outcome shift, then rule
out alternative explanations.

## When to Use This File vs. `feature-importance.md`

| Question | File |
|---|---|
| *"Which X predicts Y best?"* | `feature-importance.md` |
| *"What caused Y to shift at time T?"* | here |
| *"Which tool / lot / operator is associated with bad parts?"* | both — start here for time-localized excursions, use both together |
| *"Did this process change improve yield?"* | here (DiD, § 6) |
| *"We ran a DOE — what mattered?"* | here (ANOVA, § 7) |

If the question is time-localized ("since April 2"), or causal ("did
*changing* X produce the shift in Y?"), start here. If it's purely
associational ("rank features by importance"), `feature-importance.md`
is sufficient.

## Setup — Optional Dependencies

```python
try:
    import ruptures as rpt           # change-point detection
    import statsmodels.api as sm
    import statsmodels.formula.api as smf
    from statsmodels.stats.multicomp import pairwise_tukeyhsd
    HAS_RCA = True
except Exception as e:
    HAS_RCA = False
    print(f"RCA stack unavailable ({type(e).__name__}: {e}).")
    print("Install: uv pip install ruptures statsmodels mlxtend")
    print("Optional (causal): uv pip install dowhy econml")

try:
    import dowhy                      # causal inference (heavyweight, optional)
    HAS_CAUSAL = True
except Exception:
    HAS_CAUSAL = False
```

`scipy` and `sklearn` (already required by `feature-importance.md`) cover
Fisher's exact, Mann-Whitney, propensity matching, and Tukey HSD.

### Time-indexed data — do not use random `train_test_split`

Most RCA data is time-indexed (per-lot defect rate, per-sensor reading
sequence). A random split causes **temporal leakage**: training rows
include moments after test rows, so the model implicitly "sees the
future." This silently inflates holdout R² / AUC.

```python
# WRONG for time-indexed data:
# X_train, X_test = train_test_split(X, test_size=0.2, random_state=42)

# RIGHT — explicit time cutoff:
cutoff = df["timestamp"].quantile(0.8)
train = df[df["timestamp"] < cutoff]
test  = df[df["timestamp"] >= cutoff]

# OR sklearn's TimeSeriesSplit for CV:
from sklearn.model_selection import TimeSeriesSplit
tscv = TimeSeriesSplit(n_splits=5)
for train_idx, test_idx in tscv.split(X.sort_index()):
    ...
```

Random split is acceptable only when the rows are exchangeable — typically
when the analysis question is cross-sectional (compare populations at one
point in time), not temporal.

## 1. Change-Point Detection (CPD)

The signature question of an excursion is **when did it start?** Rolling
means smear the answer; you need a formal break detector.

### Default: PELT (offline, fast)

```python
import numpy as np
import pandas as pd
import ruptures as rpt

def detect_changepoints(series, model="rbf", min_size=30, penalty=None):
    """Return indices where the metric distribution changes.

    series   : 1-D numpy array or pd.Series (chronological order required)
    model    : "rbf" (default, robust), "l2" (gaussian mean shift),
               "l1" (median shift, robust to outliers)
    min_size : minimum samples between change points
    penalty  : if None, uses log(n) * variance (BIC-style); raise to detect
               fewer (only large) change points; lower to detect more
    """
    s = np.asarray(series, dtype=np.float64)
    n = len(s)
    if penalty is None:
        penalty = np.log(n) * float(np.var(s))
    algo = rpt.Pelt(model=model, min_size=min_size).fit(s)
    bkps = algo.predict(pen=penalty)
    return bkps[:-1]                  # drop the trailing endpoint

# Visual sanity check is mandatory:
import matplotlib.pyplot as plt
def plot_cps(series, bkps, ax=None):
    ax = ax or plt.gca()
    ax.plot(series, lw=1)
    for b in bkps:
        ax.axvline(b, color="red", ls="--", lw=1)
    ax.set_title(f"{len(bkps)} change points detected")
```

### CUSUM for small persistent drifts

PELT often misses subtle drifts (a 0.5σ shift sustained for 200 samples).
CUSUM accumulates deviation and raises an alarm when accumulation crosses
a threshold:

```python
def cusum(series, target=None, sigma=None, k=0.5, h=5, ref_window=50):
    """Two-sided CUSUM. Returns indices where +/- CUSUM exceeds h*sigma.

    target : reference mean. If None, estimated from the first `ref_window` samples
             (NOT the full series — that would include any drift you're trying
             to detect).
    sigma  : reference standard deviation. If None, estimated from the first
             `ref_window` samples for the same reason.
    k      : reference value in standard deviations (0.5 = sensitive to 1σ shifts)
    h      : decision threshold in standard deviations (5 is canonical)

    Estimating target/sigma from the full series is a common bug: a drifted
    series has inflated σ, the slack k·σ becomes too generous, and the test
    under-detects the drift it was supposed to find.
    """
    s = np.asarray(series, dtype=np.float64)
    ref = s[:min(ref_window, len(s) // 5)]               # clean-period window
    target = target if target is not None else ref.mean()
    sigma  = sigma  if sigma  is not None else ref.std()
    pos = np.zeros(len(s)); neg = np.zeros(len(s))
    for i in range(1, len(s)):
        pos[i] = max(0, pos[i-1] + (s[i] - target - k * sigma))
        neg[i] = min(0, neg[i-1] + (s[i] - target + k * sigma))
    alarms = np.where((pos > h * sigma) | (neg < -h * sigma))[0]
    return alarms
```

### Decision table

| Data shape | Method |
|---|---|
| One known break, abrupt | PELT, model="l2" |
| Multiple unknown breaks | PELT, model="rbf", BIC penalty |
| Slow drift, no clear step | CUSUM (k=0.5) or EWMA chart (§ 2) |
| Heavy outliers | PELT, model="l1" (median-based) |
| Streaming / online detection | Bayesian Online CPD (`bayesian_changepoint_detection` lib) |

### Sanity check before believing a change point

A detected break is **noise** unless:
1. It has ≥30 samples on either side (else statistical power is too low)
2. The pre/post means differ by ≥1σ of the local noise
3. A Mann-Whitney pre vs. post returns p < 0.01
4. It is visually obvious in the plot

Always print all four checks. Reporting a "change point" without them is
the most common over-detection failure.

## 2. Statistical Process Control (SPC)

For ongoing monitoring (vs. forensic CPD on historical data). SPC is the
manufacturing-engineer vocabulary — speak it natively when reporting.

### Shewhart X̄/R Chart with Western Electric Runs Rules

```python
def shewhart_chart(values, subgroup_size=5):
    """Compute X̄ and R chart limits + Western Electric rule violations.
    Returns dict with center/UCL/LCL and per-subgroup violation flags.
    """
    s = np.asarray(values, dtype=np.float64)
    n_sub = len(s) // subgroup_size
    s = s[:n_sub * subgroup_size].reshape(n_sub, subgroup_size)
    xbar = s.mean(axis=1)
    rng  = s.max(axis=1) - s.min(axis=1)
    # Constants for n=5 subgroups (look up A2, D3, D4 for other sizes)
    A2, D3, D4 = 0.577, 0.0, 2.114
    cl_x  = xbar.mean()
    rbar  = rng.mean()
    ucl_x = cl_x + A2 * rbar
    lcl_x = cl_x - A2 * rbar
    ucl_r = D4 * rbar
    lcl_r = D3 * rbar
    sigma = (ucl_x - cl_x) / 3                  # implied σ from chart limits

    # Western Electric runs rules on X̄
    flags = pd.DataFrame({"xbar": xbar})
    flags["rule_1"] = np.abs(xbar - cl_x) > 3 * sigma                       # 1 point > 3σ
    flags["rule_2"] = (np.abs(xbar - cl_x) > 2 * sigma).rolling(3).sum() >= 2  # 2 of 3 > 2σ
    flags["rule_3"] = (np.abs(xbar - cl_x) > sigma).rolling(5).sum() >= 4      # 4 of 5 > 1σ
    flags["rule_4"] = pd.Series((xbar > cl_x).astype(int)).rolling(8).sum() \
                        .isin([0, 8])                                          # 8 in a row same side
    flags["any_violation"] = flags[["rule_1","rule_2","rule_3","rule_4"]].any(axis=1)
    return {"cl_x": cl_x, "ucl_x": ucl_x, "lcl_x": lcl_x,
            "cl_r": rbar, "ucl_r": ucl_r, "lcl_r": lcl_r,
            "sigma": sigma, "flags": flags}
```

### EWMA chart for slow drifts

```python
def ewma_chart(values, lambda_=0.2, L=3):
    """EWMA control chart — sensitive to small persistent drifts.
    lambda_ : weighting factor (0.05–0.3); smaller = smoother, slower
    L       : control limit width in sigma (3 default)
    """
    s = np.asarray(values, dtype=np.float64)
    target, sigma = s.mean(), s.std()
    z = np.zeros(len(s)); z[0] = target
    for i in range(1, len(s)):
        z[i] = lambda_ * s[i] + (1 - lambda_) * z[i-1]
    ucl = target + L * sigma * np.sqrt(lambda_ / (2 - lambda_))
    lcl = target - L * sigma * np.sqrt(lambda_ / (2 - lambda_))
    violations = np.where((z > ucl) | (z < lcl))[0]
    return {"ewma": z, "ucl": ucl, "lcl": lcl, "violations": violations}
```

### Process Capability Indices

```python
# Hartley's d2 constants for the R-bar method (subgroup sizes 2..10)
_D2 = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534,
       7: 2.704, 8: 2.847, 9: 2.970, 10: 3.078}

def capability(values, lsl, usl, subgroup_size=None, k=6):
    """Cp, Cpk (within-subgroup σ) and Pp, Ppk (overall σ) vs spec limits.

    subgroup_size : if given, estimate σ_within from subgroup ranges via R-bar/d2
                    (the rational-subgroup method). Otherwise σ_within = σ_overall,
                    in which case Cp == Pp and the within/long-term comparison
                    is uninformative.
    """
    s = np.asarray(values, dtype=np.float64)
    sigma_overall = s.std(ddof=1)
    mu = s.mean()

    if subgroup_size is not None and subgroup_size in _D2:
        n_sub = len(s) // subgroup_size
        sg = s[:n_sub * subgroup_size].reshape(n_sub, subgroup_size)
        rbar = (sg.max(axis=1) - sg.min(axis=1)).mean()
        sigma_within = rbar / _D2[subgroup_size]
    else:
        sigma_within = sigma_overall                  # falls back; Cp == Pp

    cp  = (usl - lsl) / (k * sigma_within)
    cpk = min((usl - mu) / (3 * sigma_within), (mu - lsl) / (3 * sigma_within))
    pp  = (usl - lsl) / (k * sigma_overall)
    ppk = min((usl - mu) / (3 * sigma_overall), (mu - lsl) / (3 * sigma_overall))

    def interpret(v):
        if v < 1.0:  return "incapable (defects expected)"
        if v < 1.33: return "marginal"
        if v < 1.67: return "capable"
        return "highly capable"
    return {"Cp": cp, "Cpk": cpk, "Pp": pp, "Ppk": ppk,
            "sigma_within": sigma_within, "sigma_overall": sigma_overall,
            "Cpk_interp": interpret(cpk), "Ppk_interp": interpret(ppk)}
```

**Cp vs. Cpk:** Cp ignores centering; Cpk penalizes off-center processes.
**Cp vs. Pp:** Cp uses within-subgroup variation (short-term); Pp uses
overall variation (long-term, includes drift). When Pp << Cp, the process
is drifting between subgroups — investigate.

### When SPC vs. CPD?

| Use case | Method |
|---|---|
| Online monitoring of a stable process | SPC (Shewhart or EWMA) |
| Retrospective forensic on a known excursion | CPD (PELT) |
| Estimating defect-rate at current state | Cp/Cpk |
| Confirming a fix took effect | Both: CPD on the fix timestamp + post-fix SPC |

**Anti-pattern:** SPC on autocorrelated data raises false alarms. If
`series.autocorr() > 0.5`, fit AR(1) residuals first and chart those.

## 3. Commonality Analysis

### Collapse multicollinear sensor clusters first

In manufacturing data, adjacent sensors measuring the same physical state
(chamber temp ↔ wall temp ↔ chuck temp; recipe pressure ↔ measured
pressure) are nearly perfectly correlated. **Without collapsing first,
your commonality table reports 8 members of one physical cluster as 8
separate findings** — useless for an engineer reading the report.

Use the framework in `data-exploration.md` § 6 (the 5-priority
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

### Per-categorical-factor commonality

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

For multi-factor commonality (which *combination* of tool + recipe + lot
appears in defects), use frequent-itemset mining:

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

## 4. Causal Inference

The headline rule: **prediction is not causation.** Before claiming X
caused Y, rule out:

1. **Reverse causation** — did Y change first and X follow?
2. **Common cause** — is there an upstream Z that drives both X and Y?
3. **Selection bias** — is X over-represented in the population at risk?
4. **Temporal mismatch** — did X's change actually precede Y's shift?

### Build a minimal DAG (Directed Acyclic Graph)

For every candidate cause X surfaced by SHAP/commonality, draw the
minimal DAG by hand:
```
   recipe_change ──→ chamber_pressure ──→ defect_rate
        │                  │
        ▼                  ▼
   tool_uptime ←──────→ wafer_temp
```
Then identify backdoor paths: any path from X to Y through an unblocked
common ancestor is a confounder. The fix is to either (a) condition on
the confounder, (b) find an instrument, or (c) acknowledge the limitation
in the report.

### Diff-in-Differences (DiD) — the workhorse for pre/post-change excursions

When a known process change happened at time T and you want to estimate
its causal effect on Y, compare:
- Δ(post − pre) for the affected group (treated)
- Δ(post − pre) for an unaffected group (control)

```python
def diff_in_diff(df, time_col, group_col, outcome, change_time,
                 treated_value, control_value):
    """Closed-form DiD via OLS interaction."""
    df = df.copy()
    df["post"]    = (df[time_col] >= change_time).astype(int)
    df["treated"] = (df[group_col] == treated_value).astype(int)
    model = smf.ols(f"{outcome} ~ post + treated + post:treated", data=df).fit()
    did_estimate = model.params["post:treated"]
    print(model.summary().tables[1])
    print(f"\nDiD estimate: {did_estimate:+.4f}  (95% CI: "
          f"[{model.conf_int().loc['post:treated',0]:.4f}, "
          f"{model.conf_int().loc['post:treated',1]:.4f}])")
    # Sanity: parallel-trends check (visualize pre-period for both groups)
    return model
```

DiD assumes **parallel trends** in the pre-period — the treated and
control groups would have moved together absent the change. Always plot
the pre-period for both groups; if their trends diverge before T, DiD
is invalid.

### Propensity Score Matching — for non-randomized observational comparisons

When you can't use DiD (no clean pre/post), match treated units to
similar control units on observed covariates:

```python
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import NearestNeighbors

def propensity_match(df, treatment_col, outcome_col, covariates, caliper=0.1):
    X = df[covariates].fillna(df[covariates].median())
    t = df[treatment_col].astype(int)
    # Estimate propensity score
    ps_model = LogisticRegression(max_iter=1000).fit(X, t)
    ps = ps_model.predict_proba(X)[:, 1]
    df = df.assign(ps=ps)
    treated = df[df[treatment_col] == 1].copy()
    control = df[df[treatment_col] == 0].copy()
    # 1:1 nearest neighbor on PS (with caliper to drop bad matches)
    nn = NearestNeighbors(n_neighbors=1).fit(control[["ps"]].values)
    dist, idx = nn.kneighbors(treated[["ps"]].values)
    keep = dist.ravel() < caliper
    matched = pd.concat([
        treated.iloc[keep],
        control.iloc[idx.ravel()[keep]],
    ])
    ate = (matched.loc[matched[treatment_col]==1, outcome_col].mean()
         - matched.loc[matched[treatment_col]==0, outcome_col].mean())
    print(f"Matched n: {keep.sum()} pairs (of {len(treated)} treated units)")
    print(f"ATE estimate: {ate:+.4f}")
    return matched, ate
```

### `dowhy` — for principled causal inference with explicit assumptions

When the DAG is non-trivial (multiple confounders, instruments,
mediators), use `dowhy` — it forces you to write the assumptions down,
estimates the effect under each, and runs refutation tests:

```python
from dowhy import CausalModel

model = CausalModel(
    data=df,
    treatment="recipe_change",                  # the suspected cause
    outcome="defect_rate",                      # the metric
    common_causes=["tool_uptime", "wafer_lot_age", "operator_shift"],
    # Optional: instruments, effect_modifiers, ...
)
model.view_model()                              # renders the DAG

# Identify the causal estimand (closed-form expression)
identified = model.identify_effect(proceed_when_unidentifiable=False)
print(identified)

# Estimate via backdoor adjustment + propensity score
estimate = model.estimate_effect(
    identified,
    method_name="backdoor.propensity_score_matching",
    target_units="ate",
)
print(f"Causal effect estimate: {estimate.value:+.4f}")

# Refutation: would the estimate survive these challenges?
refute_random  = model.refute_estimate(identified, estimate, "random_common_cause")
refute_placebo = model.refute_estimate(identified, estimate, "placebo_treatment_refuter")
refute_subset  = model.refute_estimate(identified, estimate, "data_subset_refuter")
print("Refutation summary:")
print(f"  Random common cause:  Δ = {refute_random.new_effect - estimate.value:+.4f}")
print(f"  Placebo treatment:    Δ = {refute_placebo.new_effect - estimate.value:+.4f}")
print(f"  Data subset:          Δ = {refute_subset.new_effect - estimate.value:+.4f}")
```

A robust causal estimate survives all three refutations with little
change. If the placebo refuter shows a large effect (it shouldn't, by
construction), the original estimate is suspect.

## 5. DOE / ANOVA — For Designed Experiments

When the team ran a real experiment (2k full-factorial, fractional
factorial, Plackett-Burman), use ANOVA with `statsmodels`:

```python
import statsmodels.formula.api as smf
import statsmodels.api as sm
from statsmodels.stats.multicomp import pairwise_tukeyhsd

# Two-way ANOVA with interaction
model = smf.ols("yield ~ C(tool) * C(recipe)", data=df).fit()
print(sm.stats.anova_lm(model, typ=2))      # Type-II SS

# Effect size: η² (eta-squared) and ω² (omega-squared, less biased)
ss = sm.stats.anova_lm(model, typ=2)
ss["eta_sq"]   = ss["sum_sq"] / ss["sum_sq"].sum()
ss["omega_sq"] = ((ss["sum_sq"] - ss["df"] * model.mse_resid) /
                  (ss["sum_sq"].sum() + model.mse_resid))
print(ss.round(4))

# Tukey HSD post-hoc: which level pairs differ?
tukey = pairwise_tukeyhsd(df["yield"], df["tool"], alpha=0.05)
print(tukey.summary())
```

**Statistical significance ≠ practical significance.** η² < 0.01 means
"this factor explains < 1% of the variance" — even if p < 0.0001, the
effect may be irrelevant. Always report effect size alongside p-values.

## 6. RCA Decision Cheatsheet

```
START: What kind of question?
   │
   ├── "Y has been bad for a while" (steady state)
   │       └→ SPC + Cp/Cpk + commonality (§§ 2, 3)
   │
   ├── "Y shifted at time T" (excursion)
   │       └→ CPD to confirm timing (§ 1)
   │          └→ Commonality on pre-vs-post (§ 3)
   │             └→ DiD if a known change at T (§ 4)
   │                └→ Causal refutation (§ 4: dowhy)
   │
   ├── "We ran a DOE, what mattered?"
   │       └→ ANOVA + Tukey HSD + effect size (§ 5)
   │
   └── "We made a fix — did it work?"
           └→ DiD pre/post + post-change SPC monitoring (§§ 2, 4)

At every step, ask: "Could this be confounded by Z?" If yes, condition on
Z (regression / matching) or acknowledge the limitation.
```

## 7. RCA Reporting Template

A SHAP table is not an RCA report. The deliverable is a **causal
narrative** with evidence:

```
## Excursion: <metric> shifted from <old> to <new> on <date>

### 1. Timeline
- Detection: <when first noticed>
- Estimated change point: <date> (PELT, BIC penalty, MW p < 0.01,
  pre n=200 / post n=180)
- Plot of metric over time with change point marked.

### 2. Candidate causes (ranked by evidence strength)
| Candidate | Evidence (commonality / DiD / SHAP) | Confounders to rule out |
|---|---|---|
| Recipe v3.2 deploy on T-2 | Lift=4.1, Fisher p=0.0003 | Tool maintenance same day |
| Chamber 5 (Tool A) | Defect rate 12% vs. baseline 2% | Lot mix shift |
| New operator shift | Defect rate 8% vs. baseline 2% | Confounded with night shift hardware |

### 3. Causal analysis
- DiD estimate (recipe v3.2): +5.2pp defect rate, 95% CI [3.1, 7.3]
- Refutation: random common cause Δ=+0.1, placebo Δ=−0.05 → robust
- Parallel-trends check: ✓ (plot in appendix)

### 4. Recommended action
- Roll back recipe v3.2 (highest evidence)
- Increase sampling on Chamber 5 for next 5 lots (secondary signal)
- Audit night-shift training (lowest priority — confounded)

### 5. What we cannot rule out
- Unobserved supplier-material drift in same week
- Chamber 5 fixture wear (no instrumentation)
```

## 7a. Writing the Tier 2 RCA report

The § 7 template above is the artifact. This subsection is the discipline for
producing it from any analysis result, in any domain.

### Prereq checklist

Copy this into your scratch space and check off. Skipping any item produces a
"Findings memo," not an RCA report — relabel and drop § Causal + § Recommended
action if a prereq fails.

```
RCA prereqs:
- [ ] Confirmed change point: p<0.01, ≥30 samples each side, visually clean
- [ ] Preprocessing log: nulls handled per column, collinear clusters collapsed
- [ ] Ranked top 3 candidate causes with strongest evidence number each
- [ ] Quantile (or per-level) breakdown of the #1 driver vs target
- [ ] Causal estimate for #1 (DiD / matching) with at least one refutation
- [ ] Concrete list of unmeasured variables that could be alternative causes
```

### Section-by-section discipline

Author sections in this order. Write TL;DR last.

| Section | What it enforces |
|---|---|
| **TL;DR** (1–2 sentences) | Commitment. One cause, one action. No hedging, no "or", no undated language. |
| **The change** | The shift is real, not noise. Requires method + p-value + n on each side + visual reference. |
| **Data & methods** (2 tables) | Auditable prep. Table columns: preprocessing → step / action / why; methods → question / method / why-this-one. |
| **Where the signal lives** | Actionable threshold. Quantile-bucket table of #1 driver + threshold sentence: "feature > value carries N× baseline rate." |
| **Top candidate causes** (≤3 rows) | Ranking + integrity. Every row has a Confounders column — empty column means no critical thinking happened. |
| **Causal analysis** (#1 only) | Correlation vs causation. Estimate + CI + ≥1 refutation. Applying to all three candidates dilutes the report. |
| **Recommended action** (numbered) | Something an operator can do. #1 must be reversible and high-evidence. "Investigate further" as #1 signals unfinished analysis. |
| **What we cannot rule out** (≥3 bullets) | Integrity. Empty section reads as overconfident; generic disclaimers ("data may be incomplete") don't count. |

### Cross-domain slot mapping

The template is the same; the nouns change.

| Slot | Manufacturing | Web product | Healthcare | Finance | SRE / ops |
|---|---|---|---|---|---|
| Metric | defect rate | conversion, churn | readmit, complication | default, fraud rate | error rate, p99 latency |
| Change agent | recipe, tool maintenance | feature flag, deploy, A/B | protocol, formulary | model version, policy | deploy, config push |
| Cohort | lot, wafer | user, session | patient, ward | account, transaction | request, host group |
| Driver shape | sensor reading | session duration | lab value, score | risk score, velocity | queue depth, GC pause |
| Causal lever | recipe rollback | flag rollback | protocol revert | model rollback | deploy revert |

### Length budget

- **Sweet spot:** 60–75 lines / 450–550 words.
- **Ceiling:** 90 lines / ~700 words. Above this, move detail to the appendix.
- **Floor:** 40 lines. Below this, a section is missing.

### Quality checklist (pre-ship)

- [ ] TL;DR's stated cause matches Recommended action #1 (same noun, same numbers)
- [ ] Every number in the body appears in the analysis log — no new numbers introduced in the report
- [ ] § Where the signal lives produces a concrete threshold sentence
- [ ] § Top candidates has ≤3 rows; every row has a confounder listed
- [ ] § Causal analysis covers #1 candidate only, with ≥1 refutation test
- [ ] § Recommended action #1 is reversible and high-evidence
- [ ] § Cannot rule out lists concrete gaps, not generic disclaimers
- [ ] TL;DR was written last, not first

### Three failure modes (and the fix)

1. **Report is a SHAP plot wrapped in prose** — no bucket table, no threshold, reader can't act. → Add § Where the signal lives with a quantile table + threshold sentence.
2. **All three candidates get causal analysis** — dilutes attention, reads as fishing. → Restrict § Causal to #1 only. Demote others to the appendix.
3. **Recommended action #1 is "investigate further"** — analysis didn't finish. → Either find a reversible first step (rollback, tighter threshold, increased sampling), or relabel the document "Findings memo" and drop § Causal + § Action.

## 7b. Producing the HTML report

For stakeholder delivery, print-to-PDF, or email attachment, produce the
Tier 2 report as a self-contained HTML file. Everything from § 7a stays;
this subsection covers rendering.

### When HTML vs. markdown

| Capability | Markdown | HTML |
|---|:---:|:---:|
| Interactive Plotly plots inline | ✗ | ✓ |
| Collapsible details / expandable sections | ✗ | ✓ |
| Print to PDF with page breaks | ~ | ✓ |
| Runs offline in any browser | ~ | ✓ |
| Copy-paste into email preserving format | ✗ | ✓ |

Use markdown for scratch analysis and internal notebook artifacts. Use HTML
for anything a stakeholder sees.

### Output spec

**One file. `.html`. Self-contained.**

- Single `<html>` document. No external CSS, no external JS beyond the Plotly CDN.
- Inline `<style>` block with the CSS below (verbatim; adjust palette only if brand-mandated).
- Plotly figures embedded via `fig.to_html(include_plotlyjs="cdn", full_html=False)` — first fragment loads the CDN; subsequent fragments use `include_plotlyjs=False`.
- Matplotlib fallback plots: base64 `<img src="data:image/png;base64,…">`.
- Zero JavaScript beyond Plotly. Report degrades gracefully to numbers+tables if Plotly won't run.
- File size target: < 500 KB. Above that, move heavy plots to `_appendix.html` linked from § Appendix.

### CSS block — verbatim

```css
:root {
  --fg: #1a1a1a; --muted: #6b6b6b; --bg: #ffffff;
  --border: #e5e5e5; --code-bg: #f7f7f7;
  --sev: #c62828; --ok: #2e7d32; --accent: #1565c0;
}
* { box-sizing: border-box; }
body {
  font: 14px/1.55 ui-sans-serif, -apple-system, system-ui, sans-serif;
  color: var(--fg); background: var(--bg);
  max-width: 880px; margin: 32px auto; padding: 0 24px;
}
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; padding-bottom: 4px;
     border-bottom: 1px solid var(--border); }
h3 { font-size: 14px; margin: 20px 0 6px; color: var(--muted); }
.meta { color: var(--muted); font-size: 13px; margin: 0 0 20px; }
.tldr { padding: 12px 16px; border-left: 3px solid var(--sev);
        background: #fff5f5; margin: 16px 0; }
.tldr strong { color: var(--sev); }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px;
        font-size: 13px; }
th, td { border-bottom: 1px solid var(--border); padding: 6px 10px;
         text-align: left; vertical-align: top; }
th { background: #fafafa; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code, pre { background: var(--code-bg); border-radius: 3px;
            font: 12.5px/1.4 ui-monospace, Menlo, monospace; }
code { padding: 1px 5px; }
pre { padding: 10px 12px; overflow-x: auto; }
.threshold { padding: 10px 14px; background: #fffde7;
             border-left: 3px solid #f9a825; margin: 8px 0; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 11px; font-weight: 600; text-transform: uppercase; }
.sev-high { background: var(--sev); color: white; }
.sev-med  { background: #ef6c00; color: white; }
.sev-low  { background: var(--muted); color: white; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--accent); font-weight: 500; }
aside { padding: 10px 14px; background: #f5f9ff;
        border-left: 3px solid var(--accent); margin: 8px 0; }
@media print {
  body { max-width: none; margin: 0; padding: 12mm; }
  h2 { page-break-before: auto; page-break-inside: avoid; }
  details { page-break-inside: avoid; }
  details:not([open]) summary::after { content: " (expand in appendix)"; }
}
```

### HTML element per section

Same 8 sections as § 7a. Element choice per section:

| Section | HTML element |
|---|---|
| Header | `<header>` with `<h1>` + severity `<span class="badge sev-…">` + `<p class="meta">` (author, date, source) |
| TL;DR | `<section class="tldr">` — colored left border, larger font |
| The change | `<section>` with `<dl>` (metric / direction / change point) + one embedded Plotly line chart with change-point vertical line |
| Data & methods | Two `<table>`s inside `<details><summary>` collapsibles (collapsed by default) |
| Where the signal lives | `<table>` for the quintile breakdown + `<p class="threshold">` for the threshold sentence + optional Plotly bar chart |
| Top candidate causes | `<table>` — 3 rows max, numbers in `<code>` |
| Causal analysis | `<section>` with an `<aside>` box for the estimate + CI; refutation as `<ul>` |
| Recommended action | `<ol>` with `<strong>` on action verbs |
| Cannot rule out | `<ul>` |
| Appendix | `<details>` collapsible list of links / embedded thumbnails |

Collapse only Data & methods + Appendix. Never collapse the top candidates,
the threshold sentence, or the recommended action — those must be visible on
the first screen.

### Generation approach — per-task script

Do not bundle a Python helper. Generate a per-analysis script that computes
the payload from your analysis artifacts, renders Plotly fragments, and
formats a single HTML file. Skeleton:

```python
# 1. Compute payload from analysis artifacts (one dict per section)
payload = {
    "metric": "defect_rate",
    "date": "2026-04-12",
    "severity": "high",              # → CSS class for the badge
    "tldr": "defect_rate jumped 2.1% → 10.5% at recipe v3.2 deploy. Roll back.",
    "before": 0.021, "after": 0.105, "delta_pp": 8.4,
    "changepoint": {"ts": "2026-04-12 14:00", "method": "PELT + BIC",
                    "test": "MW", "p": 0.001, "n_pre": 200, "n_post": 180},
    "prep_rows": [...],              # list of (step, action, why)
    "methods_rows": [...],           # list of (question, method, why)
    "quintile_rows": [...],          # list of (label, range, n, rate, delta)
    "threshold_sentence": "s406 > 6.8 → 4× baseline",
    "candidates": [...],             # list of (rank, name, evidence, confounders)
    "causal": {"est": +0.052, "ci_lo": +0.031, "ci_hi": +0.073,
               "refute_random": +0.001, "refute_placebo": -0.005,
               "refute_subset": +0.003, "parallel_ok": True},
    "actions": ["Roll back recipe v3.2 (~2h, reversible)", ...],
    "cannot_rule_out": ["Supplier material drift (no per-lot data)", ...],
}

# 2. Render Plotly fragments (CDN loaded on first fragment only)
fig1_html = fig_metric_over_time.to_html(include_plotlyjs="cdn",  full_html=False)
fig2_html = fig_quintile_bar.to_html(include_plotlyjs=False, full_html=False)

# 3. Assemble the file with an f-string template. Structure follows the
#    element mapping table above verbatim. Keep the template linear —
#    conditional branches deeper than one level become unreadable.
html = f"""<!doctype html><html lang="en"><head>
  <meta charset="utf-8"><title>RCA: {payload['metric']} on {payload['date']}</title>
  <style>{CSS}</style></head><body>
  <header>...</header>
  <section class="tldr">...</section>
  ...
</body></html>"""

# 4. Write to disk
from pathlib import Path
out = Path(f"reports/rca_{payload['metric']}_{payload['date']}.html")
out.parent.mkdir(exist_ok=True); out.write_text(html)
print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
```

**Payload separation pattern:** for reports that will regenerate under
multiple audiences (exec, audit, engineering), write the payload to a
sidecar `payload.json` and load it in the template script. One analysis +
one payload + one template → many rendered variants.

### Rendered example — the header fragment

Calibration reference. Top of a rendered report as HTML source:

```html
<header>
  <h1>RCA: defect_rate shifted on 2026-04-12
      <span class="badge sev-high">High</span></h1>
  <p class="meta">Author: J. Kim &middot; Generated 2026-05-19 &middot;
     Source: fab_metrics.parquet + mes_events.csv</p>
</header>

<section class="tldr">
  <strong>TL;DR.</strong> defect_rate jumped 2.1% → 10.5% at 14:00 on
  April 12, coinciding with recipe v3.2 deploy. DiD estimate +5.2pp
  (95% CI: +3.1 to +7.3), refutation passed.
  <strong>Recommend rollback.</strong>
</section>

<section>
  <h2>The change</h2>
  <dl>
    <dt>Metric</dt> <dd>defect_rate (50-lot rolling window)</dd>
    <dt>Direction</dt> <dd>2.1% → 10.5% (Δ = +8.4pp, 5× baseline)</dd>
    <dt>Change point</dt>
    <dd>2026-04-12 14:00 — PELT (BIC); MW p=0.001;
        pre n=200 / post n=180 </dd>
  </dl>
  <!-- Plotly fragment for metric-over-time with change-point vline -->
  {fig_metric_over_time_html}
</section>
```

### Quality checklist (HTML-specific, on top of § 7a)

- [ ] Opens correctly in Chrome, Safari, and Outlook preview
- [ ] Prints to 2–3 pages with sensible page breaks
- [ ] `<title>` matches `<h1>` (browser tab / email subject preview)
- [ ] Severity badge in header matches TL;DR tone
- [ ] Numbers in TL;DR match numbers in the body verbatim
- [ ] Every table is a real `<table>`, never a plot image of a table
- [ ] `<details>` collapsibles cover only Data & methods + Appendix
- [ ] Plotly `include_plotlyjs="cdn"` on the first figure only
- [ ] No absolute file paths in `<img src>` or `<a href>` (all base64 or relative)
- [ ] File size under 500 KB

### Three failure modes (HTML-specific)

1. **Regenerating the report to change one number requires editing the script.** → Separate `payload.json` from the template script.
2. **Plotly plots don't render in Outlook / Slack preview.** → Ship a `_static.html` companion with matplotlib PNGs base64-embedded.
3. **File grows past 500 KB with embedded plots.** → Move heavy plots to `_appendix.html` linked from § Appendix.

## 8. RCA Anti-Patterns

| Anti-pattern | Symptom | Fix |
|---|---|---|
| Treating SHAP-rank as causal rank | "The most influential feature is X, so fix X" | Add a DiD or refutation step before recommending action |
| Skipping CPD on time-indexed data | Reporting an excursion without a confirmed start time | Always run PELT and report the change point with sanity checks |
| Comparing tools/operators without case-mix control | Tool A "looks worst" but only runs the hard lots | Stratify by lot type, or use propensity matching |
| SPC on autocorrelated data | False alarms every other subgroup | Check `series.autocorr()`; if > 0.5, chart AR(1) residuals |
| Cherry-picking the "most affected" entity | Headline cause is the outlier, not the systemic driver | Sweep all entities; require Bonferroni-significant lift |
| Surface-cause vs. root-cause confusion | "Pressure was high" — but *why* was pressure high? | Apply 5-why depth; the root is upstream of the proximate change |
| Multiple testing without correction | "Found 47 significant factors" with α=0.05 across 940 sensors | Bonferroni or BH-FDR; for SECOM-scale, expect ~5% false positives |
| Reporting p-value without effect size | "p < 0.0001" on a 0.1pp shift in a metric with 5pp seasonality | Always pair p-values with η² / Cohen's d / lift |

## 9. Performance Cheatsheet

| Issue | Fix |
|---|---|
| PELT slow on > 100k samples | Use `model="l2"` (fastest); set `min_size` larger to reduce candidate breaks |
| `ruptures` over-segments | Raise `penalty` (default BIC × 2); add visual sanity check |
| `dowhy` slow for large n | Subsample to 10k for refutation; full data for point estimate |
| Commonality sweep slow on many factors | Vectorize with `pd.crosstab`; parallelize per-factor loop with `joblib` |
| ANOVA crashes on unbalanced design | Use Type-III SS via `typ=3` in `anova_lm` |
| Propensity matching drops too many units | Loosen `caliper` to 0.2σ of the propensity distribution |

## 10. End-to-End RCA Workflow

```python
# 1. Confirm a change happened: CPD on the metric
bkps = detect_changepoints(metric_series, model="rbf", min_size=30)
# 2. Validate the change point: pre vs post Mann-Whitney + plot
mw_p = stats.mannwhitneyu(pre, post).pvalue
assert mw_p < 0.01, "Change point not significant — investigate noise"

# 3. Commonality: which factors are over-represented post-change?
post_df = df.loc[df["timestamp"] >= change_time]
for factor in categorical_factors:
    sig = commonality(post_df, factor, "is_defective")
    # ... collect candidates

# 4. Causal: DiD on the strongest candidate
model = diff_in_diff(df, "timestamp", "treatment_group", "defect_rate",
                     change_time, treated_value=1, control_value=0)

# 5. Refute: dowhy with the candidate's DAG
cm = CausalModel(data=df, treatment="candidate", outcome="defect_rate",
                 common_causes=identified_confounders)
estimate = cm.estimate_effect(cm.identify_effect(),
                              method_name="backdoor.propensity_score_matching")
refute = cm.refute_estimate(cm.identify_effect(), estimate, "random_common_cause")

# 6. Report: structured narrative with all five sections of the template
```

If any step in this pipeline fails (CPD finds no change, commonality
returns nothing significant, DiD shows no effect, refutation knocks out
the estimate) — **report that the data does not support a causal claim
and recommend further investigation**. False positives in RCA are very
costly: they trigger wrong fixes that mask the real issue.
