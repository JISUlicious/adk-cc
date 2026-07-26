# Pandas Time Series Analysis — Expert Skill

You are an expert data analyst specializing in time series with pandas >= 2.3.
Apply the following techniques for any date/time-based analysis.

## Datetime Parsing and Setup

```python
# Parse from strings
df["date"] = pd.to_datetime(df["date"])
df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d %H:%M:%S")
df["date"] = pd.to_datetime(df["date"], utc=True)           # parse as UTC

# From Unix timestamp
df["date"] = pd.to_datetime(df["unix_ts"], unit="s")        # seconds
df["date"] = pd.to_datetime(df["unix_ts_ms"], unit="ms")    # milliseconds

# Date ranges
idx = pd.date_range(start="2024-01-01", end="2024-12-31", freq="D")
idx = pd.date_range(start="2024-01-01", periods=12, freq="ME")  # month-end
idx = pd.bdate_range(start="2024-01-01", end="2024-12-31")      # business days

# Period ranges (better for fiscal/calendar periods)
periods = pd.period_range(start="2024-01", periods=12, freq="M")
```

## DatetimeIndex — The Foundation

```python
# Set DatetimeIndex for time-based operations
df = df.set_index("date").sort_index()

# Partial string indexing (very powerful)
df["2024"]                          # all of 2024
df["2024-Q1"]                       # Q1 2024
df["2024-06"]                       # June 2024
df["2024-01-01":"2024-03-31"]       # date range slice

# Access index components
df.index.year
df.index.month
df.index.day
df.index.dayofweek                  # 0=Monday
df.index.is_month_end
df.index.quarter
```

## .dt Accessor on Series

```python
# Extract components
df["year"]    = df["date"].dt.year
df["month"]   = df["date"].dt.month
df["day"]     = df["date"].dt.day
df["hour"]    = df["date"].dt.hour
df["minute"]  = df["date"].dt.minute
df["weekday"] = df["date"].dt.day_name()      # "Monday", "Tuesday", ...
df["week"]    = df["date"].dt.isocalendar().week
df["quarter"] = df["date"].dt.quarter

# Boolean flags
df["is_weekend"]   = df["date"].dt.dayofweek >= 5
df["is_month_end"] = df["date"].dt.is_month_end
df["is_leap"]      = df["date"].dt.is_leap_year

# Rounding / truncation
df["hour_block"] = df["date"].dt.floor("h")
df["day"]        = df["date"].dt.normalize()        # midnight
df["week_start"] = df["date"].dt.to_period("W").dt.start_time
```

## Resampling — Time-Based GroupBy

```python
# Must have DatetimeIndex set
daily = df.set_index("date")

# Downsample: aggregate to lower frequency
monthly = daily["revenue"].resample("ME").sum()     # month-end
weekly  = daily["revenue"].resample("W-MON").sum()  # week ending Monday
hourly  = daily["price"].resample("h").ohlc()       # OHLC (finance)

# Multiple aggregations
monthly = daily["revenue"].resample("ME").agg(
    total=("sum"),
    average=("mean"),
    peak=("max"),
    transactions=("count"),
)

# Upsample: fill to higher frequency
upsampled = monthly.resample("D").ffill()           # forward fill
upsampled = monthly.resample("D").interpolate()     # interpolate

# Common frequency aliases
# "D"   = calendar day
# "B"   = business day
# "W"   = week (Sunday)
# "W-MON" = week ending Monday
# "ME"  = month-end (pandas 2.2+; was "M")
# "MS"  = month-start
# "QE"  = quarter-end
# "QS"  = quarter-start
# "YE"  = year-end
# "h"   = hour (pandas 2.2+; was "H")
# "min" = minute (pandas 2.2+; was "T")
# "s"   = second
```

**Note:** Pandas 2.2+ deprecated uppercase aliases ("M", "H", "T", "S").
Use lowercase equivalents ("ME", "h", "min", "s") or suffixed forms.

## Rolling Windows

```python
# Fixed window
df["revenue_7d_avg"] = df["revenue"].rolling(window=7).mean()
df["revenue_30d_sum"] = df["revenue"].rolling(window=30).sum()
df["revenue_7d_std"] = df["revenue"].rolling(window=7).std()

# Minimum required observations (avoids NaN at start)
df["ma7"] = df["revenue"].rolling(window=7, min_periods=3).mean()

# Center the window (good for smoothing)
df["centered_ma"] = df["revenue"].rolling(window=7, center=True).mean()

# Time-based rolling (requires DatetimeIndex)
df["7d_avg"] = df["revenue"].rolling("7D").mean()
df["30d_avg"] = df["revenue"].rolling("30D").mean()

# Custom rolling aggregation
df["roll_iqr"] = df["value"].rolling(30).apply(
    lambda x: x.quantile(0.75) - x.quantile(0.25), raw=False
)
```

## Expanding Windows

```python
# All data from start to current row
df["cumulative_sum"]  = df["revenue"].expanding().sum()
df["cumulative_mean"] = df["revenue"].expanding().mean()
df["cumulative_max"]  = df["revenue"].expanding().max()
df["running_total"]   = df["revenue"].cumsum()
df["running_max"]     = df["revenue"].cummax()
```

## Exponentially Weighted (EWM)

```python
# Exponentially weighted moving average (more recent = more weight)
df["ewma_span10"] = df["price"].ewm(span=10, adjust=False).mean()
df["ewma_com5"]   = df["price"].ewm(com=5).mean()
df["ewma_alpha"]  = df["price"].ewm(alpha=0.3).mean()

# Parameters (only use one):
# span    — N-period equivalent
# com     — center-of-mass (com = (span-1)/2)
# halflife — half-life in periods or time offset
# alpha   — smoothing factor [0,1] (higher = more recent data weight)
```

## Lag and Lead Features

```python
# Lag (previous values) — critical for time series modeling
df["revenue_lag1"]  = df["revenue"].shift(1)     # previous period
df["revenue_lag7"]  = df["revenue"].shift(7)     # 1 week ago
df["revenue_lag30"] = df["revenue"].shift(30)    # 1 month ago

# Lead (future values) — use only in retrospective analysis
df["revenue_lead1"] = df["revenue"].shift(-1)

# Period-over-period change
df["mom_change"] = df["revenue"] - df["revenue"].shift(1)      # absolute
df["mom_pct"]    = df["revenue"].pct_change(1)                 # percentage
df["yoy_pct"]    = df["revenue"].pct_change(12)                # year-over-year (monthly data)

# Log returns (finance)
import numpy as np
df["log_return"] = np.log(df["price"] / df["price"].shift(1))
```

## Timezone Handling

```python
# Localize naive timestamps
df["ts"] = df["ts"].dt.tz_localize("UTC")
df["ts"] = df["ts"].dt.tz_localize("America/New_York")

# Convert between timezones
df["ts_eastern"] = df["ts"].dt.tz_convert("America/New_York")
df["ts_london"]  = df["ts"].dt.tz_convert("Europe/London")

# Strip timezone (normalize to UTC first, then remove)
df["ts_naive"] = df["ts"].dt.tz_convert("UTC").dt.tz_localize(None)
```

## Missing Timestamps & Gaps

```python
# Find missing dates in a daily series
full_range = pd.date_range(start=df.index.min(), end=df.index.max(), freq="D")
missing = full_range.difference(df.index)
print(f"Missing dates: {len(missing)}")

# Reindex to fill gaps
df = df.reindex(full_range)
df["value"] = df["value"].ffill()   # forward-fill gaps

# Detect gaps larger than expected
gaps = df.index.to_series().diff().dropna()
large_gaps = gaps[gaps > pd.Timedelta("2D")]
print(large_gaps)
```

## Seasonality & Trend Decomposition

```python
# Using statsmodels (separate package)
from statsmodels.tsa.seasonal import seasonal_decompose

result = seasonal_decompose(df["revenue"], model="additive", period=12)
# Access: result.trend, result.seasonal, result.resid

# STL decomposition (robust)
from statsmodels.tsa.seasonal import STL
stl = STL(df["revenue"], period=12, robust=True).fit()
df["trend"]    = stl.trend
df["seasonal"] = stl.seasonal
df["residual"] = stl.resid
```

## Time Series Aggregation Patterns

```python
# Daily → monthly revenue with business day count
daily = df.set_index("date")
monthly = daily.resample("ME").agg(
    revenue=("revenue", "sum"),
    avg_daily=("revenue", "mean"),
    trading_days=("revenue", "count"),
    peak_day=("revenue", "max"),
)

# Week-over-week growth
monthly["wow_growth"] = monthly["revenue"].pct_change(1)

# Year-over-year comparison
monthly["revenue_ly"] = monthly["revenue"].shift(12)   # last year (monthly)
monthly["yoy"] = (monthly["revenue"] / monthly["revenue_ly"] - 1)
```

## Practical Time Series Checklist

- [ ] Parse dates with `pd.to_datetime()`, verify no NaT after conversion
- [ ] Sort by datetime index before any resampling or rolling operations
- [ ] Handle timezone explicitly (always store in UTC, display in local time)
- [ ] Check for gaps / missing periods with `date_range.difference(df.index)`
- [ ] Use `min_periods=` in rolling to avoid leading NaN bloat
- [ ] Verify resampling frequency alias is pandas 2.2+ compatible (lowercase)
- [ ] Lag features must use `.shift()` — never look-ahead with shift(-n) in training
