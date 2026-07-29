# D5 — Verification of Corrective Action

Prove the fix worked with statistical rigor. Declaring success without a
verification period is the most common D5 failure and the reason many
"resolved" RCAs recur within one quarter.

## Contents

- Sample-size planning (power analysis)
- Pre/post experimental design
- Three verification tests (central tendency / variation / rule stability)
- Success criteria (all-must-pass)
- Anti-patterns

Read this file when working on: "we applied the fix — did it work?",
"how long do we monitor before closing the corrective action?", or
"the metric looks better this week, can we call it done?". Pairs with
`root-cause-analysis.md` § 2 (SPC monitoring in the verification
window) and `rca-causal-analysis.md` (DiD when a parallel control
line exists).

## Sample-size planning (power analysis)

Before running the verification period, compute how much post-fix data
you actually need to detect the target improvement with a chosen power.
Verifying against a small n and calling it done inflates the false-pass
rate.

**Match the effect-size definition to the unit of analysis.** A defect
rate is a *proportion* over per-wafer Bernoulli outcomes — the per-wafer
SD at p=5% is √(0.05·0.95) ≈ 0.22, not a small "rate stddev". Feeding a
made-up rate SD into a continuous-mean power routine gives a nonsense n.
Use the proportion effect size (Cohen's *h*):

```python
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import proportion_effectsize

# Inputs from D3 (containment) and D4 (root cause) — n is in WAFERS:
p_pre    = 0.070                    # pre-fix defect rate
p_target = 0.050                    # rate we need to reach (2 pp better)
h = proportion_effectsize(p_pre, p_target)   # Cohen's h — the right ES for rates

n_per_group = NormalIndPower().solve_power(
    effect_size=h,
    alpha=0.05, power=0.80,
    ratio=1.0, alternative="larger",  # "we improved" is one-sided
)
print(f"Need n≥{int(n_per_group)+1} WAFERS per group at power=0.80")
```

If instead you monitor a *continuous* CTQ (a measured dimension, not a
pass/fail rate), the mean/SD form of `NormalIndPower` is correct — just
state whether n counts wafers or aggregated lots. Report `n_needed`
alongside every verification result: "we ran for two weeks" is not a
design; "we needed 380 wafers, observed 415, achieved power 0.84" is.

## Pre/post experimental design

Choose one of two designs based on what infrastructure exists:

- **A/B on parallel lines** — same recipe on multiple identical lines,
  fix applied to one. Cleanest design. Use DiD (`rca-causal-analysis.md`)
  to estimate the treatment effect.
- **In-time comparison with SPC** — no parallel line available. Compare
  the post-fix window to a matched pre-fix window (same length, same
  product mix). Use SPC to detect any regression during monitoring.

If neither design is feasible (e.g., a one-off tool with no control),
label the D5 result as "consistent with fix" rather than "verified" —
the causal claim is weaker.

## Three verification tests

All three must pass. A single "the mean went down" test is not
sufficient — variation and rule stability can fail independently.

### 1. Central tendency

Post mean vs. pre mean — Welch's t-test for continuous metrics,
proportion z-test for defect rates. When a parallel control line exists,
use DiD instead so seasonal or drift effects are subtracted out.

```python
from scipy import stats
t_stat, p = stats.ttest_ind(post_samples, pre_samples, equal_var=False)
delta = post_samples.mean() - pre_samples.mean()
print(f"Δmean = {delta:+.4f}  Welch p = {p:.4f}")
```

### 2. Variation (continuous CTQs only)

Post variance vs. pre variance — Levene's test (robust to non-normality)
or the classical F-test. **A fix that lowers the mean but doubles the
variance may reduce headline defect rate while introducing new failure
modes.** This test catches that.

**Scope:** this is a check for *continuous* characteristics (a measured
dimension, force, thickness). For a **proportion** (defect rate), the
variance σ²=p(1−p) is a deterministic function of the mean, so "post
σ ≤ pre σ" is automatic once the rate drops and carries no independent
information — skip it for rate metrics.

```python
lev = stats.levene(pre_samples, post_samples, center="median")
print(f"Levene p = {lev.pvalue:.4f}  ratio σ_post/σ_pre = "
      f"{post_samples.std()/pre_samples.std():.3f}")
```

### 3. Rule stability

**N = 20 consecutive in-control subgroups under Western Electric rules.**
This is the single strongest test — it says "the process has stayed
stable for a long enough window to conclude the change is not transient
noise." Any WE rule violation resets the counter.

```python
# Using SPC helpers from root-cause-analysis.md § 2
subgroup_stats = compute_subgroups(post_samples, subgroup_size=5)
we_violations = check_western_electric(subgroup_stats)
consecutive_ok = count_consecutive_in_control(we_violations)
print(f"Consecutive in-control subgroups: {consecutive_ok}/20")
assert consecutive_ok >= 20, "D5 verification incomplete — extend monitoring"
```

## Success criteria (all-must-pass)

All four criteria must hold before signing off D5:

- **Δmean ≥ target improvement** defined at D5 kickoff (not adjusted
  after seeing the data)
- **Post σ ≤ pre σ** (or Δσ within a pre-declared tolerance)
- **≥ 20 in-control subgroups** post-fix under Western Electric rules
- **No WE rule violations** in the verification period

Any single failure sends the team back to D4 to reconsider whether the
root cause was correctly identified, or D6 to strengthen the corrective
action.

## Anti-patterns

| Anti-pattern | Symptom | Fix |
|---|---|---|
| Declaring success on one good week | "Defect rate dropped last week, closing D5" | The verification period must be ≥ 2× the natural variation cycle of the process. Use SPC rule stability, not a single window. |
| Post-hoc target adjustment | "Target was 2 pp improvement; we got 1.4 — good enough" | The target from D5 kickoff is binding. Under-target means D4 or D6 needs revisiting. |
| No variation check | "Δmean is significant, done" | Run Levene test. Same mean + double variance = worse process. |
| No power calculation | "We monitored for two weeks" | State `n_needed` and `power_achieved`. Under-powered verification passes by chance. |
| No control comparison when available | "Post-fix rate is lower than pre-fix, done" | If a parallel unaffected line exists, use DiD. Seasonal or process-wide drift can create a false pre/post difference. |
