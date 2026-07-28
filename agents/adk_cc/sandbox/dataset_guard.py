"""Refuse to load a dataset that will kill the sandbox (W4).

The failure this prevents is specific and ugly: `pd.read_csv("big.csv")` on a
file several hundred MB wide expands to multiples of that in RAM, the sandbox
process is OOM-killed, and what comes back is a truncated stderr or a bare
non-zero exit. The model then usually retries the same command. Nothing in that
loop tells anyone the problem was the file size.

So check BEFORE running: if the code about to execute names a data file over the
cap, don't run it — return a refusal that says which file, how big, and the two
or three ways to proceed (sample rows, select columns, chunk, or convert to
parquet). A clear refusal costs one `wc -c`; an OOM costs the turn.

Deliberately narrow, because a guard that fires on legitimate work gets disabled:

* Only python-invoking code — a `cp big.csv backup.csv` is not a memory risk.
* Only files the code actually NAMES. No directory scanning, no guessing.
* Skipped when the code already limits the READ (`nrows=`, `chunksize=`,
  `usecols=`, `columns=`, `skiprows=`, `iterator=True`) — that author has
  thought about size, and second-guessing them is the false positive that makes
  people set the kill switch. `.head(50)` AFTER a full read does not count: it
  loads everything first, which is precisely the case being guarded.

`ADK_CC_DATASET_MAX_MB` (default 100) moves the line; `ADK_CC_DATASET_GUARD=0`
turns it off.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional

from ..config.schema import env_bool

# Formats whose in-memory expansion is the problem. Deliberately excludes .txt
# and .log — those are read line-wise far more often than loaded whole.
_DATA_EXT = ("csv", "tsv", "parquet", "xlsx", "xls", "json", "jsonl", "feather")

_DATA_PATH_RE = re.compile(
    r"""["'`]([^"'`\s]+\.(?:%s))["'`]""" % "|".join(_DATA_EXT), re.IGNORECASE
)

# READ-TIME limits only. `.head(50)` and `.sample()` are deliberately absent:
# `pd.read_csv(big).head(50)` loads the entire file first and then throws most
# of it away, so it is exactly the case the guard exists to catch — treating it
# as "the author thought about size" would let the OOM through.
_SAMPLING_RE = re.compile(
    r"(?i)(\bnrows\s*=|\bchunksize\s*=|\busecols\s*=|\bcolumns\s*=|"
    r"\bskiprows\s*=|\biterator\s*=\s*True|\bLIMIT\s+\d+)"
)


def enabled() -> bool:
    return env_bool("ADK_CC_DATASET_GUARD", True)


def cap_bytes() -> int:
    try:
        mb = float(os.environ.get("ADK_CC_DATASET_MAX_MB", "100"))
    except ValueError:
        mb = 100.0
    return int(mb * 1024 * 1024)


def data_paths(code: str) -> list[str]:
    """Quoted data-file paths the code names, in order, deduped."""
    seen, out = set(), []
    for m in _DATA_PATH_RE.finditer(code or ""):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def already_samples(code: str) -> bool:
    return bool(_SAMPLING_RE.search(code or ""))


def oversized(sizes: dict[str, int], cap: Optional[int] = None) -> list[tuple[str, int]]:
    cap = cap_bytes() if cap is None else cap
    return sorted(
        ((p, n) for p, n in sizes.items() if n > cap),
        key=lambda t: -t[1],
    )


def refusal(over: Iterable[tuple[str, int]], cap: Optional[int] = None) -> str:
    """The message that replaces the OOM. Names the file, the size, and the way
    forward — a refusal with no route out just gets retried verbatim."""
    cap = cap_bytes() if cap is None else cap
    lines = ["Refused before running: this would load a dataset larger than "
             f"{cap / 1024 / 1024:.0f}MB into memory and is likely to be "
             "OOM-killed in the sandbox."]
    for path, n in over:
        lines.append(f"  {path} — {n / 1024 / 1024:.1f}MB")
    lines.append(
        "Do one of these instead, then re-run:\n"
        "  • sample rows      pd.read_csv(p, nrows=100_000)\n"
        "  • pick columns     pd.read_csv(p, usecols=[...])  /  "
        "pd.read_parquet(p, columns=[...])\n"
        "  • stream chunks    for chunk in pd.read_csv(p, chunksize=500_000): ...\n"
        "  • shrink first     convert to parquet, or filter with duckdb/awk, "
        "then load the result\n"
        "Inspect the shape without loading it: "
        "`wc -l <file>` and `head -3 <file>`.\n"
        "Raise the limit with ADK_CC_DATASET_MAX_MB if the box really has the RAM."
    )
    return "\n".join(lines)


def size_probe(paths: Iterable[str]) -> str:
    """A shell one-liner returning `<bytes> <path>` per existing file.

    One round trip for every candidate — the guard must not cost a call per
    file, or it becomes the very overhead it is trying to save.
    """
    quoted = " ".join("'" + p.replace("'", "'\\''") + "'" for p in paths)
    return (
        f"for f in {quoted}; do "
        f'if [ -f "$f" ]; then printf "%s %s\\n" "$(wc -c < "$f" | tr -d " ")" "$f"; fi; '
        f"done"
    )


def parse_sizes(stdout: str) -> dict[str, int]:
    """Parse `size_probe` output. Unparseable lines are ignored, never guessed."""
    out: dict[str, int] = {}
    for line in (stdout or "").splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) != 2:
            continue
        try:
            out[parts[1]] = int(parts[0])
        except ValueError:
            continue
    return out
