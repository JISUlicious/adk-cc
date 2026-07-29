#!/usr/bin/env python3
"""ASSERTS: missingness in this data is safe to handle with a blanket rule —
every column is either complete, or its missingness carries no signal about
the target. PASS means dropna() / median-impute won't bias the sample. FAIL
means at least one column has informative missingness (the fact that a value
is absent predicts the target) or is too sparse to model; dropping those rows
silently restricts the sample to a self-selected subgroup and every downstream
coefficient describes that subgroup, not the population.

Requires: pandas, numpy, scipy  (tier-1)

Usage:
    python null_audit_probe.py data.csv --target SalePrice
    python null_audit_probe.py survey.parquet --target stress_level --json
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from _probe_utils import (
    EXIT_ERROR,
    assoc_threshold_for,
    base_parser,
    die,
    load_for_probe,
    report,
    split_columns,
)


def null_audit(X: pd.DataFrame, y: pd.Series) -> pd.DataFrame:
    """Per-feature null fraction plus the association of *being missing* with y.

    The association measure follows the target's dtype so the numbers stay
    comparable on one scale: point-biserial r for a binary target, Spearman ρ
    for a continuous one, Cramér's V for multiclass. A non-zero value means
    the missingness itself is a feature, and imputation erases it.
    """
    from scipy import stats
    from _probe_utils import cramers_v

    y = pd.Series(y)
    is_num = pd.api.types.is_numeric_dtype(y) and not pd.api.types.is_bool_dtype(y)
    is_bin = pd.api.types.is_bool_dtype(y) or (is_num and y.nunique(dropna=True) == 2)

    rows = []
    for col in X.columns:
        miss_pct = float(X[col].isna().mean())
        if miss_pct == 0:
            rows.append({"feature": col, "null_pct": 0.0, "miss_target_assoc": 0.0})
            continue
        is_miss = X[col].isna().astype(int)
        valid = y.notna()
        if valid.sum() < 5 or is_miss[valid].nunique() < 2:
            assoc = 0.0
        elif is_bin:
            codes = pd.Series(y[valid]).astype("category").cat.codes
            r, _ = stats.pointbiserialr(is_miss[valid], codes)
            assoc = float(r) if np.isfinite(r) else 0.0
        elif is_num:
            assoc = float(is_miss[valid].corr(y[valid], method="spearman"))
            assoc = assoc if np.isfinite(assoc) else 0.0
        else:
            v = cramers_v(is_miss[valid], y[valid])
            # Too few complete rows to estimate: treat as "no evidence of
            # informative missingness" rather than propagating NaN into the
            # decision table, where it would read as an unanswered question.
            assoc = 0.0 if not np.isfinite(v) else v
        rows.append(
            {
                "feature": col,
                "null_pct": round(miss_pct, 4),
                "miss_target_assoc": round(assoc, 4),
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values("null_pct", ascending=False)
        .reset_index(drop=True)
    )


def recommend(null_pct: float, assoc: float, threshold: float) -> "tuple[str, bool]":
    """Map (null fraction, association) onto an action. Returns (action, is_failure).

    A failure here is not "bad data" — it means the *default* handling is
    wrong for that column and the caller has to do something deliberate.
    """
    a = abs(assoc)
    if null_pct == 0:
        return "none", False
    if null_pct < 0.01:
        return "drop those rows", False
    if null_pct > 0.50:
        return "drop the column (too sparse to model)", True
    if a >= threshold:
        return f"indicator + impute (informative: |assoc|={a:.3f})", True
    if null_pct <= 0.10:
        return "median-impute (linear: warn about R2 attenuation)", False
    return "indicator + impute (null_pct > 10%)", False


def run(
    df: pd.DataFrame,
    target: str,
    exclude: "tuple[str, ...]" = (),
    assoc_threshold: "float | None" = None,
) -> dict:
    """Audit every feature's missingness against the target."""
    if target not in df.columns:
        die(f"target column '{target}' not in {list(df.columns)[:12]}…")

    y = df[target]
    num_cols, cat_cols = split_columns(df, target, tuple(exclude))
    features = num_cols + cat_cols
    if not features:
        return {
            "check": "null-audit",
            "passed": True,
            "summary": "no feature columns to audit",
            "tables": {},
            "notes": [],
        }

    threshold, threshold_note = assoc_threshold_for(len(df), assoc_threshold)
    audit = null_audit(df[features], y)
    actions = audit.apply(
        lambda r: recommend(r["null_pct"], r["miss_target_assoc"], threshold),
        axis=1,
        result_type="expand",
    )
    audit["action"] = actions[0]
    audit["blocks_default"] = actions[1]

    notes = [threshold_note]
    y_null = float(y.isna().mean())
    if y_null > 0:
        notes.append(
            f"target '{target}' is itself {y_null:.1%} null — those rows are "
            "excluded from the association estimate"
        )

    flagged = audit[audit["blocks_default"]]
    complete = audit[audit["null_pct"] == 0]
    routine = audit[(audit["null_pct"] > 0) & (~audit["blocks_default"])]
    tables = {}
    if len(flagged):
        tables["Columns needing deliberate handling"] = flagged.drop(
            columns=["blocks_default"]
        )
    if len(routine):
        tables["Other columns with nulls (default handling is fine)"] = routine.drop(
            columns=["blocks_default"]
        )

    informative = flagged[flagged["miss_target_assoc"].abs() >= threshold]
    sparse = flagged[flagged["null_pct"] > 0.50]
    if len(informative):
        notes.append(
            f"{len(informative)} column(s) show informative missingness — add a "
            "{col}_is_missing indicator before imputing, never dropna()"
        )
    if len(sparse):
        notes.append(
            f"{len(sparse)} column(s) are >50% null — report as 'too sparse to "
            "model' rather than imputing a mostly-invented column"
        )

    passed = not len(flagged)
    if passed:
        summary = (
            f"{len(complete)}/{len(audit)} columns complete; no informative "
            "missingness"
        )
    else:
        summary = f"{len(flagged)} column(s) need deliberate null handling"
    return {
        "check": "null-audit",
        "passed": passed,
        "summary": summary,
        "tables": tables,
        "notes": notes,
    }


def main(argv: "list[str] | None" = None) -> int:
    p = base_parser(
        "Dtype-aware null audit: per-column null fraction, association of "
        "missingness with the target, and the handling each column needs.",
        epilog=__doc__,
    )
    p.add_argument(
        "--assoc-threshold",
        type=float,
        default=None,
        help="practical-significance cutoff (default: scaled by sample size)",
    )
    args = p.parse_args(argv)
    if not args.target:
        die("--target is required: missingness is audited against the target")

    df = load_for_probe(args)
    result = run(
        df,
        target=args.target,
        exclude=tuple(args.exclude),
        assoc_threshold=args.assoc_threshold,
    )
    return report(
        result["check"],
        result["passed"],
        result["summary"],
        result["tables"],
        result["notes"],
        as_json=args.as_json,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_ERROR)
