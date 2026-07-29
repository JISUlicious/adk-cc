#!/usr/bin/env python3
"""ASSERTS: no feature is a restatement of the target. PASS means an importance
ranking on this data describes real drivers. FAIL means at least one feature
either leaks the target (it is computed from or after the outcome) or is a
summary rollup shadowing its own components — in both cases the model's "top
driver" is a tautology, and the R2 is measuring an identity, not a
relationship.

Two checks:
  leak    — |rho(feature, target)| >= --leak-rho, or an exact duplicate of the
            target. A survey composite predicting its own sub-scale looks like
            a brilliant model and teaches you nothing.
  rollup  — a summary-named feature (overall/total/index/score/rating/summary)
            ranks top-3 while its plausible components sit just behind. Refit
            without it: if R2 barely moves, report the cluster, not the rollup.

Requires: pandas, numpy, scipy  (tier-1)

Usage:
    python leakage_probe.py data.csv --target SalePrice
    python leakage_probe.py survey.csv --target stress_level --leak-rho 0.90
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from _probe_utils import (
    EXIT_ERROR,
    SUMMARY_PATTERNS,
    base_parser,
    die,
    load_for_probe,
    report,
    split_columns,
)


def target_associations(
    df: pd.DataFrame, features: "list[str]", y: pd.Series
) -> "tuple[pd.DataFrame, list[str]]":
    """|association| of each feature with the target, on a common [0, 1] scale.

    The measure follows the dtype pair, because forcing one measure onto all
    four combinations is how false leakage findings get manufactured:

      numeric  x numeric  -> |Spearman rho|   (monotone, survives skew)
      category x numeric  -> sqrt(eta^2)      (variance explained by groups)
      numeric  x category -> sqrt(eta^2)      (same, roles swapped)
      category x category -> Cramer's V

    Columns with too little overlap to estimate are dropped and reported
    separately rather than scored — a 99%-null column can otherwise post a
    perfect association off a dozen rows and lead the whole report astray.

    Returns (scored table, names of unscoreable columns).
    """
    from _probe_utils import correlation_ratio, cramers_v

    def is_num(s: pd.Series) -> bool:
        return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)

    y_num = is_num(y)
    rows, unscoreable = [], []
    for col in features:
        s = df[col]
        try:
            if is_num(s) and y_num:
                v = s.corr(y, method="spearman")
            elif is_num(s):
                v = correlation_ratio(y, s)
            elif y_num:
                v = correlation_ratio(s, y)
            else:
                v = cramers_v(s, y)
        except Exception:  # noqa: BLE001 — unscoreable column is not evidence of leakage
            v = np.nan
        if pd.isna(v):
            unscoreable.append(col)
        else:
            rows.append({"feature": col, "assoc": round(abs(float(v)), 4)})
    table = (
        pd.DataFrame(rows, columns=["feature", "assoc"])
        .sort_values("assoc", ascending=False)
        .reset_index(drop=True)
    )
    return table, unscoreable


def run(
    df: pd.DataFrame,
    target: str,
    exclude: "tuple[str, ...]" = (),
    leak_rho: float = 0.95,
    rollup_margin: float = 0.30,
) -> dict:
    """Check for target leakage and rating-summary tautology."""
    if target not in df.columns:
        die(f"target column '{target}' not in {list(df.columns)[:12]}…")

    y = df[target]
    num_cols, cat_cols = split_columns(df, target, tuple(exclude))
    features = num_cols + cat_cols
    if not features:
        return {
            "check": "leakage",
            "passed": True,
            "summary": "no feature columns to check",
            "tables": {},
            "notes": [],
        }

    assoc, unscoreable = target_associations(df, features, y)
    tables: "dict[str, pd.DataFrame]" = {}
    notes: "list[str]" = []
    failures: "list[str]" = []
    if unscoreable:
        notes.append(
            f"{len(unscoreable)} column(s) had too few complete rows to score "
            f"(not evidence either way): {unscoreable[:6]}"
            f"{'…' if len(unscoreable) > 6 else ''}"
        )

    duplicates = [
        c for c in features if df[c].equals(y) or (df[c].astype(str).equals(y.astype(str)))
    ]

    leaks = assoc[assoc["assoc"] >= leak_rho].copy()
    if duplicates:
        leaks = pd.concat(
            [leaks, pd.DataFrame({"feature": duplicates, "assoc": 1.0})]
        ).drop_duplicates(subset=["feature"])
    if len(leaks):
        leaks["reason"] = np.where(
            leaks["feature"].isin(duplicates),
            "exact copy of target",
            f"|assoc| >= {leak_rho:g}",
        )
        tables["Suspected target leakage"] = leaks.reset_index(drop=True)
        failures.append(f"{len(leaks)} feature(s) restate the target")
        notes.append(
            "drop anything computed from or after the outcome before fitting — "
            "a leaked feature inflates R2 and starves every real driver of "
            "attributed importance"
        )

    top = assoc.head(3)["feature"].tolist()
    summary_feats = [
        c for c in top if any(p in c.lower() for p in SUMMARY_PATTERNS)
    ]
    rollups = []
    for s in summary_feats:
        s_score = float(assoc.loc[assoc["feature"] == s, "assoc"].iloc[0])
        behind = assoc[
            (assoc["feature"] != s)
            & (assoc["assoc"] >= s_score * (1 - rollup_margin))
            & (~assoc["feature"].isin(summary_feats))
        ]
        if len(behind) >= 2:
            rollups.append(
                {
                    "summary_feature": s,
                    "assoc": round(s_score, 4),
                    "components_behind": ", ".join(behind["feature"].head(5)),
                    "n_behind": len(behind),
                }
            )
    if rollups:
        tables["Rating-summary tautology candidates"] = pd.DataFrame(rollups)
        failures.append(f"{len(rollups)} summary feature(s) shadow their components")
        notes.append(
            "refit without the summary column: if R2 barely drops it was a "
            "redundant rollup — report the component cluster, not the rollup"
        )

    other_summaries = [
        c
        for c in features
        if any(p in c.lower() for p in SUMMARY_PATTERNS)
        and c not in summary_feats
    ]
    if other_summaries:
        notes.append(
            f"summary-named but not top-ranked (no action needed): "
            f"{other_summaries[:6]}{'…' if len(other_summaries) > 6 else ''}"
        )

    tables["Top associations with target"] = assoc.head(10)

    passed = not failures
    return {
        "check": "leakage",
        "passed": passed,
        "summary": "no leakage or rollup tautology detected"
        if passed
        else "; ".join(failures),
        "tables": tables,
        "notes": notes,
    }


def main(argv: "list[str] | None" = None) -> int:
    p = base_parser(
        "Detect target leakage (features that restate the outcome) and "
        "rating-summary tautology (rollups shadowing their own components).",
        epilog=__doc__,
    )
    p.add_argument(
        "--leak-rho", type=float, default=0.95, help="|assoc| at or above this is leakage (default 0.95)"
    )
    p.add_argument(
        "--rollup-margin",
        type=float,
        default=0.30,
        help="components within this fraction of the summary count as 'just behind' (default 0.30)",
    )
    args = p.parse_args(argv)
    if not args.target:
        die("--target is required: leakage is defined relative to the target")

    df = load_for_probe(args)
    result = run(
        df,
        target=args.target,
        exclude=tuple(args.exclude),
        leak_rho=args.leak_rho,
        rollup_margin=args.rollup_margin,
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
