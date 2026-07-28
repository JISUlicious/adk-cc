---
name: statistical-testing
description: >
  Answer "is this difference real?" — pick the right test for the design, check
  its assumptions, report effect size with a confidence interval, and correct for
  multiple comparisons. Use for A/B and experiment analysis, before/after
  comparisons, and any claim that one group differs from another.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the test choice is justified by the data's design and measured assumptions, not assumed", "an effect size with a confidence interval is reported alongside any p-value", "multiple comparisons are counted and corrected, or their absence is stated", "sample size / power is addressed before concluding 'no difference'"]}
---

# Statistical testing

Distinct from `data-analyst` (which explores, cleans and transforms): this skill
covers **inference** — deciding whether an observed difference is more than
noise, and quantifying how big it is. The common failures are choosing a test by
habit, reporting a p-value with no effect size, and reading "not significant" as
"no effect".

Python runs on adk-cc's uv-managed interpreter; `scipy` is in the `core` tier and
importing `statsmodels` provisions the `stats` tier on first use.

## Workflow

### 1. State the question as a comparison
What is being compared, on what metric, at what unit of analysis? The unit
matters more than anything else here: if users see multiple sessions, sessions
are not independent, and a test that assumes they are will overstate
significance. Say what the unit is and why.

### 2. Look at the data before choosing a test
```python
df.groupby(group)[metric].agg(["count", "mean", "std", "median"])
```
Then check what the candidate test assumes — and measure it rather than
asserting it:

| Assumption | Check |
|---|---|
| Normality (small n) | `scipy.stats.shapiro`, plus a histogram/QQ look |
| Equal variance | `scipy.stats.levene` |
| Independence | argued from the design, not testable from the column |
| Enough events (proportions) | expected count per cell ≥ ~5 |

### 3. Pick the test the design implies
- Two independent groups, continuous → Welch's t-test (`equal_var=False` by
  default; equal-variance is the special case, not the norm).
- Skewed or ordinal, or small n with non-normality →
  Mann-Whitney U (compares distributions/medians, not means — say which).
- Paired / before-after on the same unit → paired t or Wilcoxon signed-rank.
- Proportions → two-proportion z or Fisher's exact (small counts).
- 3+ groups → ANOVA / Kruskal-Wallis, then post-hoc **with** correction.
- Time-ordered data → do not treat sequential observations as independent
  samples; use a time-series method (see `data-analyst`'s change-point material).

### 4. Report effect size and interval — always
A p-value answers "would noise do this?", not "does it matter?". Report:
- **difference in means / medians / rates** in the metric's own units;
- a **95% CI** on that difference;
- a standardized size where useful (Cohen's d, odds ratio, relative lift);
- and n per group.

An interval that spans zero and a p-value above threshold say the same thing —
lead with the interval, because it also shows how much is still on the table.

### 5. Multiple comparisons
Count every comparison you ran, including the ones you did not report. Correct
(Holm or Benjamini-Hochberg) or state plainly that you did not and that the
p-values are therefore exploratory. Testing five metrics at 0.05 gives roughly a
one-in-four chance of a false positive somewhere.

### 6. Before saying "no difference"
Non-significant ≠ equivalent. Report the smallest effect the sample could have
detected (power), or the CI's upper bound: "we can rule out a lift above X%".
If the sample cannot support the question, say what n would.

## Output

```
Question · unit of analysis · design
Assumption checks   → what was measured, what it implied
Test used           → and why this one
Result              → effect size [95% CI], p, n per group
Comparisons run     → count, correction applied
Interpretation      → in the metric's units, with what it does NOT establish
```

Never present a conclusion the design cannot support — an observational
difference is an association; say so rather than implying cause.
