# Pandas Data Transformation — Expert Skill

You are an expert data analyst. Apply the following transformation patterns
using pandas >= 2.3 best practices. Prefer vectorized operations over loops.

## Creating and Modifying Columns

```python
# assign() — preferred: returns new DataFrame, chainable
df = df.assign(
    revenue=lambda x: x["price"] * x["quantity"],
    revenue_k=lambda x: x["revenue"] / 1000,
    is_high_value=lambda x: x["revenue"] > 10_000,
    log_revenue=lambda x: np.log1p(x["revenue"]),
)

# Direct assignment (mutates in place)
df["margin"] = (df["revenue"] - df["cost"]) / df["revenue"]
df["bucket"] = pd.cut(df["score"], bins=[0, 60, 80, 100], labels=["C", "B", "A"])
```

## Vectorized Operations — Always Prefer Over Loops

```python
import numpy as np

# Arithmetic
df["total"] = df["qty"] * df["unit_price"] * (1 - df["discount"])

# String operations via .str accessor
df["first_name"] = df["full_name"].str.split().str[0]
df["domain"] = df["email"].str.extract(r"@(.+)$")
df["upper"] = df["status"].str.upper()
df["trimmed"] = df["notes"].str.strip()

# Datetime operations via .dt accessor
df["year"]    = df["date"].dt.year
df["month"]   = df["date"].dt.month
df["weekday"] = df["date"].dt.day_name()
df["quarter"] = df["date"].dt.quarter
df["is_weekend"] = df["date"].dt.dayofweek >= 5

# np.where for conditional columns
df["tier"] = np.where(df["revenue"] > 100_000, "enterprise",
             np.where(df["revenue"] > 10_000,  "mid-market", "smb"))

# np.select for multiple conditions
conditions = [
    df["score"] >= 90,
    df["score"] >= 75,
    df["score"] >= 60,
]
choices = ["A", "B", "C"]
df["grade"] = np.select(conditions, choices, default="F")
```

## map() and replace() — Element-Wise

```python
# map: apply a function or dict mapping to a Series
df["status_code"] = df["status"].map({"active": 1, "inactive": 0, "pending": 2})
df["label"] = df["id"].map(id_to_label_dict)   # dict lookup
df["doubled"] = df["value"].map(lambda x: x * 2)

# replace: substitute values (more flexible than map, preserves unmapped)
df["region"] = df["region"].replace({"USA": "US", "U.S.A": "US", "United States": "US"})
df = df.replace({"status": {"Active": "active", "Inactive": "inactive"}})
```

## apply() — Row or Column Level

```python
# Column-wise (axis=0, default) — applies function to each column Series
col_means = df[["a", "b", "c"]].apply("mean")

# Row-wise (axis=1) — applies function to each row Series (SLOW for large data)
df["max_feature"] = df[["f1", "f2", "f3"]].apply("max", axis=1)

# Use only when vectorized alternative doesn't exist
def complex_logic(row):
    if row["type"] == "A":
        return row["x"] * 2
    return row["y"] + 10

df["result"] = df.apply(complex_logic, axis=1)  # slow — use np.select when possible
```

**Performance note:** `apply(axis=1)` is 100–1000x slower than vectorized ops.
Prefer `np.where`, `np.select`, `.str` accessors, and arithmetic operators.

## GroupBy — Split-Apply-Combine

```python
# Basic aggregation
df.groupby("category")["revenue"].sum()
df.groupby(["region", "category"])["revenue"].agg(["sum", "mean", "count"])

# Named aggregations (pandas 0.25+)
agg = df.groupby("category").agg(
    total_revenue=("revenue", "sum"),
    avg_revenue=("revenue", "mean"),
    n_orders=("order_id", "count"),
    max_order=("revenue", "max"),
    p90_revenue=("revenue", lambda x: x.quantile(0.9)),
)

# Multiple columns, multiple aggs
df.groupby("region").agg({
    "revenue": ["sum", "mean"],
    "quantity": "sum",
    "discount": "mean",
})

# transform() — adds group result back to original shape
df["group_mean"] = df.groupby("category")["revenue"].transform("mean")
df["pct_of_group"] = df["revenue"] / df.groupby("category")["revenue"].transform("sum")
df["rank_in_group"] = df.groupby("category")["revenue"].rank(ascending=False)

# filter() — keep groups satisfying a condition
large_groups = df.groupby("category").filter(lambda g: len(g) >= 100)
active_regions = df.groupby("region").filter(lambda g: g["revenue"].sum() > 1_000_000)

# apply() on groups — arbitrary group-level transformation
def normalize_group(g):
    g["norm_score"] = (g["score"] - g["score"].mean()) / g["score"].std()
    return g

df = df.groupby("category", group_keys=False).apply(normalize_group)
```

## Pivot and Reshape

```python
# pivot_table — like Excel pivot, with aggregation
pivot = df.pivot_table(
    index="region",
    columns="category",
    values="revenue",
    aggfunc="sum",
    fill_value=0,
    margins=True,       # row/col totals
    margins_name="Total",
)

# pivot — no aggregation (needs unique index+column combos)
wide = df.pivot(index="date", columns="metric", values="value")

# melt — wide to long (unpivot)
long = pd.melt(
    df,
    id_vars=["id", "date"],
    value_vars=["jan", "feb", "mar"],
    var_name="month",
    value_name="amount",
)

# stack / unstack — work with MultiIndex
stacked = df.stack()           # columns → innermost index level
unstacked = df.unstack()       # innermost index level → columns

# crosstab — frequency table
ct = pd.crosstab(df["region"], df["status"], values=df["revenue"], aggfunc="sum")
```

## Binning and Discretization

```python
# Equal-width bins
df["age_band"] = pd.cut(
    df["age"],
    bins=[0, 18, 35, 50, 65, 120],
    labels=["<18", "18-34", "35-49", "50-64", "65+"],
    right=False,
)

# Equal-frequency (quantile) bins
df["revenue_quartile"] = pd.qcut(df["revenue"], q=4, labels=["Q1", "Q2", "Q3", "Q4"])
df["revenue_decile"] = pd.qcut(df["revenue"], q=10, labels=False)  # 0–9 integers

# Custom bin with np.digitize
bins = [0, 100, 500, 1000, np.inf]
df["tier"] = pd.cut(df["revenue"], bins=bins, labels=["Low", "Medium", "High", "VIP"])
```

## Sorting

```python
# Sort by single column
df = df.sort_values("revenue", ascending=False)

# Sort by multiple columns (multi-key)
df = df.sort_values(["region", "revenue"], ascending=[True, False])

# Sort index
df = df.sort_index()

# Stable sort (preserves original order for ties)
df.sort_values("score", kind="mergesort")

# nlargest / nsmallest (faster than sort + head)
df.nlargest(10, "revenue")
df.nsmallest(5, "error_rate")
```

## String Transformations (`.str` Accessor)

```python
# Splitting
df[["first", "last"]] = df["full_name"].str.split(" ", n=1, expand=True)
df["words"] = df["sentence"].str.split()           # list of words

# Pattern extraction
df[["area_code", "number"]] = df["phone"].str.extract(r"(\d{3})-(\d{7})")

# Contains / match
df["has_promo"] = df["notes"].str.contains("promo|discount", case=False, na=False)

# Replace
df["clean"] = df["text"].str.replace(r"<[^>]+>", "", regex=True)  # strip HTML

# Padding / alignment
df["padded_id"] = df["id"].astype(str).str.zfill(8)
```

## Date/Time Transformations (`.dt` Accessor)

```python
df["date"] = pd.to_datetime(df["date"])

# Components
df["year"]     = df["date"].dt.year
df["month"]    = df["date"].dt.month
df["day"]      = df["date"].dt.day
df["hour"]     = df["date"].dt.hour
df["week"]     = df["date"].dt.isocalendar().week
df["quarter"]  = df["date"].dt.quarter
df["weekday"]  = df["date"].dt.day_of_week          # 0=Monday
df["is_month_end"] = df["date"].dt.is_month_end

# Arithmetic
df["age_days"] = (pd.Timestamp.now() - df["birth_date"]).dt.days
df["next_week"] = df["date"] + pd.Timedelta(weeks=1)

# Rounding
df["hour_bucket"] = df["timestamp"].dt.floor("h")
df["day_bucket"]  = df["timestamp"].dt.normalize()
```

## Method Chaining — Preferred Style

```python
result = (
    df
    .query("status == 'active' and amount > 0")
    .assign(
        revenue=lambda x: x["price"] * x["qty"],
        margin=lambda x: (x["revenue"] - x["cost"]) / x["revenue"],
    )
    .groupby(["region", "category"])
    .agg(total_revenue=("revenue", "sum"), avg_margin=("margin", "mean"))
    .sort_values("total_revenue", ascending=False)
    .reset_index()
    .head(20)
)
```
