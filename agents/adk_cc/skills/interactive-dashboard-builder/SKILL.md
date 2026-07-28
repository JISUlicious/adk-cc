---
name: interactive-dashboard-builder
description: >
  Build a self-contained interactive HTML dashboard from a real dataset —
  chart types chosen for the question, filters that work offline, and a rendered
  check before it is called done. Use for reporting, metric monitoring, or
  "make me a dashboard from this data".
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["the dashboard was opened/rendered and the charts contain real data points, not placeholders", "every figure comes from the dataset in this turn — no invented sample data", "the file is self-contained (no external CDN/network fetch at view time)", "each chart's type is justified by the question it answers"]}
---

# Interactive dashboard builder

A dashboard is a set of answers, not a wall of charts. Start from the questions
the reader will ask on Monday morning, then build the smallest set of views that
answers them.

Python runs on adk-cc's uv-managed interpreter; `plotly` is in the `core` tier.
Write output into the workspace (e.g. `analysis/dashboard.html`) — interactive
Plotly HTML renders directly in the adk-cc UI.

## Workflow

### 1. Questions first
Ask for (or state) the three to five questions this must answer, who reads it,
and how often. "Show me the data" is not a question; "is churn rising, and in
which segment?" is. Write the questions at the top of the dashboard itself so
its purpose survives contact with new readers.

### 2. Load the real data and state its shape
```python
import pandas as pd
df = pd.read_csv("<path>")          # parquet/xlsx equally fine
print(df.shape, df.dtypes.to_dict())
print(df.isna().sum().loc[lambda s: s > 0])
```
Report rows in, rows used, and what was dropped. **Never** generate sample data
to "show the layout" — a dashboard with invented numbers is indistinguishable
from a real one at a glance, and that is precisely the danger.

### 3. Choose chart types by the question
| Question | Chart |
|---|---|
| How does it change over time? | line (multi-series if comparing) |
| How do categories compare? | horizontal bar, sorted by value |
| What is the distribution? | histogram or box; never a bar of means alone |
| Does A relate to B? | scatter, with a trend line only if it is defensible |
| What makes up the whole? | stacked bar (prefer over pie beyond ~4 slices) |
| Where is it concentrated? | heatmap |

Rules that keep it honest: start bar axes at zero; label units; state the
date range; show n; do not use dual y-axes to imply a relationship.

### 4. Build it self-contained
```python
import plotly.graph_objects as go
from plotly.subplots import make_subplots
fig.write_html("analysis/dashboard.html", include_plotlyjs="inline")
```
`include_plotlyjs="inline"` is not optional — a CDN-linked dashboard breaks the
moment it is opened offline, emailed, or viewed inside a sandboxed preview.

Interactivity that earns its complexity: hover detail, legend toggling, range
selectors on time axes, and dropdown filters via `updatemenus`. Skip anything
that needs a server.

### 5. Verify it rendered — do not skip this
```bash
python -c "
import re,sys; h=open('analysis/dashboard.html').read()
print('size', len(h)); print('plotly inlined', 'plotly' in h[:200000] and 'cdn' not in h.lower()[:5000])
print('data points', len(re.findall(r'\"x\":\\[', h)))"
```
Then open it in the UI preview and look at it. A dashboard that fails to render,
or renders with empty axes, is the normal failure — and it is invisible unless
you check.

## Output

The HTML file, plus a short note: what each view answers, the data window, row
counts, and any caveat a reader would otherwise misread (sampling, a partial
final period, a metric definition that is not obvious).
