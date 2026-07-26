# Pandas Merging, Joining & Concatenation — Expert Skill

You are an expert data analyst. Apply the following patterns for combining
DataFrames precisely. Choosing the wrong join type is a common source of
data quality errors — always verify row counts after any join.

## pd.merge() — SQL-Style Joins

```python
# Syntax
result = pd.merge(left, right, how="inner", on="key")

# how options:
# "inner" — only matching keys (default, safest)
# "left"  — all left rows, NaN for unmatched right
# "right" — all right rows, NaN for unmatched left
# "outer" — all rows from both, NaN for non-matches
# "cross" — Cartesian product (all combinations)
```

### Common Join Patterns

```python
# Single key
orders = pd.merge(orders, customers, on="customer_id", how="left")

# Multiple keys (composite key)
merged = pd.merge(
    df_a, df_b,
    on=["user_id", "date"],
    how="inner"
)

# Different column names in each DataFrame
merged = pd.merge(
    orders, products,
    left_on="product_id",
    right_on="id",
    how="left"
)

# Join on index
merged = pd.merge(df_a, df_b, left_index=True, right_index=True, how="inner")
merged = pd.merge(df_a, df_b, left_on="id", right_index=True, how="left")

# Handle duplicate column names with suffixes
merged = pd.merge(
    orders, returns,
    on="order_id",
    how="left",
    suffixes=("_order", "_return"),
)
```

### Validating Joins

```python
# Validate join type to catch data integrity issues
pd.merge(df_a, df_b, on="id", how="inner", validate="one_to_one")
pd.merge(orders, customers, on="customer_id", how="left", validate="many_to_one")
pd.merge(products, tags, on="product_id", how="left", validate="one_to_many")
# Raises MergeError if assumption is violated
```

**Always check row counts:**
```python
n_before = len(orders)
result = pd.merge(orders, customers, on="customer_id", how="left")
n_after = len(result)
print(f"Rows before: {n_before}, after: {n_after}, delta: {n_after - n_before}")
assert n_after == n_before, "Left join should not change row count!"
```

### Detecting Unmatched Keys

```python
# Which orders have no matching customer?
merged = pd.merge(orders, customers, on="customer_id", how="left", indicator=True)
unmatched = merged[merged["_merge"] == "left_only"]
print(f"Orders with no customer: {len(unmatched)}")

# Anti-join: rows in left that are NOT in right
anti = merged.loc[merged["_merge"] == "left_only"].drop(columns="_merge")
```

## DataFrame.join() — Index-Based

```python
# join uses the index by default
result = df_a.join(df_b, how="left")                    # index on index
result = df_a.join(df_b.set_index("id"), on="id")       # column on index
result = df_a.join([df_b, df_c])                         # join multiple at once
```

## pd.concat() — Stack DataFrames

```python
# Stack vertically (row-wise) — same columns
df_all = pd.concat([df_2022, df_2023, df_2024], ignore_index=True)

# With source labels
df_all = pd.concat(
    {"2022": df_2022, "2023": df_2023},
    names=["year", "row"],
)

# Stack horizontally (column-wise) — same rows
df_wide = pd.concat([df_features, df_targets], axis=1)

# Handling mismatched columns
df_all = pd.concat([df_a, df_b], ignore_index=True, sort=False)
# NaN fills missing columns
```

### concat vs merge

| Task | Use |
|---|---|
| Same schema, stacking rows | `pd.concat(axis=0)` |
| Side-by-side columns (same index) | `pd.concat(axis=1)` |
| Joining on a key | `pd.merge()` |
| Join on index | `df.join()` |

## Appending Rows Efficiently

**Do NOT use deprecated `.append()`** (removed in pandas 2.0).

```python
# Collect DataFrames in a list, then concat once
chunks = []
for batch in data_batches:
    processed = process(batch)
    chunks.append(processed)
df = pd.concat(chunks, ignore_index=True)   # one concat at the end
```

Concatenating inside a loop is O(n²). Always collect, then concat once.

## Merging Time-Ordered Data

### Merge Asof (Nearest Prior Key)

```python
# merge_asof: match each row in left to the nearest prior row in right
# Both DataFrames must be sorted by the key column
quotes = quotes.sort_values("timestamp")
trades = trades.sort_values("timestamp")

result = pd.merge_asof(
    trades,
    quotes,
    on="timestamp",
    by="ticker",              # exact match on this column
    direction="backward",     # nearest prior quote
    tolerance=pd.Timedelta("1min"),  # only match within 1 minute
)
```

Use cases: matching trades to last available quote, joining sensor readings
to the nearest configuration change.

### Merge Ordered

```python
# merge_ordered: outer join with optional fill
result = pd.merge_ordered(
    df_a, df_b,
    on="date",
    fill_method="ffill",    # forward-fill after merge
)
```

## Combining with update() and combine_first()

```python
# update: overwrite NaN values in-place from another DataFrame (aligned on index)
df.update(df_updates)              # modifies df in-place, NaN only

# combine_first: fill NaN values from another DataFrame
df_combined = df.combine_first(df_fallback)
# Uses df where non-null, else falls back to df_fallback
```

## Common Mistakes & Fixes

| Problem | Symptom | Fix |
|---|---|---|
| Many-to-many join | Row explosion | Add `validate=` param; check key uniqueness |
| Missing rows after inner join | Fewer rows than expected | Use `how="left"` with `indicator=True` to diagnose |
| Duplicate columns | `_x`, `_y` suffixes | Set `suffixes=` explicitly; drop/rename post-merge |
| Wrong dtype on key | No matches despite matching values | Align dtypes: `df["id"] = df["id"].astype(int)` |
| Slow merge on large data | Timeout | Set index first: `df.set_index("key")` before join |

## Full Example: Order Enrichment Pipeline

```python
# Start: orders DataFrame
# Join customers (many-to-one), products (many-to-one), promotions (left)
result = (
    orders
    .merge(customers[["customer_id", "name", "region"]],
           on="customer_id", how="left", validate="many_to_one")
    .merge(products[["product_id", "name", "category", "cost"]],
           on="product_id", how="left", validate="many_to_one",
           suffixes=("", "_product"))
    .merge(promotions[["order_id", "discount_pct"]],
           on="order_id", how="left", validate="one_to_one")
)

# Verify no row count change from left joins
assert len(result) == len(orders), "Row count changed unexpectedly"

# Check for unmatched keys
print("Unmatched customers:", result["name"].isna().sum())
print("Unmatched products:", result["category"].isna().sum())
```
