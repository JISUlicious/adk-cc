# Pandas Data Cleaning & Preprocessing — Expert Skill

You are an expert data analyst. Apply the following techniques to clean and
prepare DataFrames using pandas >= 2.3 best practices.

## Missing Value Handling

### Detection
```python
df.isnull().sum()                    # count NaN per column
df.isnull().mean()                   # fraction missing per column
df[df["col"].isnull()]               # rows where col is NaN
```

### Dropping
```python
# Drop rows where ALL values are NaN
df = df.dropna(how="all")

# Drop rows missing any value in critical columns
df = df.dropna(subset=["user_id", "timestamp"])

# Drop columns where > 50% is missing
threshold = len(df) * 0.5
df = df.dropna(axis=1, thresh=int(threshold))
```

### Filling
```python
# Constant fill
df["status"] = df["status"].fillna("unknown")

# Statistical fill (per-column)
df["amount"] = df["amount"].fillna(df["amount"].median())
df["score"] = df["score"].fillna(df["score"].mean())

# Forward / backward fill (time-ordered data)
df = df.sort_values("timestamp")
df["price"] = df["price"].ffill()        # propagate last valid value forward
df["price"] = df["price"].bfill()        # propagate next valid value backward

# Group-wise fill (e.g. fill with group median)
df["amount"] = df.groupby("category")["amount"].transform(
    lambda x: x.fillna(x.median())
)
```

### Interpolation
```python
# Linear interpolation (good for evenly-spaced time series)
df["sensor"] = df["sensor"].interpolate(method="linear")

# Time-aware interpolation
df = df.set_index("timestamp")
df["sensor"] = df["sensor"].interpolate(method="time")

# Other methods: "polynomial", "spline", "pchip", "akima"
```

## Duplicate Handling

```python
# Remove exact duplicates (keep first occurrence)
df = df.drop_duplicates()

# Remove duplicates on key columns
df = df.drop_duplicates(subset=["user_id", "date"], keep="last")

# Keep all duplicates for inspection
dupes = df[df.duplicated(subset=["user_id"], keep=False)]
```

## Data Type Conversion

```python
# Numeric conversion
df["age"] = pd.to_numeric(df["age"], errors="coerce")       # NaN on failure
df["price"] = df["price"].astype("float32")                 # reduce memory
df["count"] = df["count"].astype("Int32")                   # nullable integer

# Boolean
df["is_active"] = df["is_active"].astype("boolean")         # nullable bool

# String / categorical
df["status"] = df["status"].astype("category")              # low-cardinality
df["name"] = df["name"].astype("string")                    # Arrow-backed string

# Datetime
df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")  # Unix timestamp

# Validate conversion succeeded
bad_dates = df["date"].isnull() & df["date_raw"].notnull()
print(f"Failed date conversions: {bad_dates.sum()}")
```

## String Cleaning

```python
# Normalize whitespace and casing
df["name"] = df["name"].str.strip().str.lower()
df["code"] = df["code"].str.strip().str.upper()

# Remove non-alphanumeric characters
df["phone"] = df["phone"].str.replace(r"[^\d]", "", regex=True)

# Extract patterns
df["zip"] = df["address"].str.extract(r"(\d{5})")
df["domain"] = df["email"].str.extract(r"@(.+)$")

# Replace multiple values
df["status"] = df["status"].str.replace(
    r"^(cancelled|canceled)$", "cancelled", regex=True, case=False
)

# Standardize categories
status_map = {
    "active": "active", "Active": "active", "ACTIVE": "active",
    "inactive": "inactive", "Inactive": "inactive",
}
df["status"] = df["status"].map(status_map).fillna(df["status"])

# Check for non-printable / hidden characters
df["name"].str.contains(r"[\x00-\x1f\x7f]", regex=True).sum()
```

## Outlier Treatment

```python
import numpy as np

def iqr_clip(series, factor=1.5):
    """Clip to IQR bounds."""
    q1, q3 = series.quantile([0.25, 0.75])
    iqr = q3 - q1
    return series.clip(lower=q1 - factor * iqr, upper=q3 + factor * iqr)

# Clip numeric columns
for col in ["amount", "duration"]:
    df[col] = iqr_clip(df[col])

# Winsorize at percentile bounds
lo, hi = df["value"].quantile([0.01, 0.99])
df["value"] = df["value"].clip(lo, hi)

# Remove extreme outliers (Z-score)
from scipy import stats
z = np.abs(stats.zscore(df["revenue"].dropna()))
df = df.loc[df.index.isin(df["revenue"].dropna().index[z < 4])]
```

## Inconsistency Correction

```python
# Date ordering: end must be >= start
bad = df["end_date"] < df["start_date"]
print(f"Invalid date ranges: {bad.sum()}")
df.loc[bad, ["start_date", "end_date"]] = (
    df.loc[bad, ["end_date", "start_date"]].values  # swap
)

# Numeric range constraints
df["age"] = df["age"].clip(lower=0, upper=120)
df["pct"] = df["pct"].clip(lower=0.0, upper=1.0)

# Cross-column validation
inconsistent = (df["status"] == "shipped") & df["ship_date"].isnull()
df.loc[inconsistent, "ship_date"] = df.loc[inconsistent, "created_at"]
```

## Index Management

```python
# Reset index after filtering
df = df.reset_index(drop=True)

# Set meaningful index
df = df.set_index("id")

# Sort and reset
df = df.sort_values(["user_id", "timestamp"]).reset_index(drop=True)
```

## Renaming & Column Cleanup

```python
# Rename specific columns
df = df.rename(columns={"qty": "quantity", "amt": "amount"})

# Normalize all column names (snake_case)
import re
df.columns = [re.sub(r"\W+", "_", c).strip("_").lower() for c in df.columns]

# Drop useless columns
df = df.drop(columns=["unnamed_0", "redundant_col"], errors="ignore")

# Reorder columns
priority_cols = ["id", "user_id", "timestamp"]
remaining = [c for c in df.columns if c not in priority_cols]
df = df[priority_cols + remaining]
```

## Encoding Categorical Variables

```python
# Ordinal encoding
size_order = {"S": 0, "M": 1, "L": 2, "XL": 3}
df["size_enc"] = df["size"].map(size_order)

# One-hot encoding
dummies = pd.get_dummies(df["status"], prefix="status", drop_first=True, dtype="int8")
df = pd.concat([df, dummies], axis=1)

# Using sklearn for ML pipelines
from sklearn.preprocessing import OrdinalEncoder, LabelEncoder
```

## Copy-on-Write

CoW is always enabled in pandas >= 3.0 (opt-in in 2.x). Key implications:
- **Never use `inplace=True`** — use assignment instead
- Method chaining is fully safe and preferred
- Slices return views that copy on mutation (predictable behavior)

```python
# PREFERRED: Method chaining
df = (df
    .dropna(subset=["id"])
    .drop_duplicates()
    .assign(amount=lambda x: x["amount"].fillna(0))
    .reset_index(drop=True)
)
```

## Cleaning Pipeline Template

```python
def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    return (df
        # 1. Drop fully-empty rows/cols
        .dropna(how="all")
        .dropna(axis=1, how="all")
        # 2. Remove duplicates
        .drop_duplicates()
        # 3. Normalize column names
        .rename(columns=lambda c: re.sub(r"\W+", "_", c).strip("_").lower())
        # 4. Convert types
        .assign(
            created_at=lambda x: pd.to_datetime(x["created_at"], errors="coerce"),
            amount=lambda x: pd.to_numeric(x["amount"], errors="coerce"),
            category=lambda x: x["category"].astype("category"),
        )
        # 5. Fill known defaults
        .assign(status=lambda x: x["status"].fillna("unknown"))
        # 6. Reset index
        .reset_index(drop=True)
    )
```
