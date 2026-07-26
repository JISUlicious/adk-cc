# Pandas Indexing & Selection — Expert Skill

You are an expert data analyst. Apply the following indexing patterns precisely.
Choosing the right indexer is critical for correctness, performance, and
avoiding the SettingWithCopyWarning.

## The Four Primary Indexers

| Indexer | Axis | Basis | Best For |
|---|---|---|---|
| `df["col"]` | columns | label | Single column access |
| `.loc[row, col]` | both | **label** | Label-based selection |
| `.iloc[row, col]` | both | **position** | Integer-position selection |
| `.at[row, col]` | both | **label** | Single scalar (fast) |
| `.iat[row, col]` | both | **position** | Single scalar by position |

## Column Selection

```python
# Single column → Series
s = df["price"]

# Multiple columns → DataFrame
sub = df[["user_id", "price", "timestamp"]]

# Dynamic column selection
cols = [c for c in df.columns if c.startswith("feature_")]
df[cols]

# Select by dtype
numeric_df = df.select_dtypes(include="number")
str_df     = df.select_dtypes(include=["object", "string", "category"])
date_df    = df.select_dtypes(include="datetime")
```

## Row Selection — Boolean Indexing

```python
# Single condition
df[df["status"] == "active"]
df[df["amount"] > 1000]

# Multiple conditions — use & | ~ with parentheses
df[(df["status"] == "active") & (df["amount"] > 1000)]
df[(df["region"] == "US") | (df["region"] == "EU")]
df[~df["status"].isin(["cancelled", "refunded"])]

# isin / notin
df[df["category"].isin(["Electronics", "Books"])]
df[~df["category"].isin(["Spam"])]

# String filters
df[df["name"].str.startswith("A")]
df[df["email"].str.contains(r"@gmail\.com", regex=True, na=False)]

# Date range filter
df[df["date"].between("2024-01-01", "2024-12-31")]
df[(df["date"] >= "2024-01-01") & (df["date"] < "2025-01-01")]

# Null filters
df[df["phone"].notna()]
df[df["optional_col"].isna()]
```

## .loc — Label-Based Selection

```python
# Rows by label, all columns
df.loc[42]
df.loc[[10, 20, 30]]
df.loc[10:30]            # label slice (inclusive both ends)

# Rows + columns
df.loc[df["amount"] > 1000, ["user_id", "amount"]]
df.loc[:, "price":"revenue"]   # column label slice

# Conditional row selection with column subset
df.loc[(df["status"] == "active") & (df["amount"] > 0), "revenue"] *= 1.1

# Setting values safely (no chained indexing)
df.loc[df["flag"] == True, "score"] = 0
```

## .iloc — Position-Based Selection

```python
df.iloc[0]               # first row
df.iloc[-1]              # last row
df.iloc[0:10]            # first 10 rows
df.iloc[[0, 5, 10]]      # rows at positions 0, 5, 10

df.iloc[:, 0]            # first column
df.iloc[:, [0, 2, 4]]    # columns at positions 0, 2, 4
df.iloc[0:5, 0:3]        # top-left 5×3 sub-grid

# Last 3 rows, last 2 columns
df.iloc[-3:, -2:]
```

## .query() — Expression Strings

```python
# Simple
df.query("amount > 1000")
df.query("status == 'active'")

# Compound
df.query("status == 'active' and amount > 1000")
df.query("region in ['US', 'EU']")
df.query("amount > @threshold")           # reference Python variable with @
df.query("price > price.mean()")          # column expressions

# Date comparison
df.query("date >= '2024-01-01'")

# Index access
df.query("index > 100")                  # works on RangeIndex
```

`.query()` advantages: readable, avoids boolean-operator parentheses, safe
against chained-indexing bugs.

## MultiIndex (Hierarchical)

```python
# Create
midx_df = df.set_index(["region", "category"])

# Access outer level
midx_df.loc["US"]

# Access specific combination
midx_df.loc[("US", "Electronics")]

# Cross-section
midx_df.xs("Electronics", level="category")

# Query on MultiIndex levels
midx_df.loc[midx_df.index.get_level_values("region") == "US"]

# Reset MultiIndex
df_flat = midx_df.reset_index()
```

## .at / .iat — Single Scalar Access

```python
# Faster than .loc/.iloc for single value
val = df.at[row_label, "price"]
df.at[row_label, "price"] = 99.99

val = df.iat[0, 3]          # row 0, column position 3
df.iat[0, 3] = 42
```

Use `.at`/`.iat` in tight loops where speed matters.

## Avoiding SettingWithCopyWarning

**Problem:** Chained indexing silently fails to update the original DataFrame.

```python
# BAD — may not update df
df[df["status"] == "active"]["score"] = 100

# GOOD — use .loc
df.loc[df["status"] == "active", "score"] = 100

# GOOD — use .copy() when you want an independent slice
subset = df[df["status"] == "active"].copy()
subset["score"] = 100   # safe: separate object
```

**Copy-on-Write** is always enabled in pandas >= 3.0 (opt-in in 2.x):
```python
# All indexing operations return CoW views; chained writes raise errors explicitly
# Do NOT set pd.options.mode.copy_on_write — it's deprecated in pandas 3.0
```

## Sampling & Head/Tail

```python
df.head(10)
df.tail(5)
df.sample(n=100, random_state=42)
df.sample(frac=0.1, random_state=42)          # 10% sample
df.sample(n=100, weights="weight_col")         # weighted sampling
df.nlargest(10, "revenue")                     # top 10 by revenue
df.nsmallest(5, "error_rate")                  # bottom 5
```

## Filtering Patterns Cheat Sheet

```python
# Between (inclusive)
df[df["score"].between(80, 100)]

# Null / not-null
df[df["col"].isna()]
df[df["col"].notna()]

# Duplicate rows
df[df.duplicated(subset=["id"], keep=False)]

# Starts/ends with string
df[df["code"].str.startswith("SFO")]
df[df["file"].str.endswith(".csv")]

# Regex match
df[df["email"].str.match(r"^[\w.+-]+@[\w-]+\.\w+$", na=False)]

# All nulls in a column list
df[df[["col_a", "col_b"]].isnull().all(axis=1)]

# Any null in a column list
df[df[["col_a", "col_b"]].isnull().any(axis=1)]
```
