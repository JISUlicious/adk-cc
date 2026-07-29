# Qualitative RCA Techniques — Pareto, Fishbone, 5-Why

Complementary to the statistical toolkit — Pareto scopes what matters,
Fishbone brainstorms candidate causes, 5-Why builds a testable causal
chain. **All three are candidate generators. Statistical verification
stays in the other RCA files.**

## Contents

- Rule out the gauge first (MSA) — is the defect even real?
- Pareto: what to work on first
- Is/Is-Not (Kepner-Tregoe) — scope the problem before brainstorming
- Fishbone (Ishikawa, 6M taxonomy) with statistical verification map
- 5-Why with statistical falsification
- Combining: MSA → Pareto → Is/Is-Not → Fishbone → 5-Why → verify
- Anti-patterns

Read this file when working on: "there's a huge backlog of failure modes,
where do we start?", "brainstorm possible causes before diving into
data", "we have a suspected causal chain, how do we falsify each link?".
Pair with `rca-commonality.md` (verifies Machine/Material branches),
`rca-causal-analysis.md` (verifies Method branches), and
`root-cause-analysis.md` § 2 (verifies Milieu/environment).

## Rule out the gauge first (MSA)

Before attributing a shift to the *process*, confirm it isn't the
*measurement system*. A probe swap, a recalibration, a new inspection
recipe, or a firmware update on the metrology tool can move a metric with
no physical change in the product. This is the first question a veteran
asks about any excursion — and the one most data analyses skip.

**Cheap checks, in order:**
1. **Timeline join.** Did any gauge / calibration / inspection-recipe
   event land within the change-point window (`root-cause-analysis.md`
   § 1)? If a metrology event coincides with the shift, treat it as the
   leading suspect, not the process.
2. **Reference-sample re-measure.** Re-measure retained known-good parts
   on the current gauge. If *they* now read shifted, the gauge moved.
3. **Gauge R&R (if you have repeated measures).** Decompose total
   variation into part-to-part vs. measurement (repeatability +
   reproducibility). Report `%GRR = σ_measurement / σ_total` and the
   number of distinct categories `ndc = 1.41 · σ_part / σ_gauge`.
   Rule of thumb: **%GRR < 10% good, 10–30% marginal, > 30% the gauge
   is unfit** — investigate metrology before the process.

```python
# %GRR from a crossed operator×part study (variance-components sketch)
import statsmodels.formula.api as smf
m = smf.ols("measure ~ C(part) + C(operator) + C(part):C(operator)", data=g).fit()
aov = sm.stats.anova_lm(m, typ=2)
# σ² components → repeatability (residual), reproducibility (operator+interaction),
# part-to-part (part). %GRR = sqrt(var_rr) / sqrt(var_total).
```

If MSA is untrustworthy, **stop** — no amount of commonality or DiD on a
noisy gauge yields a real cause.

## Pareto: what to work on first

Before drilling into any single cause, plot the cumulative distribution
of the loss you're trying to reduce. The 80/20 rule usually holds:
**20% of failure modes account for 80% of the loss.** Rationing analyst
attention to the top 20% is the single largest efficiency gain in any
RCA program.

```python
pareto = (df.groupby("failure_mode")["cost_dollars"]
            .sum()
            .sort_values(ascending=False)
            .to_frame(name="total"))
pareto["cum_pct"] = pareto["total"].cumsum() / pareto["total"].sum() * 100
top20 = pareto[pareto["cum_pct"] <= 80]

print(f"Top {len(top20)} of {len(pareto)} failure modes = 80% of loss")
print(top20)
```

**Choose the cost dimension deliberately.** Cost = dollars for capital
loss, hours for cycle-time loss, defect count for yield loss, incidents
for reliability. Pareto by defect count and Pareto by dollars often rank
differently — a rare but catastrophic mode may dominate dollars while a
frequent minor mode dominates counts. Report both when they diverge.

**Visual reference:** Pareto bar chart (descending) with a secondary axis
for cumulative percent — the classic diagram. Draw a horizontal line at
80% to mark the cutoff.

## Is/Is-Not analysis (Kepner-Tregoe)

Before brainstorming causes, **scope the problem** along four dimensions —
What, Where, When, How-big — and for each record both what the problem
**is** and, sharply, what it **is-not** but plausibly could have been.
The "is-not" column is the analytical payload: it narrows the cause space
and, crucially, **defines the control group** for the statistical tools.

| Dimension | IS | IS-NOT (but could have been) |
|---|---|---|
| What | which defect / product / feature | similar products that are *fine* |
| Where | line 3, chamber 5, edge of wafer | line 1-2, other chambers, wafer center |
| When | since the 06-20 recipe deploy | before 06-20; not on weekends |
| How-big | 14% defect rate, +9pp | not 50%; not a single-lot spike |

**Why this feeds the statistics directly:**
- The IS-NOT set is the **control / comparison group** for commonality
  (`rca-commonality.md`) and Diff-in-Differences (`rca-causal-analysis.md`).
  "Line 3 is bad, line 1-2 are fine" is exactly a DiD treated/control split.
- Sharp Is/Is-Not boundaries kill whole Fishbone branches for free: if
  the problem is **only** on the night shift, a material-lot cause that
  spans both shifts is already refuted — no test needed.
- A dimension where you *can't* fill the IS-NOT column is a data gap —
  record it as a limitation for the Tier 2 § "cannot rule out".

Run Is/Is-Not after Pareto (which picks *which* problem) and before
Fishbone (which brainstorms *why*) — it turns a vague complaint into a
bounded, testable comparison.

## Fishbone (Ishikawa) — 6M taxonomy

A candidate-generation tool, not an answer. Six branches (the 6M) span
the space of possible causes; you brainstorm at each and then hand each
candidate to a statistical test. **A Fishbone diagram with no
verification of any branch is not an RCA finding — it's a whiteboard
sketch.**

| Branch | Examples | How to statistically verify |
|---|---|---|
| **Man** (operator, shift, training) | new operator, night shift | Shift/operator commonality with case-mix warning (`rca-commonality.md`) |
| **Machine** (tool, equipment, fixture) | Chamber 5, robot arm | Per-tool commonality (`rca-commonality.md`) |
| **Material** (lot, supplier, consumable) | supplier batch, wafer lot | Lot / supplier commonality (`rca-commonality.md`) |
| **Method** (recipe, procedure, SOP) | recipe v3.2 deploy | DiD on recipe versions (`rca-causal-analysis.md`) |
| **Measurement** (gauge, calibration) | drifted probe, new inspection recipe | MSA / Gauge R&R — see "Rule out the gauge first" above; verify *before* the process branches |
| **Milieu** (environment, humidity, T°C) | seasonal ambient shift | SPC autocorrelation + covariate control (`root-cause-analysis.md` § 2) |

The verification column is what turns a Fishbone into an RCA. When a
team publishes a Fishbone with no verified branch, ask "which of these
six is the strongest hypothesis, and what commonality / DiD / SPC test
would kill it?" — then run that test first.

## 5-Why with statistical falsification

The classical 5-Why chain — "defect rate rose. Why? recipe changed. Why?
new operator deployed the wrong one. Why? training was rushed. Why?
release deadline. Why? unclear priorities" — is only load-bearing when
**each "why" step is tested, not asserted.**

Every step in the chain is a causal claim:
- Step 1: recipe change → defect rate rise (test with DiD)
- Step 2: new operator → recipe change (test with role-timestamp cross-check)
- Step 3: training gap → wrong recipe deployed (test with training-record vs. deploy-log join)
- Step 4: deadline → training rushed (usually not statistically testable, human factors)
- Step 5: unclear priorities → deadline set incorrectly (organizational, not statistical)

The bottom of the chain often crosses out of the statistical domain into
human factors — that's fine, but say so explicitly. Everything above
that boundary must have a test attached.

**The falsification rule:** for each "why" step, name the statistical
test that *would refute* the claim. If no test exists, the step is
speculation — mark it as such in the report and continue by mapping it
to a Fishbone branch that has a test.

## Combining: Pareto → Fishbone → 5-Why → statistical verify

The full qualitative-first pipeline:

1. **MSA** — rule out the gauge. If the measurement system moved, stop
   and fix metrology first.
2. **Pareto** — identify the single failure mode responsible for the
   largest chunk of loss. Everything below the 80% cutoff is deferred.
3. **Is/Is-Not** — scope What / Where / When / How-big; the IS-NOT set
   becomes the control group for the statistical tests.
4. **Fishbone** — brainstorm candidate causes across the 6M for that
   failure mode; drop branches the Is/Is-Not boundaries already refute.
5. **5-Why** — pick the top 1-2 surviving candidates and build a causal
   chain for each.
6. **Statistical verify** — run the tests attached to each step. Report
   only the chains where every testable step survived falsification.

This structure gives an engineer *why* to trust the finding, not just
*what* the finding is. It also produces the § 5 "What we cannot rule
out" content for the Tier 2 report — the untestable steps become the
declared limitations.

## Anti-patterns

| Anti-pattern | Symptom | Fix |
|---|---|---|
| Publishing Fishbone as the answer | 6-branch diagram in the deck, no test result | Fishbone is a candidate generator. Attach a statistical verification to at least the top branch before publishing. |
| 5-Why without falsification | Chain of 5 assertions, no test for any link | Every step must have a named statistical test that would refute it. Steps below the "human factors" boundary are the exception — mark them as such. |
| Fishbone with no verification of any branch | Whiteboard photo pasted into the report | Run the test for at least the leading branch. If time-constrained, publish only the verified branch and label the rest as "candidates for follow-up". |
| Pareto by wrong dimension | Ranking by defect count when dollars are the goal | State the cost dimension explicitly. When count and dollars disagree, report both. |
| Skipping Pareto entirely | Deep-dive on the first failure mode reported | Always Pareto first. The mode you were told about may not be in the top 5. |
| Attributing a shift without ruling out the gauge | "Defect rate jumped" — but a probe was recalibrated the same day | Run the MSA checks first. A metrology event in the change-point window is the leading suspect, not the process. |
