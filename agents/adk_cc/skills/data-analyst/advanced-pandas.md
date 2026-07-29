# Advanced Pandas v2.3 — Expert Skill

You are an expert data analyst. Apply the following advanced techniques for
sophisticated data manipulation with pandas >= 2.3.

## Method Chaining with pipe()

```python
# pipe() inserts the DataFrame as the first argument to any function
# Enables fully chainable custom transformations

def add_revenue(df, price_col="price", qty_col="qty"):
    return df.assign(revenue=df[price_col] * df[qty_col])

def normalize(df, col, group_col):
    df = df.copy()
    df[f"{col}_norm"] = df.groupby(group_col)[col].transform(
        lambda x: (x - x.mean()) / x.std()
    )
    return df

def tag_outliers(df, col, n_std=3):
    mu, sigma = df[col].mean(), df[col].std()
    return df.assign(
        is_outlier=(df[col] - mu).abs() > n_std * sigma
    )

result = (
    df
    .pipe(add_revenue)
    .pipe(normalize, col="revenue", group_col="region")
    .pipe(tag_outliers, col="revenue_norm")
    .query("not is_outlier")
    .groupby("region")
    .agg(total=("revenue", "sum"), avg=("revenue", "mean"))
)
```

## Nullable Extension Types (pandas 1.0+)

Unlike legacy NumPy dtypes, nullable types support `NA` (missing value)
without coercing integers to float64.

```python
# Nullable integer
s = pd.array([1, 2, None, 4], dtype="Int64")   # capital I
df["count"] = df["count"].astype("Int32")

# Nullable float
df["rate"] = df["rate"].astype("Float32")

# Nullable boolean
df["flag"] = df["flag"].astype("boolean")

# Arrow-backed string (pandas 2.0+)
df["name"] = df["name"].astype("string")       # default StringDtype
# or explicitly Arrow-backed:
df["name"] = df["name"].astype(pd.StringDtype(storage="pyarrow"))

# Convert entire DataFrame to best nullable types
df = df.convert_dtypes()                        # infers best nullable type
df = df.convert_dtypes(dtype_backend="pyarrow") # force PyArrow backend
```

**Key difference:**

```python
# Legacy: NaN forces int → float64
pd.Series([1, 2, None]).dtype       # float64
# Nullable: preserves int with NA
pd.array([1, 2, None], dtype="Int64").dtype  # Int64
```

## Categorical Dtype — Advanced Usage

```python
# Define explicit category order (for sorting/comparison)
size_type = pd.CategoricalDtype(
    categories=["XS", "S", "M", "L", "XL", "XXL"],
    ordered=True
)
df["size"] = df["size"].astype(size_type)

# Ordered comparisons
df[df["size"] >= "L"]                         # works because ordered=True
df.sort_values("size")                        # sorts in defined category order

# GroupBy with all categories (even empty ones)
df.groupby("size", observed=False)["revenue"].sum()

# Rename categories
df["size"] = df["size"].cat.rename_categories({"XS": "Extra Small"})

# Add / remove categories
df["size"] = df["size"].cat.add_categories(["XXXL"])
df["size"] = df["size"].cat.remove_unused_categories()

# Category codes (integer representation — useful for ML)
df["size_code"] = df["size"].cat.codes
```

## MultiIndex — Hierarchical Indexing

```python
# Create MultiIndex
df = df.set_index(["year", "quarter", "region"])

# Access levels
df.loc["2024"]                                # outer level
df.loc[("2024", "Q1")]                        # outer + middle
df.loc[("2024", "Q1", "US")]                  # exact
df.loc[("2024", slice(None), "US")]           # all quarters for 2024, US

# Cross-section
df.xs("US", level="region")
df.xs(("2024", "Q1"), level=["year", "quarter"])

# IndexSlice for cleaner slicing
idx = pd.IndexSlice
df.loc[idx["2024":"2025", "Q1":"Q2", :], "revenue"]

# Swap level order
df = df.swaplevel("quarter", "region")
df = df.sort_index()

# Flatten MultiIndex columns (after pivot_table)
df.columns = ["_".join(col).strip("_") for col in df.columns]
```

## eval() and query() — Expression Engine

```python
# eval: fast vectorized expression evaluation
# Best for complex arithmetic on large DataFrames (avoids temp arrays)
df = df.eval("margin = (revenue - cost) / revenue")
df = df.eval("""
    gross = price * qty
    discount_amt = gross * discount_pct
    net = gross - discount_amt
""")

# query: filter with expression string
df.query("region == 'US' and revenue > @min_rev and status in ['active', 'trial']")

# Use @ to reference local variables
threshold = df["revenue"].quantile(0.9)
df.query("revenue > @threshold")

# pandas eval uses numexpr for speed (install numexpr for 2–3x boost)
```

## Window Functions — Advanced

```python
# Rolling with custom offset (DatetimeIndex required)
df = df.set_index("date")
df["7d_mean"]  = df["revenue"].rolling("7D").mean()
df["30d_mean"] = df["revenue"].rolling("30D").mean()
df["90d_mean"] = df["revenue"].rolling("90D").mean()

# Multiple statistics in one rolling pass
rolled = df["revenue"].rolling(30).agg(
    roll_mean="mean",
    roll_std="std",
    roll_min="min",
    roll_max="max",
)

# Bollinger Bands
df["bb_mid"]   = df["price"].rolling(20).mean()
df["bb_upper"] = df["bb_mid"] + 2 * df["price"].rolling(20).std()
df["bb_lower"] = df["bb_mid"] - 2 * df["price"].rolling(20).std()

# Expanding window (cumulative stats)
df["running_avg"]  = df["revenue"].expanding().mean()
df["running_best"] = df["revenue"].expanding().max()

# EWM with halflife as timedelta (pandas 1.1+)
df["ewm_halflife"] = df["price"].ewm(
    halflife=pd.Timedelta("7 days"),
    times=df.index
).mean()
```

## DataFrame Styling (Output Formatting)

```python
# pandas Styler for Jupyter notebooks / HTML reports
styled = (
    df.style
    .format({
        "revenue": "${:,.0f}",
        "margin": "{:.1%}",
        "growth": "{:+.1%}",
        "date": "{:%Y-%m-%d}",
    })
    .background_gradient(subset=["revenue"], cmap="Blues")
    .background_gradient(subset=["margin"], cmap="RdYlGn", vmin=0, vmax=0.5)
    .bar(subset=["growth"], align="zero", color=["#d65f5f", "#5fba7d"])
    .highlight_max(subset=["revenue"], color="lightgreen")
    .highlight_min(subset=["revenue"], color="salmon")
    .set_caption("Revenue Summary by Region")
    .set_table_styles([
        {"selector": "th", "props": [("font-size", "11px"), ("text-align", "center")]},
    ])
)

# Export
styled.to_excel("report.xlsx", engine="openpyxl")
styled.to_html("report.html")

# Display in Jupyter
styled  # just output the variable
```

## Custom Accessor Extension

```python
# Register a custom namespace on DataFrame
@pd.api.extensions.register_dataframe_accessor("biz")
class BizAccessor:
    def __init__(self, pandas_obj):
        self._obj = pandas_obj

    def revenue_summary(self, group_col):
        return self._obj.groupby(group_col).agg(
            total_revenue=("revenue", "sum"),
            avg_revenue=("revenue", "mean"),
            n_orders=("order_id", "count"),
        )

    def top_n(self, col, n=10):
        return self._obj.nlargest(n, col)

# Usage
df.biz.revenue_summary("region")
df.biz.top_n("revenue")
```

## Efficient Iteration Patterns

```python
# When iteration is truly necessary, use these (fastest first):

# 1. itertuples — namedtuples, ~10x faster than iterrows
for row in df.itertuples(index=False):
    process(row.revenue, row.category)

# 2. to_dict("records") — list of dicts
for record in df.to_dict("records"):
    process(record["revenue"])

# 3. Vectorize with numpy/numba (fastest for numerics)
import numba
@numba.njit
def loop_calc(revenues, costs):
    results = np.empty(len(revenues))
    for i in range(len(revenues)):
        results[i] = revenues[i] - costs[i]
    return results
df["profit"] = loop_calc(df["revenue"].values, df["cost"].values)

# 4. iterrows — SLOWEST, avoid for large DataFrames
for idx, row in df.iterrows():   # 1000x slower than vectorized
    ...
```

## Pandas Options

```python
# Display settings
pd.set_option("display.max_rows", 100)
pd.set_option("display.max_columns", 50)
pd.set_option("display.float_format", "{:.2f}".format)
pd.set_option("display.max_colwidth", 80)
pd.set_option("display.width", 120)

# Performance settings
pd.set_option("compute.use_numexpr", True)          # enable numexpr in eval/query
# Note: CoW is always-on in pandas 3.0; do NOT set mode.copy_on_write
# Note: Arrow strings are the default in pandas 3.0; do NOT set future.infer_string

# Context manager for temporary settings
with pd.option_context("display.max_rows", 200, "display.float_format", "{:.4f}".format):
    display(df)

# Reset all options
pd.reset_option("all")
```
