# Diagnostic probes

Executable versions of the Pre-Modeling Diagnostics that `SKILL.md` requires.
The reference files explain the methods; these run them. Prefer running a probe
over re-implementing the check inline — the probe is the authoritative version,
and its exit code is something CI and an agent mid-analysis can both act on.

These are **framework probes**: fixed signature, no adaptation to the dataset.
Task-specific analysis code (a particular EDA pass, an RCA script for one
excursion) is still written per task — see the note at the bottom.

## What each probe asserts

| Probe | Passes when | Failing means |
|---|---|---|
| `collinearity_probe.py` | No feature is redundant with the others — every VIF below the severe threshold, no deterministic cross-type binding | OLS β will flip sign across re-runs; "top driver" is arbitrary within the redundant cluster |
| `null_audit_probe.py` | Every column is complete, or its missingness carries no signal about the target | `dropna()` restricts the sample to a self-selected subgroup; every downstream coefficient describes that subgroup, not the population |
| `leakage_probe.py` | No feature restates the target, and no summary rollup shadows its own components | The model's top driver is a tautology and R² measures an identity, not a relationship |
| `cluster_representative.py` | Every multicollinear cluster resolves to one defensible representative | At least one cluster is a genuine tie — picking arbitrarily silently changes what the analysis concludes |
| `premodel_audit.py` | All four diagnostics pass (the three above plus target skew) | The per-check output names which one blocked |

## Usage

```bash
python scripts/premodel_audit.py data.csv --target SalePrice      # one verdict
python scripts/collinearity_probe.py data.csv --target SalePrice  # one check
python scripts/premodel_audit.py data.csv --target y --json       # machine-readable
```

Shared flags: `--target`, `--exclude COL …`, `--sample N` (seeded), `--json`.
Run any probe with `-h` for its own thresholds.

Accepts CSV, TSV, Parquet, Excel, and JSON/JSONL — dispatched on file extension.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Check passed |
| 1 | Check failed — a real finding the caller must act on |
| 2 | The probe could not run (bad path, unreadable file, missing `--target`) |

The 1/2 split matters: "your data has a problem" and "I couldn't look" need
different responses from both CI and an agent.

## Dependencies

Tier-1 only — `pandas`, `numpy`, `scipy`. Parquet additionally needs `pyarrow`
and Excel needs `openpyxl`; both are in the common set, and a missing reader is
reported as a usage error rather than a traceback.

No probe requires scikit-learn, xgboost, shap, or statsmodels. Those belong to
the modeling step the probes gate, not to the gate itself.

## Reading the output honestly

**Exclude ID columns.** A row index or record ID is perfectly collinear with
anything else monotonic in row order and will dominate the VIF table for no
useful reason: `--exclude "Unnamed: 0" Order PID`.

**Structural absence is not missingness.** When NaN means "not applicable"
rather than "not recorded" — Ames `Pool QC` is null because there is no pool —
encode it as an explicit level before running `null_audit_probe.py`, or the
probe will correctly-but-uselessly recommend dropping a column that is actually
fully informative.

**VIF needs rows.** The collinearity probe refuses to compute VIF when rows
< features + 2 (the design matrix is rank-deficient there, so every VIF reads
infinite for arithmetic reasons) and warns when n/p < 10, where estimates are
unstable. Wide sensor data should be narrowed by domain or cluster first;
`--max-features` guards the default.

**Association measures follow the dtype pair.** Numeric×numeric uses Spearman ρ,
categorical×numeric uses √η², categorical×categorical uses Cramér's V. Forcing
one measure onto all combinations manufactures false findings — Cramér's V on a
ranked continuous target saturates at 1.0 for any categorical, which reads as
perfect leakage and is not.

**Sparse columns are reported, not scored.** A 99%-null column can post a
perfect association off a dozen rows. Below 30 complete rows the probes return
"unscoreable" instead of a number.

## Calibration

Run against `data/ames_housing.csv`, which every reference file uses for its
worked examples. Expected findings, all documented in the reference text:

- `collinearity_probe.py` — `Gr Liv Area` at VIF = ∞, because it equals
  `1st Flr SF + 2nd Flr SF + Low Qual Fin SF` exactly. Same for
  `Total Bsmt SF` over its three basement components.
- `leakage_probe.py` — `Overall Qual` tops the ranking with the per-component
  ratings (`Exter Qual`, `Bsmt Qual`, `Kitchen Qual`) immediately behind: the
  rating-summary tautology.
- `null_audit_probe.py` — the garage and basement column families show
  informative missingness (|assoc| ≈ 0.28 and ≈ 0.20), because absence there
  means "no garage / no basement", which itself predicts price.

## Scope — what belongs here and what does not

A probe earns a place in this directory when its signature is stable across
datasets: it takes a DataFrame plus column roles and returns a verdict. That is
true of every check above.

Analysis code is different. An EDA pass, a change-point investigation, an RCA
report script — those depend on the columns, the question, and the shape of the
data, and shipping a fixed version would be worse than generating one per task.
The reference files carry those as patterns to adapt, deliberately, not as
scripts to run.
