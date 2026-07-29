#!/usr/bin/env python3
"""ASSERTS: all four Pre-Modeling Diagnostics pass, so modeling can proceed
without a silent-failure class already baked in. PASS means collinearity, null
handling, target shape, and leakage were all checked and none blocks the
default path. FAIL means at least one did — the per-check output says which.

This is the single entry point for CI and for an agent that wants one verdict
before it starts modeling. Each check also runs standalone; see scripts/README.md.

  1. collinearity  -> collinearity_probe.py
  2. null handling -> null_audit_probe.py
  3. target skew   -> inline (three lines of logic, no probe of its own)
  4. leakage       -> leakage_probe.py

Requires: pandas, numpy, scipy  (tier-1)

Usage:
    python premodel_audit.py data.csv --target SalePrice
    python premodel_audit.py data.csv --target y --json
    python premodel_audit.py data.csv            # skips target-dependent checks
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

import collinearity_probe
import leakage_probe
import null_audit_probe
from _probe_utils import EXIT_ERROR, EXIT_FAIL, EXIT_PASS, base_parser, load_for_probe


def target_skew_check(y: "pd.Series | None") -> dict:
    """Diagnostic 3 — a heavily right-skewed positive target wants log1p.

    This never blocks: an untransformed skewed target still fits, it just
    fits worse and reports an R2 that flatters the tail. So it returns a
    recommendation rather than a failure, and the caller decides.
    """
    if y is None or not pd.api.types.is_numeric_dtype(y):
        return {"check": "target-skew", "passed": True, "summary": "no numeric target — skipped", "notes": []}
    yc = pd.Series(y).dropna()
    if yc.empty:
        return {"check": "target-skew", "passed": True, "summary": "target is all null", "notes": []}
    if yc.nunique() <= 2:
        return {"check": "target-skew", "passed": True, "summary": "binary target — skew not applicable", "notes": []}
    skew = float(yc.skew())
    positive = bool((yc > 0).all())
    notes = []
    if skew > 1 and positive:
        notes.append(
            "fit on np.log1p(y), report both raw and transformed R2, and "
            "translate log-space MAE back to the original units for stakeholders"
        )
        summary = f"skew={skew:.2f} and strictly positive — recommend log1p transform"
    elif skew > 1:
        notes.append("skewed but not strictly positive — log1p unavailable; consider a signed transform")
        summary = f"skew={skew:.2f}, non-positive values present"
    else:
        summary = f"skew={skew:.2f} — no transform needed"
    return {"check": "target-skew", "passed": True, "summary": summary, "notes": notes}


def main(argv: "list[str] | None" = None) -> int:
    p = base_parser(
        "Run all four Pre-Modeling Diagnostics and return one verdict.",
        epilog=__doc__,
    )
    p.add_argument("--max-vif", type=float, default=10.0)
    p.add_argument("--assoc-threshold", type=float, default=None)
    p.add_argument("--leak-rho", type=float, default=0.95)
    p.add_argument("--numeric-only", action="store_true", help="skip the categorical collinearity layers")
    args = p.parse_args(argv)

    df = load_for_probe(args)
    y = df[args.target] if args.target and args.target in df.columns else None
    if args.target and y is None:
        print(f"error: target column '{args.target}' not found", file=sys.stderr)
        return EXIT_ERROR

    results = []
    results.append(
        collinearity_probe.run(
            df,
            target=args.target,
            exclude=tuple(args.exclude),
            max_vif=args.max_vif,
            numeric_only=args.numeric_only,
        )
    )
    if args.target:
        results.append(
            null_audit_probe.run(
                df,
                target=args.target,
                exclude=tuple(args.exclude),
                assoc_threshold=args.assoc_threshold,
            )
        )
    else:
        results.append(
            {
                "check": "null-audit",
                "passed": True,
                "summary": "no --target given — missingness/target association not testable",
                "tables": {},
                "notes": ["pass --target to check for informative missingness"],
            }
        )
    results.append(target_skew_check(y))
    if args.target:
        results.append(
            leakage_probe.run(
                df,
                target=args.target,
                exclude=tuple(args.exclude),
                leak_rho=args.leak_rho,
            )
        )
    else:
        results.append(
            {
                "check": "leakage",
                "passed": True,
                "summary": "no --target given — leakage not testable",
                "tables": {},
                "notes": ["pass --target to check for target leakage"],
            }
        )

    failed = [r for r in results if not r["passed"]]
    overall = not failed

    if args.as_json:
        print(
            json.dumps(
                {
                    "check": "premodel-audit",
                    "status": "pass" if overall else "fail",
                    "rows": int(len(df)),
                    "columns": int(df.shape[1]),
                    "target": args.target,
                    "failed_checks": [r["check"] for r in failed],
                    "checks": [
                        {
                            "check": r["check"],
                            "status": "pass" if r["passed"] else "fail",
                            "summary": r["summary"],
                            "notes": list(r.get("notes", [])),
                            "tables": {
                                k: v.head(25).to_dict("records")
                                for k, v in (r.get("tables") or {}).items()
                                if v is not None and len(v)
                            },
                        }
                        for r in results
                    ],
                },
                indent=2,
                default=str,
            )
        )
        return EXIT_PASS if overall else EXIT_FAIL

    print(f"Pre-Modeling Audit — {len(df):,} rows x {df.shape[1]} columns", end="")
    print(f", target='{args.target}'" if args.target else " (no target)")
    print("=" * 72)
    for r in results:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['check']} — {r['summary']}")
        for name, tbl in (r.get("tables") or {}).items():
            if tbl is None or not len(tbl):
                continue
            print(f"\n  {name}  ({len(tbl)} rows):")
            print("\n".join("    " + ln for ln in tbl.head(15).to_string(index=False).splitlines()))
            if len(tbl) > 15:
                print(f"    … {len(tbl) - 15} more")
        for note in r.get("notes", []):
            print(f"  → {note}")
        print()
    print("=" * 72)
    if overall:
        print("[PASS] premodel-audit — all four diagnostics clear; modeling can proceed")
    else:
        print(
            f"[FAIL] premodel-audit — {len(failed)} of {len(results)} checks need "
            f"action: {', '.join(r['check'] for r in failed)}"
        )
    return EXIT_PASS if overall else EXIT_FAIL


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_ERROR)
