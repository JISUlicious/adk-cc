# Pandas Performance & Memory Optimization — Expert Skill

You are an expert data analyst. Apply these techniques to make pandas code fast
and memory-efficient with pandas >= 2.3.

## Step 1 — Profile First, Optimize Second

```python
import time
import tracemalloc

# Time a block
start = time.perf_counter()
result = df.groupby("category")["revenue"].sum()
elapsed = time.perf_counter() - start
print(f"Time: {elapsed:.3f}s")

# Memory usage
tracemalloc.start()
result = expensive_operation(df)
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(f"Peak memory: {peak / 1e6:.1f} MB")

# DataFrame memory
print(df.memory_usage(deep=True).sum() / 1e6, "MB")
print(df.memory_usage(deep=True).sort_values(ascending=False).head(10))

# %timeit in Jupyter
# %timeit df.groupby("category")["revenue"].sum()
```

## Step 2 — Optimize Dtypes (Most Impactful Change)

```python
def optimize_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Automatically downcast numeric dtypes."""
    for col in df.select_dtypes(include=["int64"]).columns:
        df[col] = pd.to_numeric(df[col], downcast="integer")
    for col in df.select_dtypes(include=["float64"]).columns:
        df[col] = pd.to_numeric(df[col], downcast="float")
    for col in df.select_dtypes(include=["object", "str"]).columns:
        if df[col].nunique() / len(df) < 0.5:
            df[col] = df[col].astype("category")
    return df

# Memory impact by dtype
# int64  → int32:  50% reduction
# float64 → float32: 50% reduction
# object → category: up to 95% reduction (low cardinality)
# object → string (arrow): ~30% reduction + faster ops
```

**Dtype size reference:**

| Dtype | Bytes | Use When |
|---|---|---|
| `bool` | 1 | binary flag |
| `boolean` | 1+null | nullable binary flag |
| `int8` | 1 | [-128, 127] |
| `int16` | 2 | [-32768, 32767] |
| `int32` | 4 | [-2B, 2B] |
| `int64` | 8 | larger integers (default) |
| `float32` | 4 | acceptable precision for most metrics |
| `float64` | 8 | high precision needed |
| `category` | varies | < 50% unique values |
| `string` (Arrow) | compact | text columns |

## Step 3 — PyArrow Backend (pandas 2.0+)

```python
# Load with PyArrow engine (fastest for CSV)
df = pd.read_csv("data.csv", engine="pyarrow", dtype_backend="pyarrow")

# Convert existing DataFrame
df = df.convert_dtypes(dtype_backend="pyarrow")

# Why PyArrow?
# - Strings stored contiguously in memory (not Python objects)
# - SIMD-accelerated operations
# - Zero-copy Arrow <> pandas interchange
# - Nullable dtypes for all types (no float64 trick for nullable ints)
```

## Step 4 — Vectorized Operations (Never Loop)

```python
# SLOW — Python-level loop
for i, row in df.iterrows():                   # ~1000x slower than vectorized
    df.at[i, "revenue"] = row["price"] * row["qty"]

# SLOW — apply row-wise
df["revenue"] = df.apply(lambda r: r["price"] * r["qty"], axis=1)

# FAST — vectorized arithmetic
df["revenue"] = df["price"] * df["qty"]

# FAST — numpy operations
import numpy as np
df["log_revenue"] = np.log1p(df["revenue"])
df["clipped"] = np.clip(df["value"], 0, 100)

# FAST — .str accessor (vectorized string)
df["domain"] = df["email"].str.extract(r"@(.+)$")

# FAST — .dt accessor (vectorized datetime)
df["month"] = df["date"].dt.month
```

### When apply() Is Unavoidable

```python
# Vectorize with numba for numerical apply
from numba import njit

@njit
def custom_calc(x, y, z):
    return x * y + z ** 2

df["result"] = custom_calc(df["a"].values, df["b"].values, df["c"].values)
```

## Step 5 — Copy-on-Write

CoW is always enabled in pandas >= 3.0 (opt-in in 2.x). Do **not** set
`pd.options.mode.copy_on_write = True` — it raises a warning in pandas 3.0.

```python
# CoW means views copy on mutation — behavior is always predictable
# Method chaining is fully safe and memory-efficient
result = (df
    .query("status == 'active'")
    .assign(revenue=lambda x: x["price"] * x["qty"])
    .groupby("region")["revenue"]
    .sum()
)

# NEVER use inplace=True — it is deprecated in pandas 3.0
# Use assignment instead: df = df.dropna()
```

## Step 6 — Large File Handling

```python
# 1. Read in chunks
results = []
for chunk in pd.read_csv("huge.csv", chunksize=200_000, dtype={"id": "int32"}):
    processed = chunk.query("status == 'active'").groupby("category")["revenue"].sum()
    results.append(processed)
final = pd.concat(results).groupby(level=0).sum()

# 2. Column pruning (only load what you need)
df = pd.read_csv("huge.csv", usecols=["id", "revenue", "date"])

# 3. Use Parquet for repeated access
df.to_parquet("data.parquet", engine="pyarrow", compression="snappy")
df = pd.read_parquet("data.parquet", columns=["id", "revenue"])  # column pruning

# 4. Use Dask for truly massive datasets (> RAM)
import dask.dataframe as dd
ddf = dd.read_csv("huge_*.csv")
result = ddf.groupby("category")["revenue"].sum().compute()
```

## Step 7 — GroupBy Performance

```python
# Fast: built-in agg functions (C implementation)
df.groupby("category")["revenue"].sum()       # fast
df.groupby("category")["revenue"].mean()      # fast
df.groupby("category")["revenue"].std()       # fast

# Fast: named agg with built-ins
df.groupby("category").agg(total=("revenue", "sum"), n=("id", "count"))

# Slower: lambda or custom functions (Python-level)
df.groupby("category")["revenue"].agg(lambda x: x.quantile(0.9))

# Avoid observed=False on categoricals (computes all combos)
df.groupby("category", observed=True)["revenue"].sum()  # only existing categories
```

## Step 8 — Merge Performance

```python
# Sort before merge_asof
left = left.sort_values("key")
right = right.sort_values("key")

# Use hash join (default for equality joins — already fast)
pd.merge(left, right, on="id")

# For repeated joins on the same key: set index first
df_b = df_b.set_index("customer_id")
result = df_a.join(df_b, on="customer_id")  # index join is faster
```

## Step 9 — Avoid Common Anti-Patterns

```python
# ❌ Growing DataFrame in a loop
df = pd.DataFrame()
for item in items:
    df = pd.concat([df, pd.DataFrame([item])])   # O(n²) — never do this

# ✅ Collect then concat once
rows = []
for item in items:
    rows.append(item)
df = pd.DataFrame(rows)                          # O(n)

# ❌ Chained indexing (slow + bug-prone)
df[df["a"] > 0]["b"] = 1

# ✅ loc
df.loc[df["a"] > 0, "b"] = 1

# ❌ apply(axis=1) for simple arithmetic
df.apply(lambda r: r["x"] + r["y"], axis=1)

# ✅ vectorized
df["x"] + df["y"]

# ❌ Repeated df.shape / df.columns calls in a loop
for _ in range(10_000):
    n = len(df)  # len() is O(1)

# ❌ astype on every row instead of once on the column
```

## Step 10 — SQL via PyArrow ADBC (pandas 2.2+)

```python
# Much faster than SQLAlchemy for large result sets
import adbc_driver_postgresql.dbapi as adbc

with adbc.connect("postgresql://user:pw@host/db") as conn:
    df = pd.read_sql("SELECT * FROM large_table", conn)
    df.to_sql("output", conn, if_exists="replace", index=False)
```

## Memory Reduction Checklist

- [ ] `df.info(memory_usage="deep")` — identify large columns
- [ ] Downcast `int64` → `int32`/`int16` where range permits
- [ ] Downcast `float64` → `float32` for non-critical precision
- [ ] Convert `object` string columns with < 50% unique → `category`
- [ ] Load only needed columns with `usecols=`
- [ ] Use `dtype=` on `read_csv()` to avoid object fallback
- [ ] Use method chaining to avoid copies (CoW is always-on in pandas 3.0)
- [ ] Use Parquet instead of CSV for repeated analysis
- [ ] Process large files in chunks
