#!/usr/bin/env python3
"""ASSERTS: no feature is redundant with the others — every VIF is below the
severe threshold and no numeric/categorical pair is deterministically bound.
PASS means OLS coefficients and correlation rankings on this data can be read
at face value. FAIL means at least one redundant cluster exists: β values will
flip sign across re-runs and "top driver" rankings are arbitrary within the
cluster. Resolve with cluster_representative.py before reporting drivers.

Requires: pandas, numpy, scipy  (tier-1)

Usage:
    python collinearity_probe.py data.csv --target SalePrice
    python collinearity_probe.py data.parquet --numeric-only --max-vif 5
    python collinearity_probe.py data.csv --target y --json
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from _probe_utils import (
    EXIT_ERROR,
    base_parser,
    find_degenerate,
    load_for_probe,
    prepare_matrix,
    report,
    split_columns,
)


def vif_table(X: pd.DataFrame) -> pd.DataFrame:
    """VIF_j = 1 / (1 - R²_j), where R²_j regresses column j on all the others.

    Pairwise ρ cannot find this: a feature can correlate weakly with every
    other column individually and still be an exact linear combination of
    three of them. That is the case VIF exists to catch.
    """
    Xv = X.to_numpy(dtype=np.float64)
    rows = []
    for j, col in enumerate(X.columns):
        y = Xv[:, j]
        Xrest = np.delete(Xv, j, axis=1)
        Xd = np.column_stack([np.ones(len(Xrest)), Xrest])
        beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        ss_tot = ((y - y.mean()) ** 2).sum()
        if ss_tot == 0:
            r2, vif = 1.0, float("inf")
        else:
            r2 = 1 - ((y - Xd @ beta) ** 2).sum() / ss_tot
            vif = float("inf") if r2 >= 0.9999 else 1 / (1 - r2)
        rows.append(
            {
                "feature": col,
                "R2_on_others": round(float(r2), 3),
                "VIF": round(float(vif), 2),
            }
        )
    return (
        pd.DataFrame(rows)
        .sort_values("VIF", ascending=False)
        .reset_index(drop=True)
    )


def mixed_type_vif(
    X: pd.DataFrame,
    num_cols: "list[str]",
    cat_cols: "list[str]",
    max_cardinality: int = 15,
) -> "tuple[pd.DataFrame, pd.DataFrame, list[str]]":
    """VIF on the full design matrix (numeric + one-hot dummies).

    drop_first=True is required: without it the dummies of one categorical
    sum to 1 and are collinear by construction, which would report every
    categorical as severe regardless of the data.

    High-cardinality categoricals bloat the design matrix and produce
    per-dummy VIFs nobody can act on — they are routed to cross-type
    binding instead.

    Returns (per_source, per_feature, notes).
    """
    notes: "list[str]" = []
    cat_low = [c for c in cat_cols if X[c].nunique(dropna=True) <= max_cardinality]
    cat_skip = [c for c in cat_cols if c not in cat_low]
    if cat_skip:
        notes.append(
            f"{len(cat_skip)} high-cardinality categorical(s) skipped from the "
            f"design matrix (covered by cross-type binding): {cat_skip[:5]}"
            f"{'…' if len(cat_skip) > 5 else ''}"
        )

    design_src = X[num_cols + cat_low]
    X_design = pd.get_dummies(
        design_src, columns=cat_low, drop_first=True, dtype="float64"
    )
    X_design, prep_notes = prepare_matrix(X_design, list(X_design.columns))
    notes.extend(prep_notes)
    if X_design.shape[1] < 2:
        return pd.DataFrame(), pd.DataFrame(), notes

    per_feature = vif_table(X_design)

    source_map = {c: c for c in num_cols}
    for src in cat_low:
        for c in X_design.columns:
            if c.startswith(f"{src}_"):
                source_map[c] = src
    per_feature["source"] = per_feature["feature"].map(source_map).fillna("(unmapped)")

    per_source = (
        per_feature.groupby("source")
        .agg(
            max_VIF=("VIF", "max"),
            mean_VIF=("VIF", "mean"),
            n_features=("feature", "count"),
        )
        .sort_values("max_VIF", ascending=False)
        .reset_index()
        .round(2)
    )
    return per_source, per_feature, notes


def cross_type_binding(
    X: pd.DataFrame,
    num_cols: "list[str]",
    cat_cols: "list[str]",
    threshold: float = 0.5,
) -> pd.DataFrame:
    """Pairwise association strength, including across dtypes.

    num↔cat uses η² (share of the numeric's variance explained by the
    category means), cat↔cat uses Cramér's V, num↔num uses ρ². All three
    land on [0, 1] so one threshold reads consistently across the table.

    η² > 0.5 means the numeric carries nothing beyond the group means;
    η² > 0.9 means the two encode the same fact (a magnitude column that is
    zero exactly when its categorical says "None").
    """
    from _probe_utils import cramers_v

    rows = []

    # numeric ↔ categorical: η², computed one categorical at a time so the
    # whole numeric block is handled in a single grouped pass.
    for c in cat_cols:
        sub = X.loc[X[c].notna()]
        if len(sub) < 5 or sub[c].nunique() < 2:
            continue
        nums = sub[num_cols].apply(pd.to_numeric, errors="coerce")
        grand = nums.mean()
        ss_tot = ((nums - grand) ** 2).sum()
        grouped = nums.groupby(sub[c], observed=True)
        ss_btw = ((grouped.mean() - grand) ** 2).mul(grouped.size(), axis=0).sum()
        eta2 = (ss_btw / ss_tot.replace(0, np.nan)).dropna()
        for n_col, score in eta2[eta2 > threshold].items():
            rows.append(
                {
                    "a": n_col,
                    "b": c,
                    "score": round(float(score), 3),
                    "kind": "eta2 (num-cat)",
                }
            )

    # categorical ↔ categorical: Cramér's V
    for i, a in enumerate(cat_cols):
        for b in cat_cols[i + 1 :]:
            v = cramers_v(X[a], X[b])
            if v > threshold:
                rows.append(
                    {"a": a, "b": b, "score": round(v, 3), "kind": "CramersV (cat-cat)"}
                )

    # numeric ↔ numeric: ρ². One correlation matrix beats O(p²) pairwise calls.
    if len(num_cols) >= 2:
        rho = X[num_cols].corr(method="spearman")
        rho2 = (rho**2).to_numpy()
        iu = np.triu_indices_from(rho2, k=1)
        for i, j in zip(*iu):
            score = rho2[i, j]
            if np.isfinite(score) and score > threshold:
                rows.append(
                    {
                        "a": num_cols[i],
                        "b": num_cols[j],
                        "score": round(float(score), 3),
                        "kind": "rho2 (num-num)",
                    }
                )

    if not rows:
        return pd.DataFrame(columns=["a", "b", "score", "kind"])
    return (
        pd.DataFrame(rows)
        .sort_values("score", ascending=False)
        .reset_index(drop=True)
    )


def run(
    df: pd.DataFrame,
    target: "str | None" = None,
    exclude: "tuple[str, ...]" = (),
    max_vif: float = 10.0,
    binding_threshold: float = 0.5,
    binding_fail: float = 0.7,
    max_cardinality: int = 15,
    max_features: int = 150,
    numeric_only: bool = False,
) -> dict:
    """Run the three-layer collinearity audit. Returns a result payload."""
    num_cols, cat_cols = split_columns(df, target, tuple(exclude))
    if numeric_only:
        cat_cols = []

    tables: "dict[str, pd.DataFrame]" = {}
    notes: "list[str]" = []
    failures: "list[str]" = []

    degenerate = find_degenerate(df, num_cols + cat_cols)
    if len(degenerate):
        tables["Degenerate columns"] = degenerate
        failures.append(f"{len(degenerate)} constant/all-null column(s)")
        bad = set(degenerate["feature"])
        num_cols = [c for c in num_cols if c not in bad]
        cat_cols = [c for c in cat_cols if c not in bad]

    if len(num_cols) < 2:
        notes.append("fewer than 2 usable numeric columns — VIF not computed")
    elif len(num_cols) > max_features:
        return {
            "check": "collinearity",
            "passed": False,
            "summary": (
                f"{len(num_cols)} numeric columns exceeds --max-features "
                f"({max_features}); per-column VIF on a matrix this wide is "
                "neither fast nor interpretable — pre-select features first"
            ),
            "tables": tables,
            "notes": notes,
        }
    else:
        X, prep_notes = prepare_matrix(df, num_cols)
        notes.extend(prep_notes)
        n_rows, n_feat = X.shape
        if n_rows < n_feat + 2:
            # Regressing one column on the other p-1 plus an intercept needs
            # more rows than parameters. Below that every R² is exactly 1 and
            # every VIF reads infinite — an arithmetic artefact, not evidence
            # of redundancy. Reporting it as collinearity sends people hunting
            # for a duplicate that isn't there.
            return {
                "check": "collinearity",
                "passed": False,
                "summary": (
                    f"{n_rows:,} usable rows for {n_feat} features — VIF is "
                    "undefined below p+2 rows (the design matrix is "
                    "rank-deficient, so every VIF would read as infinite "
                    "regardless of the data). Reduce features or add rows."
                ),
                "tables": tables,
                "notes": notes,
            }
        if n_rows < 10 * n_feat:
            notes.append(
                f"n/p = {n_rows / n_feat:.1f} — VIF estimates are unstable "
                "below ~10; treat borderline values as noise"
            )
        vif = vif_table(X)
        tables["Numeric VIF"] = vif
        severe = vif.loc[vif["VIF"] > max_vif, "feature"].tolist()
        moderate = vif.loc[
            (vif["VIF"] > 5) & (vif["VIF"] <= max_vif), "feature"
        ].tolist()
        if severe:
            failures.append(f"{len(severe)} feature(s) with VIF > {max_vif:g}")
            notes.append(
                f"severe: {severe[:8]}{'…' if len(severe) > 8 else ''} — drop one "
                "per redundant cluster (cluster_representative.py) or switch to "
                "RidgeCV/LassoCV"
            )
        elif moderate:
            notes.append(
                f"moderate (5 < VIF <= {max_vif:g}): {moderate[:8]} — prefer "
                "SHAP/permutation rankings over OLS β if you model later"
            )

    if cat_cols and num_cols:
        per_source, _per_feature, mv_notes = mixed_type_vif(
            df, num_cols, cat_cols, max_cardinality
        )
        notes.extend(mv_notes)
        if len(per_source):
            tables["Design-matrix VIF by source"] = per_source
            src_severe = per_source.loc[
                per_source["max_VIF"] > max_vif, "source"
            ].tolist()
            if src_severe:
                failures.append(
                    f"{len(src_severe)} source(s) with design-matrix VIF > {max_vif:g}"
                )

    if cat_cols or len(num_cols) >= 2:
        bindings = cross_type_binding(df, num_cols, cat_cols, binding_threshold)
        if len(bindings):
            tables["Cross-type bindings"] = bindings
            hard = bindings[bindings["score"] > binding_fail]
            if len(hard):
                failures.append(f"{len(hard)} binding(s) above {binding_fail:g}")
                notes.append(
                    "a binding above 0.9 means the pair encodes the same fact — "
                    "keep one side, not both"
                )

    passed = not failures
    summary = (
        "no redundant features detected"
        if passed
        else "; ".join(failures)
    )
    return {
        "check": "collinearity",
        "passed": passed,
        "summary": summary,
        "tables": tables,
        "notes": notes,
    }


def main(argv: "list[str] | None" = None) -> int:
    p = base_parser(
        "Three-layer collinearity audit: numeric VIF, design-matrix VIF by "
        "source, and cross-type binding (eta2 / Cramer's V / rho2).",
        epilog=__doc__,
    )
    p.add_argument("--max-vif", type=float, default=10.0, help="severe VIF threshold (default 10)")
    p.add_argument(
        "--binding-threshold", type=float, default=0.5, help="report bindings above this (default 0.5)"
    )
    p.add_argument(
        "--binding-fail", type=float, default=0.7, help="fail on bindings above this (default 0.7)"
    )
    p.add_argument(
        "--max-cardinality", type=int, default=15, help="max categorical levels for one-hot VIF (default 15)"
    )
    p.add_argument(
        "--max-features", type=int, default=150, help="refuse per-column VIF above this width (default 150)"
    )
    p.add_argument("--numeric-only", action="store_true", help="skip the categorical layers")
    args = p.parse_args(argv)

    df = load_for_probe(args)
    result = run(
        df,
        target=args.target,
        exclude=tuple(args.exclude),
        max_vif=args.max_vif,
        binding_threshold=args.binding_threshold,
        binding_fail=args.binding_fail,
        max_cardinality=args.max_cardinality,
        max_features=args.max_features,
        numeric_only=args.numeric_only,
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
