"""Get a dataset INTO the workspace (W5 ingestion).

Until now a dataset had to already be in the project directory: the agent can
read files, but there was no way to hand it one. The gap is small and completely
blocking — every analysis skill starts with "load the data", and the answer was
"go copy it there yourself first".

Two routes in, matching how each deployment already moves files:

* **Desktop** — the user picks a local path and the server copies it in. Same
  trust model as adding a project folder or a skill from a directory: desktop is
  single-user loopback, so the server may read the path the user just chose.
* **Web** — a multipart upload lands in the tenant's workspace. The bytes never
  touch the agent host's shell.

Datasets land in `data/` under the workspace root — one visible, predictable
place, inside the boundary the sandbox already enforces. Nothing here puts data
in a prompt: the agent reads it with its normal file tools (bounded), and the
dataset guard (`sandbox/dataset_guard.py`) stops it loading something that would
OOM the sandbox.

Limits are deliberate and boring: an allowed extension list, a size cap, and a
filename that cannot escape the directory.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Iterable, Optional

_log = logging.getLogger(__name__)

# Where datasets live, relative to the workspace root.
DATA_DIR = "data"

# Formats the analysis runtime can actually open (pyarrow ships in the `core`
# tier). `.txt`/`.log` are excluded — those are read, not loaded, and allowing
# them turns this into a general file-upload endpoint.
ALLOWED_EXT = (
    ".csv", ".tsv", ".parquet", ".xlsx", ".xls", ".json", ".jsonl", ".feather",
    ".csv.gz", ".tsv.gz", ".jsonl.gz",
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\- ]{0,120}$")


class DatasetError(ValueError):
    """Rejected input. The message is user-facing — say what to do instead."""


def max_bytes() -> int:
    try:
        mb = float(os.environ.get("ADK_CC_DATASET_UPLOAD_MAX_MB", "500"))
    except ValueError:
        mb = 500.0
    return int(mb * 1024 * 1024)


def check_name(name: str) -> str:
    """Validate a dataset filename. Returns the bare name (never a path)."""
    name = (name or "").strip()
    if not name:
        raise DatasetError("a filename is required")
    if "/" in name or "\\" in name or name.startswith("."):
        raise DatasetError(f"unsafe filename: {name!r}")
    if not _SAFE_NAME.match(name):
        raise DatasetError(
            f"unsafe filename: {name!r} — letters, digits, dot, dash, "
            "underscore and space only"
        )
    if not lower_ext(name):
        raise DatasetError(
            f"unsupported format: {name!r}. Supported: "
            + ", ".join(sorted(set(ALLOWED_EXT)))
        )
    return name


def lower_ext(name: str) -> Optional[str]:
    """The matching allowed extension, longest first so `.csv.gz` beats `.gz`."""
    low = name.lower()
    for ext in sorted(ALLOWED_EXT, key=len, reverse=True):
        if low.endswith(ext):
            return ext
    return None


def check_size(size: int) -> None:
    cap = max_bytes()
    if size > cap:
        raise DatasetError(
            f"dataset is {size / 1024 / 1024:.1f}MB, over the "
            f"{cap / 1024 / 1024:.0f}MB limit. Filter or convert it first "
            f"(parquet is usually several times smaller), or raise "
            f"ADK_CC_DATASET_UPLOAD_MAX_MB."
        )


def data_dir(workspace_root: Path, *, create: bool = False) -> Path:
    d = Path(workspace_root) / DATA_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def target_path(workspace_root: Path, name: str) -> Path:
    """Validated destination inside the workspace's data dir."""
    name = check_name(name)
    root = Path(workspace_root).resolve()
    dest = (data_dir(root) / name).resolve()
    if dest.parent != data_dir(root).resolve():
        raise DatasetError(f"unsafe filename: {name!r}")
    return dest


def ingest_local_path(workspace_root: Path, src: str, *, name: str = "") -> dict:
    """Copy a local file into the workspace (desktop). Returns its listing row."""
    source = Path(os.path.abspath(os.path.expanduser(src.strip())))
    if not source.is_file():
        raise DatasetError(f"not a file: {source}")
    dest = target_path(workspace_root, name or source.name)
    size = source.stat().st_size
    check_size(size)
    data_dir(workspace_root, create=True)
    shutil.copy2(source, dest)
    _log.info("dataset ingested: %s (%d bytes)", dest.name, size)
    return describe(dest, workspace_root)


def write_bytes(workspace_root: Path, name: str, blob: bytes) -> dict:
    """Store an uploaded body (web). Same validation as the local path route."""
    check_size(len(blob))
    dest = target_path(workspace_root, name)
    data_dir(workspace_root, create=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(blob)
    os.replace(tmp, dest)          # atomic — never expose a half-written dataset
    _log.info("dataset uploaded: %s (%d bytes)", dest.name, len(blob))
    return describe(dest, workspace_root)


def describe(path: Path, workspace_root: Path) -> dict:
    """One listing row. Cheap by design — no parsing, no row counting.

    Shape and dtypes need the analysis runtime and belong to the dataset
    browser (W6.2); doing it here would make listing a directory cost a sandbox
    round trip per file.
    """
    st = path.stat()
    return {
        "name": path.name,
        "path": str(Path(DATA_DIR) / path.name),
        "bytes": st.st_size,
        "modified": int(st.st_mtime),
        "format": (lower_ext(path.name) or "").lstrip("."),
    }


def listing(workspace_root: Path) -> list[dict]:
    d = data_dir(workspace_root)
    if not d.is_dir():
        return []
    rows = [
        describe(p, workspace_root)
        for p in sorted(d.iterdir())
        if p.is_file() and lower_ext(p.name) and not p.name.endswith(".part")
    ]
    return rows


def remove(workspace_root: Path, name: str) -> bool:
    dest = target_path(workspace_root, name)
    if not dest.is_file():
        return False
    dest.unlink()
    return True


def supported() -> Iterable[str]:
    return sorted(set(ALLOWED_EXT))


# --- profiling (W6.2) -------------------------------------------------------
#
# What an analyst checks before asking anything: shape, dtypes, nulls, head.
# Getting it from the UI rather than from a turn saves the round trip AND the
# tokens — a `df.info()` transcript is a surprisingly large fraction of an early
# analysis conversation.
#
# Bounded by construction: parquet reads metadata (exact row count, no scan),
# text formats read `sample_rows` rows and count newlines. Nothing here loads a
# dataset whole, which is the same rule `sandbox/dataset_guard.py` enforces for
# the agent.

PROFILE_SAMPLE_ROWS = 500
HEAD_ROWS = 8
_CELL_CHARS = 60

PROFILE_SCRIPT = r"""
import json, os, sys
path, sample, head_n, cell = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
low = path.lower()
out = {"path": path, "bytes": os.path.getsize(path), "rows_exact": False,
       "sampled": 0, "columns": [], "head": {"columns": [], "rows": []}}

def count_lines(p):
    n = 0
    with open(p, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            n += b.count(b"\n")
    return n

try:
    import pandas as pd
    if low.endswith(".parquet") or low.endswith(".feather"):
        import pyarrow.parquet as pq
        if low.endswith(".parquet"):
            pf = pq.ParquetFile(path)
            out["rows"] = pf.metadata.num_rows            # exact, no scan
            out["rows_exact"] = True
            batch = next(pf.iter_batches(batch_size=sample))
            df = batch.to_pandas()
        else:
            import pyarrow.feather as fe
            df = fe.read_table(path).to_pandas().head(sample)
            out["rows"] = len(df); out["rows_exact"] = True
    elif low.endswith((".xlsx", ".xls")):
        df = pd.read_excel(path, nrows=sample)
        out["rows"] = len(df)
        out["rows_exact"] = len(df) < sample
    elif low.endswith((".jsonl", ".jsonl.gz")):
        df = pd.read_json(path, lines=True, nrows=sample)
        out["rows"] = count_lines(path) if not path.endswith(".gz") else len(df)
        out["rows_exact"] = not path.endswith(".gz")
    elif low.endswith(".json"):
        df = pd.read_json(path)
        out["rows"] = len(df); out["rows_exact"] = True
        df = df.head(sample)
    else:
        sep = "\t" if ".tsv" in low else ","
        df = pd.read_csv(path, nrows=sample, sep=sep)
        if path.endswith(".gz"):
            out["rows"] = len(df)
        else:
            lines = count_lines(path)
            out["rows"] = max(0, lines - 1)               # header
            out["rows_exact"] = True
    out["sampled"] = int(len(df))
    out["columns"] = [
        {"name": str(c), "dtype": str(df[c].dtype),
         "nulls": int(df[c].isna().sum()),
         "null_pct": round(float(df[c].isna().mean()) * 100, 1)}
        for c in df.columns
    ]
    head = df.head(head_n)
    out["head"] = {
        "columns": [str(c) for c in head.columns],
        "rows": [[(str(v)[:cell]) for v in row] for row in head.itertuples(index=False)],
    }
except Exception as e:
    out["error"] = f"{type(e).__name__}: {e}"
print("__ADKCC_PROFILE__" + json.dumps(out))
"""


def profile_command(rel_path: str, *, sample_rows: int = PROFILE_SAMPLE_ROWS,
                    python: str = "python3") -> str:
    """Shell command that profiles one dataset with the managed interpreter."""
    safe = rel_path.replace("'", "'\\''")
    return (
        "mkdir -p .adk-cc; "
        f"cat <<'__ADKCC_PY__' > .adk-cc/_profile.py\n{PROFILE_SCRIPT}\n__ADKCC_PY__\n"
        f"{python} .adk-cc/_profile.py '{safe}' {sample_rows} {HEAD_ROWS} {_CELL_CHARS}"
    )


def parse_profile(stdout: str) -> Optional[dict]:
    """Pull the JSON line out of the script's stdout. Returns None if absent —
    a profiler that half-worked must not be reported as a profile."""
    import json as _json

    for line in (stdout or "").splitlines():
        if line.startswith("__ADKCC_PROFILE__"):
            try:
                return _json.loads(line[len("__ADKCC_PROFILE__"):])
            except ValueError:
                return None
    return None
