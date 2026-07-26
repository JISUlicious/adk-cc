# Pandas Data Visualization — Expert Skill

You are an expert data analyst. **Plotly is the default visualization
library.** Interactive charts (hover, zoom, click-filter, exportable to
self-contained HTML) are markedly more compelling for stakeholders than
static PNGs, and they cost nothing in code complexity.

Use **matplotlib + seaborn only when** you need:
- Print-ready static figures for publications or papers
- Slide decks where HTML embedding is impossible
- Tight control over a custom layout the Plotly grammar can't express
- Server-side image generation in environments without a browser

For everything else — exploratory analysis, dashboards, internal reports,
notebooks, web apps — Plotly is the right call.

## Plotly — Default Recipes

```python
import plotly.express as px
import plotly.graph_objects as go
```

### Line / Time Series

```python
# Single series
fig = px.line(df, x="date", y="revenue", title="Daily Revenue")
fig.show()

# Multiple series with color
fig = px.line(df, x="date", y="revenue", color="region",
              title="Revenue by Region", markers=False)
fig.update_layout(hovermode="x unified")           # show all series at hover
fig.show()

# With smoothed trend (rolling mean overlay)
df_long = df.melt(id_vars="date", value_vars=["revenue", "revenue_7d"],
                  var_name="series", value_name="value")
fig = px.line(df_long, x="date", y="value", color="series",
              title="Daily Revenue + 7-day Rolling Mean")
fig.show()
```

### Bar Charts

```python
# Vertical bar
fig = px.bar(
    df.groupby("category", as_index=False)["revenue"].sum(),
    x="category", y="revenue",
    title="Revenue by Category",
    text_auto=".2s",                               # auto-formatted labels
)
fig.show()

# Grouped bar (category × time)
pivot = (df.groupby(["region", "quarter"])["revenue"].sum()
           .reset_index())
fig = px.bar(pivot, x="region", y="revenue", color="quarter",
             barmode="group", text_auto=".2s",
             title="Revenue by Region and Quarter")
fig.show()

# Stacked bar
fig = px.bar(pivot, x="region", y="revenue", color="quarter",
             barmode="stack", title="Revenue Composition by Region")
fig.show()

# Horizontal bar — best when many categories
top_products = (df.groupby("product", as_index=False)["revenue"].sum()
                  .nlargest(15, "revenue").sort_values("revenue"))
fig = px.bar(top_products, x="revenue", y="product",
             orientation="h", text_auto=".2s",
             title="Top 15 Products by Revenue")
fig.show()
```

### Scatter Plots

```python
# Scatter with hover, color, and size encodings
fig = px.scatter(
    df, x="ad_spend", y="revenue",
    color="segment", size="margin",
    hover_data=["product", "region", "date"],
    opacity=0.6,
    title="Ad Spend vs Revenue (size = margin)",
    trendline="ols",                               # built-in regression line
)
fig.show()

# Faceted scatter (small multiples)
fig = px.scatter(df, x="ad_spend", y="revenue", color="segment",
                 facet_col="region", facet_col_wrap=3,
                 title="Spend vs Revenue by Region")
fig.show()
```

### Distributions

```python
# Histogram
fig = px.histogram(df, x="revenue", nbins=50,
                   title="Revenue Distribution")
fig.show()

# Histogram by group (overlaid or stacked)
fig = px.histogram(df, x="revenue", color="segment",
                   nbins=40, barmode="overlay", opacity=0.6,
                   title="Revenue by Segment")
fig.show()

# Box plot by group
fig = px.box(df, x="segment", y="revenue", color="segment",
             points="outliers", title="Revenue Distribution by Segment")
fig.show()

# Violin plot — full distribution shape
fig = px.violin(df, x="region", y="revenue", color="region",
                box=True, points="all",
                title="Revenue Distribution by Region")
fig.show()
```

### Correlation Heatmap (replacement for sns.heatmap)

```python
corr = df[numeric_cols].corr().round(2)
fig = px.imshow(
    corr, text_auto=True, aspect="auto",
    color_continuous_scale="RdBu_r", color_continuous_midpoint=0,
    title="Feature Correlation Matrix",
)
fig.show()
```

### Geographic / Maps

```python
# Choropleth — country-level
country_rev = df.groupby("country", as_index=False)["revenue"].sum()
fig = px.choropleth(country_rev, locations="country",
                    locationmode="country names", color="revenue",
                    color_continuous_scale="Viridis",
                    title="Revenue by Country")
fig.show()

# Scatter on map (lat/lon points)
fig = px.scatter_geo(df, lat="lat", lon="lon", color="region", size="revenue",
                     hover_name="city", projection="natural earth")
fig.show()
```

### Treemap / Sunburst (hierarchical)

```python
fig = px.treemap(df, path=["region", "category", "product"],
                 values="revenue", color="margin",
                 color_continuous_scale="RdYlGn",
                 title="Revenue Hierarchy")
fig.show()

fig = px.sunburst(df, path=["region", "category"], values="revenue",
                  title="Revenue by Region → Category")
fig.show()
```

### Funnel / Conversion

```python
funnel_df = pd.DataFrame({
    "stage": ["Visit", "Sign-up", "Trial", "Paid"],
    "count": [100_000, 12_000, 4_000, 800],
})
fig = px.funnel(funnel_df, x="count", y="stage",
                title="Conversion Funnel")
fig.show()
```

### Time-series with annotations

```python
fig = px.line(df, x="date", y="metric", title="Daily Metric")
# Mark a known event
fig.add_vline(x="2024-06-15", line_dash="dash", line_color="red",
              annotation_text="Recipe v3 deploy", annotation_position="top")
# Highlight a window
fig.add_vrect(x0="2024-06-15", x1="2024-06-22",
              fillcolor="red", opacity=0.15, line_width=0)
fig.show()
```

### Saving / Sharing

```python
# Self-contained HTML — works in any browser, includes the data
fig.write_html("/tmp/chart.html", include_plotlyjs="cdn")

# Static export (requires `kaleido` — `uv pip install kaleido`)
fig.write_image("/tmp/chart.png", width=1200, height=700, scale=2)
fig.write_image("/tmp/chart.svg")                  # vector for slides
```

### Plotly Theme Defaults

```python
import plotly.io as pio

pio.templates.default = "plotly_white"             # cleaner background
# Other built-ins: "plotly_dark", "ggplot2", "seaborn", "simple_white"

# Per-figure overrides (use sparingly)
fig.update_layout(
    font_family="Inter, sans-serif",
    title_font_size=16,
    legend=dict(orientation="h", y=-0.15),
    margin=dict(l=40, r=20, t=60, b=40),
    hovermode="closest",
)
```

## Plotly Dashboard (Subplots)

When you need a multi-panel report-style figure, use Plotly's `make_subplots`
— still interactive, still single-HTML, no matplotlib needed:

```python
from plotly.subplots import make_subplots

fig = make_subplots(
    rows=2, cols=3,
    subplot_titles=("Weekly Revenue", "Revenue by Region",
                    "Margin Distribution", "Top 10 Products",
                    "Cohort Retention", "Spend vs Revenue"),
    specs=[[{"type": "scatter"}, {"type": "bar"},     {"type": "histogram"}],
           [{"type": "bar"},     {"type": "heatmap"}, {"type": "scatter"}]],
)

# Top-left: weekly revenue
weekly = df.set_index("date")["revenue"].resample("W").sum().reset_index()
fig.add_trace(go.Scatter(x=weekly["date"], y=weekly["revenue"], mode="lines"),
              row=1, col=1)

# Top-middle: revenue by region
region_rev = df.groupby("region", as_index=False)["revenue"].sum()
fig.add_trace(go.Bar(x=region_rev["region"], y=region_rev["revenue"]),
              row=1, col=2)

# Top-right: margin histogram
fig.add_trace(go.Histogram(x=df["margin"], nbinsx=40), row=1, col=3)

# Bottom-left: top 10 products
top10 = (df.groupby("product")["revenue"].sum()
           .nlargest(10).sort_values().reset_index())
fig.add_trace(go.Bar(x=top10["revenue"], y=top10["product"], orientation="h"),
              row=2, col=1)

# Bottom-middle: cohort retention heatmap
fig.add_trace(go.Heatmap(z=retention.values, x=retention.columns,
                         y=retention.index.astype(str),
                         colorscale="YlGn"), row=2, col=2)

# Bottom-right: scatter
fig.add_trace(go.Scatter(x=df["ad_spend"], y=df["revenue"], mode="markers",
                         marker=dict(size=4, opacity=0.3)),
              row=2, col=3)

fig.update_layout(height=900, width=1600, showlegend=False,
                  title_text="Executive Dashboard — Q1 2024", title_x=0.5)
fig.write_html("/tmp/dashboard.html", include_plotlyjs="cdn")
fig.show()
```

## Static Figures — Matplotlib & Seaborn

Use these only when the deliverable is a static image. The Plotly recipes
above will cover most needs.

```python
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
import seaborn as sns

# Professional style defaults (apply once at the top of the script)
plt.rcParams.update({
    "figure.dpi": 150,
    "font.family": "sans-serif",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.3,
    "axes.labelsize": 12, "axes.titlesize": 14,
    "legend.fontsize": 10,
})
sns.set_theme(style="whitegrid")
```

### Matplotlib quick patterns

```python
# Line
df.set_index("date")["revenue"].plot(title="Daily Revenue", figsize=(12, 4))
plt.tight_layout(); plt.savefig("revenue.png", dpi=300, bbox_inches="tight")

# Bar
(df.groupby("category")["revenue"].sum().sort_values()
   .plot(kind="barh", figsize=(8, 6)))
plt.title("Revenue by Category")
plt.tight_layout(); plt.savefig("by_category.png", dpi=300, bbox_inches="tight")

# Histogram + KDE
fig, ax = plt.subplots(figsize=(10, 5))
df["revenue"].plot(kind="hist", bins=50, density=True, alpha=0.5, ax=ax)
df["revenue"].plot(kind="kde", ax=ax, color="red", linewidth=2)
plt.tight_layout(); plt.savefig("dist.png", dpi=300, bbox_inches="tight")

# Box plot by group
df.boxplot(column="revenue", by="segment", figsize=(10, 6))
plt.suptitle("")  # remove auto-added title
plt.tight_layout(); plt.savefig("box.png", dpi=300, bbox_inches="tight")
```

### Seaborn for statistical figures

```python
# Correlation heatmap (publication style)
corr = df[numeric_cols].corr()
fig, ax = plt.subplots(figsize=(10, 8))
sns.heatmap(corr, annot=True, fmt=".2f", cmap="coolwarm",
            center=0, square=True, linewidths=0.5, ax=ax)
plt.title("Feature Correlation Matrix")
plt.tight_layout(); plt.savefig("corr.png", dpi=300, bbox_inches="tight")

# Pairplot
sns.pairplot(df[["revenue", "cost", "margin", "segment"]],
             hue="segment", plot_kws={"alpha": 0.4})
plt.savefig("pairplot.png", dpi=300, bbox_inches="tight")

# Regression with confidence band
sns.regplot(data=df, x="ad_spend", y="revenue", scatter_kws={"alpha": 0.3})

# Faceted small multiples
g = sns.FacetGrid(df, col="region", row="quarter", height=4)
g.map(sns.histplot, "revenue", bins=20)
g.savefig("facets.png", dpi=300, bbox_inches="tight")
```

### Static dashboard (matplotlib subplots)

```python
fig, axes = plt.subplots(2, 3, figsize=(18, 10))
fig.suptitle("Executive Dashboard — Q1 2024", fontsize=16, y=1.02)
df.set_index("date")["revenue"].resample("W").sum().plot(ax=axes[0, 0])
df.groupby("region")["revenue"].sum().sort_values().plot(kind="barh", ax=axes[0, 1])
df["margin"].plot(kind="hist", bins=40, ax=axes[0, 2])
df.groupby("product")["revenue"].sum().nlargest(10).plot(kind="barh", ax=axes[1, 0])
sns.heatmap(retention.head(6), annot=True, fmt=".0%", ax=axes[1, 1], cmap="YlGn")
axes[1, 2].scatter(df["ad_spend"], df["revenue"], alpha=0.2, s=10)
plt.tight_layout()
plt.savefig("dashboard.png", dpi=150, bbox_inches="tight")
```

### Y-axis formatters

```python
def format_yaxis_millions(ax):
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda x, _: f"${x/1e6:.1f}M"))

def format_yaxis_pct(ax):
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(xmax=1, decimals=0))
```

## Visualization Best Practices

- **Default to Plotly.** Stakeholders interact with hover and zoom; static
  PNGs feel inert by comparison.
- **Choose the right chart:** line for trends, bar for comparison, scatter
  for correlation, histogram for distribution, box/violin for distribution
  comparison across groups, heatmap for matrix data, treemap for
  hierarchical proportions.
- **Label everything:** title, axis labels, units, data source. Plotly
  hover labels reduce the need for in-figure annotations but don't replace
  axis labels.
- **Color intentionally:** sequential (blues / Viridis) for quantity,
  diverging (RdBu) for deviation around a midpoint, qualitative (Tab10)
  for categories. Plotly's `color_continuous_midpoint=0` for diverging
  scales is essential when data spans positive and negative.
- **Avoid chartjunk:** no 3D effects, no gratuitous gradients. Plotly's
  `plotly_white` template strips most defaults.
- **Accessibility:** use colorblind-safe palettes (`px.colors.qualitative.Safe`,
  Plotly's "Viridis", or seaborn "colorblind"). Avoid red/green only.
- **Save HTML for sharing:** `fig.write_html(..., include_plotlyjs="cdn")`
  produces a self-contained file under 50 KB that works in any browser.
- **Never auto-open** dozens of `fig.show()` calls in a script — write to
  files instead, then list the paths in the report.
