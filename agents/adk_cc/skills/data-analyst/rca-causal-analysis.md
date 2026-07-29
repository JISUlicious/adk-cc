# RCA Causal Inference and Designed Experiments

The distinction between correlation and causation for RCA. Contains both
the observational / retrospective toolkit (DAG, DiD, propensity matching,
dowhy) and the designed-experiment toolkit (ANOVA / Tukey / effect size).

## Contents

- Reject: reverse causation / common cause / selection / temporal mismatch
- Build a minimal DAG
- Diff-in-Differences (DiD)
- Propensity score matching
- `dowhy` — principled causal inference with explicit assumptions
- **§ 4.5: Occurrence cause vs escape cause** (two-question protocol)
- DOE / ANOVA — designed experiments
- Effect size: statistical vs. practical significance

Read this file when working on: "did X *cause* Y to shift", "estimating
the counterfactual", "should we roll back the recipe change", or "we
ran a DOE, what mattered". Pairs with `root-cause-analysis.md` § 1
(CPD provides the pre/post window) and `rca-commonality.md` (surfaces
the candidate cause to feed into DiD / dowhy).

## Reject before believing

The headline rule: **prediction is not causation.** Before claiming X
caused Y, rule out:

1. **Reverse causation** — did Y change first and X follow?
2. **Common cause** — is there an upstream Z that drives both X and Y?
3. **Selection bias** — is X over-represented in the population at risk?
4. **Temporal mismatch** — did X's change actually precede Y's shift?

## Build a minimal DAG (Directed Acyclic Graph)

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

## Diff-in-Differences (DiD) — workhorse for pre/post-change excursions

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
    # Cluster SEs by group: repeated per-unit observations are serially
    # correlated, so plain OLS SEs understate variance and inflate
    # significance (Bertrand-Duflo-Mullainathan 2004). Cluster on the
    # unit that the treatment varies at.
    model = smf.ols(f"{outcome} ~ post + treated + post:treated", data=df).fit(
        cov_type="cluster", cov_kwds={"groups": df[group_col]})
    did_estimate = model.params["post:treated"]
    print(model.summary().tables[1])
    print(f"\nDiD estimate: {did_estimate:+.4f}  (95% CI: "
          f"[{model.conf_int().loc['post:treated',0]:.4f}, "
          f"{model.conf_int().loc['post:treated',1]:.4f}])")
    return model
```

DiD assumes **parallel trends** in the pre-period — the treated and
control groups would have moved together absent the change. Plotting
the pre-period is the eyeball check; the formal test is a **placebo /
pre-trend regression** — interact `treated` with per-period time dummies
*before* the change and confirm none are significant. If pre-period
interactions are significant, the trends already diverged and DiD is
invalid.

**Few-clusters caveat:** clustered SEs are asymptotic in the *number of
clusters*. With fewer than ~40 groups they are unreliable — use a
wild-cluster bootstrap (`wildboottest`) instead of the closed-form CI.

## Propensity Score Matching — for non-randomized observational comparisons

When you can't use DiD (no clean pre/post), match treated units to
similar control units on observed covariates:

```python
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import NearestNeighbors

def propensity_match(df, treatment_col, outcome_col, covariates, caliper=0.2):
    """1:1 nearest-neighbour matching on the LOGIT of the propensity score.
    Returns the ATT (effect on the treated) — 1:1 matching keeps treated
    units and finds controls that look like them, so it does NOT estimate
    the population ATE. `caliper` is in units of SD of the logit-PS
    (Austin 2011 recommends 0.2)."""
    X = df[covariates].fillna(df[covariates].median())
    t = df[treatment_col].astype(int)
    ps = LogisticRegression(max_iter=1000).fit(X, t).predict_proba(X)[:, 1]
    # Match on the logit scale — raw probabilities are compressed near 0/1,
    # so a fixed probability caliper is too loose in the tails.
    logit_ps = np.log(ps / (1 - ps))
    df = df.assign(logit_ps=logit_ps)
    cal = caliper * logit_ps.std()
    treated = df[df[treatment_col] == 1].copy()
    control = df[df[treatment_col] == 0].copy()
    nn = NearestNeighbors(n_neighbors=1).fit(control[["logit_ps"]].values)
    dist, idx = nn.kneighbors(treated[["logit_ps"]].values)
    keep = dist.ravel() < cal
    matched = pd.concat([treated.iloc[keep],
                         control.iloc[idx.ravel()[keep]]])
    att = (matched.loc[matched[treatment_col]==1, outcome_col].mean()
         - matched.loc[matched[treatment_col]==0, outcome_col].mean())

    # Balance check — the credibility gate. Post-match |SMD| < 0.1 per
    # covariate means the groups are comparable; otherwise the estimate
    # is still confounded.
    mt = matched[matched[treatment_col]==1]; mc = matched[matched[treatment_col]==0]
    smd = ((mt[covariates].mean() - mc[covariates].mean()).abs()
           / df[covariates].std())
    print(f"Matched n: {keep.sum()} pairs (of {len(treated)} treated units)")
    print(f"ATT estimate: {att:+.4f}")
    print(f"Max post-match |SMD|: {smd.max():.3f}  (want < 0.10)")
    return matched, att, smd
```

## `dowhy` — for principled causal inference with explicit assumptions

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
# NOTE: each refuter has a DIFFERENT pass target — don't apply one rule to all.
refute_random  = model.refute_estimate(identified, estimate, "random_common_cause")
refute_placebo = model.refute_estimate(identified, estimate, "placebo_treatment_refuter")
refute_subset  = model.refute_estimate(identified, estimate, "data_subset_refuter")
est = estimate.value
print("Refutation summary (new_effect vs. its pass target):")
print(f"  Random common cause:  new = {refute_random.new_effect:+.4f}   (robust ≈ {est:+.4f})")
print(f"  Placebo treatment:    new = {refute_placebo.new_effect:+.4f}   (robust ≈  0.0000)")
print(f"  Data subset:          new = {refute_subset.new_effect:+.4f}   (robust ≈ {est:+.4f})")
```

A robust causal estimate behaves differently under each refuter, so a
single "stays the same" rule is wrong:

- **Random common cause** and **data subset** should leave `new_effect`
  ≈ the original estimate — adding an irrelevant confounder or dropping
  a random subset shouldn't move a real effect.
- **Placebo treatment** replaces the treatment with a random variable,
  so `new_effect` should collapse to ≈ **0** — a fake cause has no
  effect. If the placebo *reproduces* the original effect, the estimate
  is capturing noise or confounding, not causation, and is **suspect**.

Each refuter reports a `p_value` for its own null; use that rather than
eyeballing the deltas when the estimates are noisy.

## § 4.5: Occurrence cause vs escape cause

Every defect has **two causes** and the D7 preventive-action step needs
both.

- **Occurrence cause** — *why did it happen?* The physical / process
  chain that produced the defect. Reversing it is the D6 corrective
  action.
- **Escape cause** — *why didn't detection catch it before impact?* The
  monitoring / SPC / audit gap that let the defect slip through. In
  canonical 8D the escape point is *identified* in D4; its specific fix
  can land in D6, and D7 is the broader systemic prevention — control-plan
  / FMEA update plus **read-across** to sister lines and products.

**Reporting only the occurrence cause** — the most common D4 failure —
leaves the escape open, so the same class of defect recurs the next
time the occurrence mechanism triggers. This is why the same excursion
often recurs quarterly even after "resolution".

**Two-question protocol:** for every occurrence candidate you verify
statistically, ask a second question:

> "And what monitor / SPC rule / audit *should* have flagged this before
> impact — and why didn't it?"

The answer is the escape cause. It might be:
- No monitor existed on this signal (add SPC on the shifted sensor)
- A monitor existed but wasn't sensitive enough (Shewhart X-bar missed
  a sub-3σ drift → switch to EWMA — see `root-cause-analysis.md` § 2)
- Monitor existed but alarm threshold was wrong (recalibrate)
- Manual audit was skipped (make it automated)

**Worked example (SECOM style):**
- Occurrence: recipe v3.2 deploy on 2008-08-21 shifted the s406 cluster
  by +4.5σ → defect rate rose from 4.8% to 14.0%. D6 action: roll back
  recipe.
- Escape: pre-existing SPC on s406 was Shewhart X-bar, which is
  insensitive to sub-3σ persistent drifts of the kind recipe v3.2
  induced. D7 action: switch s406 chart to EWMA with λ=0.2, alarm on
  h=3.5σ.

**Reporting integration.** The Tier 2 report (`rca-reporting.md` § 7)
must have separate **Occurrence evidence** and **Escape evidence**
columns in the Candidate causes table. A candidate that has occurrence
evidence but no escape column signals the D7 gap wasn't analyzed — send
it back to D4.

**Close the loop into FMEA.** The occurrence/escape split *is* the input
to a pFMEA update — don't leave the analysis stranded in the report. The
verified occurrence cause updates the **Occurrence (O)** ranking for that
failure mode; the escape cause updates the **Detection (D)** ranking
(better if you added an EWMA chart, worse if you found none existed).
Recompute **RPN = S × O × D** (Severity unchanged) and confirm it dropped
after the D6/D7 actions — a corrective action that doesn't move the RPN
didn't address the risk. This is the D7 artifact auditors look for and it
falls out of work you've already done.

## DOE / ANOVA — for designed experiments

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
