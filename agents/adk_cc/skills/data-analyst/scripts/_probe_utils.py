"""Shared plumbing for the data-analyst diagnostic probes.

Not a probe itself. Provides table loading, column typing, degenerate-column
detection, and result formatting so each probe file stays one focused check.

Requires: pandas, numpy  (tier-1 — no scikit-learn / xgboost / statsmodels)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Exit codes are the probe contract: 0 = check passed, 1 = check failed
# (a real finding the caller must act on), 2 = the probe could not run at
# all (bad path, unreadable file, missing target). CI and agents need to
# tell "your data has a problem" apart from "I couldn't look".
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

SUMMARY_PATTERNS = ("overall", "total", "index", "score", "rating", "summary")


def die(message: str) -> "None":
    """Abort with EXIT_ERROR — the probe could not run, which is not a finding."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(EXIT_ERROR)


def load_table(path: str) -> pd.DataFrame:
    """Load a tabular file, dispatching on extension.

    Parquet needs pyarrow and Excel needs openpyxl; both are in the common
    dependency set, but we surface the ImportError as a usage error rather
    than a traceback so the caller knows which extra to install.
    """
    p = Path(path)
    if not p.exists():
        die(f"file not found: {p}")
    suffix = p.suffix.lower()
    try:
        if suffix in (".csv", ".txt", ".data"):
            df = pd.read_csv(p)
        elif suffix == ".tsv":
            df = pd.read_csv(p, sep="\t")
        elif suffix in (".parquet", ".pq"):
            df = pd.read_parquet(p)
        elif suffix in (".xlsx", ".xls"):
            df = pd.read_excel(p)
        elif suffix == ".jsonl":
            df = pd.read_json(p, lines=True)
        elif suffix == ".json":
            df = pd.read_json(p)
        else:
            die(f"unsupported file type '{suffix}' (csv/tsv/parquet/xlsx/json)")
    except ImportError as exc:
        die(f"missing reader dependency for {suffix}: {exc}")
    except Exception as exc:  # noqa: BLE001 — any read failure is a usage error
        die(f"could not read {p}: {exc}")
    if df.empty:
        die(f"{p} loaded but has zero rows")
    return df


def resolve_target(df: pd.DataFrame, target: "str | None") -> "pd.Series | None":
    """Return the target column, or None when the probe was given no target."""
    if target is None:
        return None
    if target not in df.columns:
        die(f"target column '{target}' not in {list(df.columns)[:12]}…")
    return df[target]


def split_columns(
    df: pd.DataFrame,
    target: "str | None" = None,
    exclude: "tuple[str, ...]" = (),
) -> "tuple[list[str], list[str]]":
    """Partition feature columns into (numeric, categorical).

    Booleans count as categorical — they carry group membership, not
    magnitude, so η²/Cramér's V describe them better than VIF does.
    Datetimes are dropped: a raw timestamp has no meaningful VIF, and
    derived date parts should be engineered explicitly before auditing.
    """
    drop = set(exclude) | ({target} if target else set())
    numeric, categorical = [], []
    for col in df.columns:
        if col in drop:
            continue
        s = df[col]
        if pd.api.types.is_bool_dtype(s):
            categorical.append(col)
        elif pd.api.types.is_numeric_dtype(s):
            numeric.append(col)
        elif pd.api.types.is_datetime64_any_dtype(s):
            continue
        else:
            categorical.append(col)
    return numeric, categorical


def find_degenerate(df: pd.DataFrame, cols: "list[str]") -> pd.DataFrame:
    """Columns that break VIF before collinearity is even in question.

    An all-null or constant column is perfectly collinear with the intercept,
    so its VIF is infinite for a reason that has nothing to do with the other
    features. Separating them keeps the collinearity report honest.
    """
    rows = []
    for col in cols:
        s = df[col]
        if s.isna().all():
            rows.append({"feature": col, "issue": "all null"})
        elif s.nunique(dropna=True) <= 1:
            rows.append({"feature": col, "issue": "constant"})
    return pd.DataFrame(rows, columns=["feature", "issue"])


def prepare_matrix(
    df: pd.DataFrame, cols: "list[str]", min_rows: int = 30
) -> "tuple[pd.DataFrame, list[str]]":
    """Return a NaN-free numeric matrix plus any notes about how we got there.

    Listwise deletion is the default because VIF is defined on complete
    cases. On wide data that can wipe out every row, so we fall back to
    median imputation and say so — a silently imputed VIF and a listwise
    VIF can differ enough to change the verdict.
    """
    notes: "list[str]" = []
    X = df[cols].apply(pd.to_numeric, errors="coerce")
    complete = X.dropna()
    needed = max(min_rows, len(cols) + 2)
    if len(complete) >= needed:
        if len(complete) < len(X):
            notes.append(
                f"listwise deletion kept {len(complete):,}/{len(X):,} rows"
            )
        return complete, notes
    notes.append(
        f"listwise deletion left {len(complete):,} rows (< {needed} needed) — "
        "median-imputed instead; VIF here is an approximation"
    )
    return X.fillna(X.median(numeric_only=True)).dropna(axis=1, how="all"), notes


def assoc_threshold_for(n: int, explicit: "float | None" = None) -> "tuple[float, str]":
    """Practical-significance threshold for a missingness/target association.

    0.05 is calibrated for n ≈ 1k–100k. At n = 1M an |assoc| of 0.05 is
    overwhelmingly significant but practically nil; at n < 200 the estimate
    is too noisy to trust at 0.05. Scaling by n keeps the threshold about
    "does this matter" rather than "is this detectable".
    """
    if explicit is not None:
        return explicit, f"threshold {explicit} (explicit)"
    if n < 200:
        return 0.20, f"threshold 0.20 (n={n:,} — small sample, noisy estimate)"
    if n > 500_000:
        return 0.10, f"threshold 0.10 (n={n:,} — large sample, tiny effects detectable)"
    return 0.05, f"threshold 0.05 (n={n:,})"


def base_parser(description: str, epilog: str = "") -> argparse.ArgumentParser:
    """Argument parser shared by every probe."""
    p = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("data", help="path to CSV / TSV / Parquet / Excel / JSON")
    p.add_argument("--target", default=None, help="target column (excluded from features)")
    p.add_argument("--exclude", nargs="*", default=[], help="feature columns to skip")
    p.add_argument(
        "--sample",
        type=int,
        default=None,
        help="analyse a random N-row sample (seeded, for speed on large files)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON")
    return p


def load_for_probe(args: argparse.Namespace) -> pd.DataFrame:
    """Load and optionally subsample, honouring the shared CLI flags."""
    df = load_table(args.data)
    if args.sample and args.sample < len(df):
        df = df.sample(n=args.sample, random_state=0)
    return df


def report(
    check: str,
    passed: bool,
    summary: str,
    tables: "dict[str, pd.DataFrame] | None" = None,
    notes: "tuple[str, ...] | list[str]" = (),
    as_json: bool = False,
    max_rows: int = 25,
) -> int:
    """Print the result and return the exit code the caller should use.

    The one-line verdict comes first in both modes: an agent reading this
    mid-analysis, or a human reading CI output, should not have to parse a
    table to learn whether the check passed.
    """
    tables = tables or {}
    if as_json:
        payload = {
            "check": check,
            "status": "pass" if passed else "fail",
            "summary": summary,
            "notes": list(notes),
            "tables": {
                name: tbl.head(max_rows).to_dict("records")
                for name, tbl in tables.items()
                if tbl is not None and len(tbl)
            },
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"[{'PASS' if passed else 'FAIL'}] {check} — {summary}")
        for name, tbl in tables.items():
            if tbl is None or not len(tbl):
                continue
            print(f"\n{name}  ({len(tbl)} row{'s' if len(tbl) != 1 else ''}):")
            print(tbl.head(max_rows).to_string(index=False))
            if len(tbl) > max_rows:
                print(f"  … {len(tbl) - max_rows} more")
        for note in notes:
            print(f"\n  → {note}")
    return EXIT_PASS if passed else EXIT_FAIL


MIN_ASSOC_N = 30


def cramers_v(a: pd.Series, b: pd.Series, min_n: int = MIN_ASSOC_N) -> float:
    """Cramér's V between two *categorical* series. 0 = independent, 1 = deterministic.

    Returns NaN rather than a number when there is too little overlap to
    estimate. V saturates at 1 on sparse contingency tables — a column that
    is 99% null can show V = 1.0 off a dozen rows — so a floor on n is part
    of the measure, not an optional guard.

    Only valid when both sides are genuinely categorical. Do not rank a
    continuous variable to force it through here: with ~n distinct levels
    every table is near-diagonal and V goes to 1 for reasons that have
    nothing to do with association. Use correlation_ratio() instead.
    """
    from scipy import stats

    d = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(d) < min_n or d["a"].nunique() < 2 or d["b"].nunique() < 2:
        return float("nan")
    ct = pd.crosstab(d["a"], d["b"])
    chi2, *_ = stats.chi2_contingency(ct)
    n = ct.to_numpy().sum()
    denom = n * max(min(ct.shape) - 1, 1)
    return float(np.sqrt(chi2 / denom)) if denom > 0 else float("nan")


def correlation_ratio(
    categories: pd.Series, values: pd.Series, min_n: int = MIN_ASSOC_N
) -> float:
    """sqrt(η²) — how much of a numeric's variance the group means explain.

    This is the right measure for a categorical/numeric pair, and it lands on
    [0, 1] so it reads on the same scale as |ρ| and Cramér's V. Returns NaN
    when the overlap is too small to estimate.
    """
    d = pd.DataFrame(
        {"g": categories, "x": pd.to_numeric(values, errors="coerce")}
    ).dropna()
    if len(d) < min_n or d["g"].nunique() < 2:
        return float("nan")
    grand = d["x"].mean()
    ss_tot = float(((d["x"] - grand) ** 2).sum())
    if ss_tot == 0:
        return float("nan")
    grouped = d.groupby("g", observed=True)["x"]
    ss_btw = float((grouped.mean().sub(grand).pow(2) * grouped.size()).sum())
    return float(np.sqrt(max(ss_btw / ss_tot, 0.0)))
