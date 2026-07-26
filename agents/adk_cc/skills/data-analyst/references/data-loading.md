# Pandas Data Loading & IO — Expert Skill

You are an expert data analyst with deep knowledge of pandas >= 2.3 IO tools.
Apply the following knowledge precisely when loading or saving data.

## Core Reader Functions

### CSV / Flat Files
```python
# Minimal – fast C engine (default)
df = pd.read_csv("file.csv")

# Production-grade: specify dtypes, parse dates, handle NA, chunking
df = pd.read_csv(
    "file.csv",
    dtype={"id": "int32", "category": "category"},
    parse_dates=["timestamp"],
    na_values=["NA", "N/A", "-", ""],
    usecols=["id", "name", "value", "timestamp"],  # only load needed cols
    chunksize=None,          # set e.g. 100_000 for large files
    engine="c",              # "c" (fast) | "python" (feature-rich) | "pyarrow"
    low_memory=False,        # avoids mixed-type columns in C engine
)

# Large file: iterate chunks
chunks = []
for chunk in pd.read_csv("big.csv", chunksize=100_000):
    chunks.append(chunk.query("value > 0"))
df = pd.concat(chunks, ignore_index=True)
```

Key parameters:
- `sep` / `delimiter` — delimiter character (default `,`)
- `header` — row number(s) to use as column names (0 = first row)
- `index_col` — column(s) to use as row index
- `skiprows` / `nrows` — skip leading rows; limit rows read
- `thousands` / `decimal` — locale-aware number parsing
- `encoding` — "utf-8", "latin-1", "utf-8-sig" (BOM), etc.
- `compression` — "gzip", "bz2", "zip", "xz", "zstd" (auto-detected)

### Excel
```python
# Single sheet
df = pd.read_excel("data.xlsx", sheet_name="Sales", engine="openpyxl")

# Multiple sheets → dict of DataFrames
sheets = pd.read_excel("data.xlsx", sheet_name=None)  # all sheets
first_two = pd.read_excel("data.xlsx", sheet_name=[0, 1])

# Write
with pd.ExcelWriter("out.xlsx", engine="openpyxl", mode="w") as w:
    df1.to_excel(w, sheet_name="Summary", index=False)
    df2.to_excel(w, sheet_name="Detail", index=False)
```

### JSON
```python
df = pd.read_json("data.json", orient="records", dtype={"id": int})
# orient options: "records", "split", "index", "columns", "values", "table"

df.to_json("out.json", orient="records", lines=True, date_format="iso")
```

### Parquet (recommended for columnar storage)
```python
# Requires pyarrow or fastparquet
df = pd.read_parquet("data.parquet", engine="pyarrow")
df = pd.read_parquet("data.parquet", columns=["id", "value"])  # column pruning

df.to_parquet("out.parquet", engine="pyarrow", compression="snappy", index=False)
```

Parquet is the best format for:
- Columnar access patterns (read only needed columns)
- Type fidelity (preserves dtypes exactly)
- Compression efficiency

### SQL
```python
import sqlalchemy as sa

engine = sa.create_engine("postgresql+psycopg2://user:pw@host/db")

# Full table
df = pd.read_sql_table("orders", con=engine)

# Custom query
df = pd.read_sql_query(
    "SELECT id, amount FROM orders WHERE status = 'complete'",
    con=engine,
    parse_dates=["created_at"],
    chunksize=50_000,
)

# Write back
df.to_sql("results", con=engine, if_exists="replace", index=False, method="multi")

# pandas 2.2+: ADBC drivers (much faster for Arrow-native DBs)
# pip install adbc-driver-postgresql
import adbc_driver_postgresql.dbapi as adbc
conn = adbc.connect("postgresql://user:pw@host/db")
df = pd.read_sql("SELECT * FROM orders", conn)
```

### HDF5
```python
df.to_hdf("store.h5", key="df", mode="w", complevel=9)
df = pd.read_hdf("store.h5", key="df")

# HDFStore for multiple datasets
with pd.HDFStore("store.h5") as store:
    store["sales"] = sales_df
    store["returns"] = returns_df
```

### Other formats
```python
pd.read_clipboard()          # paste from clipboard
pd.read_html("url")[0]       # first table from HTML page
pd.read_feather("file.feather")
pd.read_orc("file.orc")
pd.read_xml("file.xml", xpath=".//record")
pd.read_stata("file.dta")
pd.read_spss("file.sav")
```

## Dtype Optimization on Load

Always specify dtypes to avoid silent object-column fallback:

```python
dtype_map = {
    "id": "int32",
    "user_id": "int32",
    "amount": "float32",
    "status": "category",
    "flag": "boolean",      # nullable boolean (pandas 1.0+)
    "name": "string",       # Arrow-backed string (pandas 2.0+)
}
df = pd.read_csv("data.csv", dtype=dtype_map)
```

Use `category` dtype for low-cardinality string columns — saves memory 10–100x.

## PyArrow-Backed dtypes (pandas 2.0+)

```python
# Load entire CSV with PyArrow engine for Arrow-native types
df = pd.read_csv("data.csv", engine="pyarrow", dtype_backend="pyarrow")

# Or convert post-load
df = df.convert_dtypes(dtype_backend="pyarrow")
```

## Validation After Load

Always validate immediately after loading:
```python
print(df.shape)           # rows × columns
print(df.dtypes)          # check all dtypes are as expected
print(df.isnull().sum())  # missing value counts per column
print(df.duplicated().sum())  # duplicate rows
print(df.head())
print(df.describe())
```

## Common Pitfalls & Fixes

| Problem | Fix |
|---|---|
| Mixed-type column loaded as `object` | Set `dtype=` explicitly or `low_memory=False` |
| Dates loaded as strings | Use `parse_dates=["col"]` |
| MemoryError on large file | Use `chunksize=` and process iteratively |
| Encoding error | Try `encoding="latin-1"` or `encoding_errors="replace"` |
| Slow SQL reads | Switch to ADBC driver or use `chunksize=` |
| Integers become float due to NaN | Use nullable int: `dtype="Int64"` (capital I) |
