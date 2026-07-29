#!/usr/bin/env python3
"""ASSERTS: every multicollinear cluster resolves to one defensible
representative. PASS means each cluster has a clear keep/drop split you can
apply mechanically. FAIL means at least one cluster is a genuine tie — and
picking arbitrarily there is the most common silent error in driver analysis,
because the feature you keep decides what story the analysis tells.

Priority order: (1) drop an aggregate that equals the sum of its components,
(2) drop summary-named features when their components are present, (3) rank by
association with the target, (4) tiebreak on completeness then dynamic range,
(5) refuse to decide and flag for a human.

Requires: pandas, numpy, scipy  (tier-1)

Usage:
    python cluster_representative.py data.csv --target SalePrice
    python cluster_representative.py data.csv --context eda --rho 0.90
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from _probe_utils import (
    EXIT_ERROR,
    SUMMARY_PATTERNS,
    base_parser,
    find_degenerate,
    load_for_probe,
    report,
    split_columns,
)


def find_clusters(X: pd.DataFrame, rho_threshold: float = 0.85) -> "list[list[str]]":
    """Group features by hierarchical clustering on |Spearman ρ|.

    Average linkage on 1-|ρ| means a cluster is a set of features that are
    mutually redundant, not just a chain of pairwise-correlated neighbours.
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    if X.shape[1] < 2:
        return []
    corr = X.corr(method="spearman").abs().fillna(0.0)
    dist = 1.0 - corr.to_numpy()
    np.fill_diagonal(dist, 0.0)
    dist = np.clip((dist + dist.T) / 2, 0, None)  # enforce exact symmetry
    Z = linkage(squareform(dist, checks=False), method="average")
    labels = fcluster(Z, t=1 - rho_threshold, criterion="distance")
    clusters = [list(corr.columns[labels == lbl]) for lbl in sorted(set(labels))]
    return [c for c in clusters if len(c) > 1]


def find_aggregate(
    X: pd.DataFrame, cluster_cols: "list[str]", threshold: float = 0.99
) -> "str | None":
    """Return the cluster member best predicted by the rest (R² ≥ threshold).

    When several members qualify — the perfect-sum case `agg = a + b + c`,
    where any of the four is predictable from the other three — prefer the
    largest mean magnitude. Aggregates are sums, so they are numerically
    bigger than their parts, and dropping the sum keeps the more granular
    information.
    """
    Xc = X[cluster_cols].dropna()
    if len(Xc) < len(cluster_cols) + 1:
        return None
    candidates = []
    for col in cluster_cols:
        others = [c for c in cluster_cols if c != col]
        Xrest = Xc[others].to_numpy(dtype=np.float64)
        y = Xc[col].to_numpy(dtype=np.float64)
        Xd = np.column_stack([np.ones(len(Xrest)), Xrest])
        beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        ss_tot = ((y - y.mean()) ** 2).sum()
        if ss_tot == 0:
            continue
        r2 = 1 - ((y - Xd @ beta) ** 2).sum() / ss_tot
        if r2 >= threshold:
            candidates.append((col, abs(float(Xc[col].mean()))))
    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[1])
    return candidates[0][0]


def select_cluster_representative(
    X: pd.DataFrame,
    y: "pd.Series | None",
    cluster_cols: "list[str]",
    *,
    score_fn=None,
) -> "tuple[list[str] | None, list[str] | None, str]":
    """Returns (keep, drop, reason). keep is None when the cluster is ambiguous.

    score_fn(X, y, col) -> float, higher is better. Defaults to |Spearman ρ|
    against the target, or standard deviation when there is no target.
    """
    agg = find_aggregate(X, cluster_cols)
    if agg is not None:
        components = [c for c in cluster_cols if c != agg]
        return components, [agg], f"aggregate dropped: {agg} = sum({components})"

    summaries = [
        c for c in cluster_cols if any(p in c.lower() for p in SUMMARY_PATTERNS)
    ]
    components = [c for c in cluster_cols if c not in summaries]
    if summaries and components:
        return components, summaries, f"summary names dropped: {summaries}"

    if score_fn is None:
        if y is None:
            score_fn = lambda Xf, _y, c: float(Xf[c].std())  # noqa: E731
        else:
            score_fn = lambda Xf, yf, c: abs(  # noqa: E731
                float(Xf[c].corr(yf, method="spearman"))
            )

    raw = {}
    for c in cluster_cols:
        try:
            v = float(score_fn(X, y, c))
        except Exception:  # noqa: BLE001 — a column that cannot be scored ranks last
            v = 0.0
        raw[c] = 0.0 if not np.isfinite(v) else v
    scores = pd.Series(raw).sort_values(ascending=False)

    if len(scores) == 1:
        return [scores.index[0]], [], "single member"

    top, runner_up = scores.iloc[0], scores.iloc[1]
    if top > 0 and (top - runner_up) / top < 0.05:
        candidates = scores[scores >= runner_up * 0.95].index.tolist()
        tie = pd.Series(
            {
                c: (1 - X[c].isna().mean())
                * (X[c].std() / (abs(X[c].mean()) + 1e-9))
                for c in candidates
            }
        ).sort_values(ascending=False)
        if (
            len(tie) > 1
            and tie.iloc[0] > 0
            and (tie.iloc[0] - tie.iloc[1]) / tie.iloc[0] < 0.05
        ):
            return None, None, f"AMBIGUOUS — needs domain judgement: {candidates}"
        keep = tie.index[0]
    else:
        keep = scores.index[0]

    drop = [c for c in cluster_cols if c != keep]
    return [keep], drop, f"kept {keep} (score={scores[keep]:.3f})"


def run(
    df: pd.DataFrame,
    target: "str | None" = None,
    exclude: "tuple[str, ...]" = (),
    rho: float = 0.85,
    context: str = "importance",
) -> dict:
    """Find multicollinear clusters and resolve each to a representative."""
    num_cols, _cat = split_columns(df, target, tuple(exclude))
    degenerate = find_degenerate(df, num_cols)
    if len(degenerate):
        num_cols = [c for c in num_cols if c not in set(degenerate["feature"])]

    if len(num_cols) < 2:
        return {
            "check": "cluster-representative",
            "passed": True,
            "summary": "fewer than 2 usable numeric columns — nothing to cluster",
            "tables": {},
            "notes": [],
        }

    y = df[target] if (target and context == "importance") else None
    if context == "importance" and y is None:
        context = "eda"

    X = df[num_cols].apply(pd.to_numeric, errors="coerce")
    clusters = find_clusters(X, rho)

    notes = [
        f"clusters formed at |rho| >= {rho:g}; ranking context = {context}"
        + ("" if y is not None else " (no target — ranking by dispersion)")
    ]
    if not clusters:
        return {
            "check": "cluster-representative",
            "passed": True,
            "summary": f"no multicollinear clusters at |rho| >= {rho:g}",
            "tables": {},
            "notes": notes,
        }

    rows, ambiguous, keep_all = [], 0, set(num_cols)
    for members in clusters:
        keep, drop, reason = select_cluster_representative(X, y, members)
        if keep is None:
            ambiguous += 1
            rows.append(
                {
                    "cluster": ", ".join(members),
                    "keep": "(ambiguous)",
                    "drop": "",
                    "reason": reason,
                }
            )
            continue
        keep_all -= set(drop)
        rows.append(
            {
                "cluster": ", ".join(members),
                "keep": ", ".join(keep),
                "drop": ", ".join(drop),
                "reason": reason,
            }
        )

    tables = {"Cluster resolutions": pd.DataFrame(rows)}
    notes.append(
        f"surviving feature set ({len(keep_all)}): "
        f"{sorted(keep_all)[:12]}{'…' if len(keep_all) > 12 else ''}"
    )
    notes.append(
        "re-run collinearity_probe.py on the surviving set — a feature still "
        "above VIF 5 means the cluster was drawn too tight; lower --rho and retry"
    )

    passed = ambiguous == 0
    summary = (
        f"{len(clusters)} cluster(s) resolved; "
        f"{len(num_cols) - len(keep_all)} feature(s) to drop"
        if passed
        else f"{ambiguous} of {len(clusters)} cluster(s) ambiguous — needs a human"
    )
    return {
        "check": "cluster-representative",
        "passed": passed,
        "summary": summary,
        "tables": tables,
        "notes": notes,
    }


def main(argv: "list[str] | None" = None) -> int:
    p = base_parser(
        "Find multicollinear feature clusters and pick one representative per "
        "cluster using the documented priority order.",
        epilog=__doc__,
    )
    p.add_argument(
        "--rho", type=float, default=0.85, help="|Spearman rho| for cluster membership (default 0.85)"
    )
    p.add_argument(
        "--context",
        choices=["importance", "eda"],
        default="importance",
        help="importance ranks by |rho(target)| (needs --target); eda ranks by dispersion",
    )
    args = p.parse_args(argv)

    df = load_for_probe(args)
    result = run(
        df,
        target=args.target,
        exclude=tuple(args.exclude),
        rho=args.rho,
        context=args.context,
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
