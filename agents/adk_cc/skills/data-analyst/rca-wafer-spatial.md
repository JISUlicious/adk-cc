# Spatial Defect Pattern Analysis (Wafer Maps)

For manufacturing defect data with (x, y) location. **The spatial pattern
often reveals the physical cause faster than any commonality table** —
an edge ring implicates handling / edge-bead removal, a repeating step
pattern implicates the stepper, a chamber-centered hotspot implicates
non-uniformity in the deposition step.

## Contents

- When applicable (requires x, y coordinates)
- 2D kernel density estimation
- Ripley's K function (cluster vs. random)
- Pattern taxonomy (edge / center / radial / stepper / random / stripe)
- Interpretation guide (pattern → likely physical cause)
- Synthetic wafer example

Read this file when working on: wafer-map defect analysis, spatial
inspection data, "why do all our failures cluster on one side of the
wafer?", or any semi manufacturing defect investigation where (x, y)
coordinates are recorded. This methodology also applies to PCB defect
maps, screen coating defects, and any other 2D-coordinate defect data.

## When applicable

Requires each defect to have (x, y) coordinates on the substrate. If
your defect log only has per-lot or per-wafer aggregates (SECOM-style),
this file's methods don't apply — use `rca-commonality.md` instead.

Typical schema:
```
lot_id | wafer_id | defect_id | x_mm | y_mm | defect_class | detected_at
```

## 2D kernel density estimation

The fastest way to see whether defects cluster is to overlay a KDE on
the wafer diagram.

```python
import numpy as np
from scipy.stats import gaussian_kde
import plotly.graph_objects as go

pts = df.loc[df["defect_class"] == "particle", ["x_mm", "y_mm"]].values.T
kde = gaussian_kde(pts, bw_method="scott")

# Evaluate on a 200x200 grid, restricted to the wafer disc
r_wafer_mm = 150
gx, gy = np.mgrid[-r_wafer_mm:r_wafer_mm:200j, -r_wafer_mm:r_wafer_mm:200j]
inside = (gx**2 + gy**2) < r_wafer_mm**2
density = np.where(inside, kde(np.vstack([gx.ravel(), gy.ravel()])).reshape(gx.shape), np.nan)

fig = go.Figure(go.Heatmap(x=gx[:, 0], y=gy[0], z=density.T, colorscale="Viridis"))
fig.add_scatter(x=pts[0], y=pts[1], mode="markers",
                marker=dict(color="white", size=3, opacity=0.5))
fig.update_layout(title="Defect KDE — 150 mm wafer",
                  xaxis_title="x (mm)", yaxis_title="y (mm)",
                  yaxis_scaleanchor="x", width=600, height=600)
```

Use log density scaling for skewed patterns and mask outside the wafer
disc (set to NaN) — otherwise the KDE bleeds into non-existent regions.

## Ripley's K function — cluster vs. random test

KDE shows *where* defects concentrate; Ripley's K tests whether the
overall pattern differs from complete spatial randomness (CSR). If the
observed K exceeds the CSR envelope, the pattern is clustered at that
radius; if below, it's regular. Prefer the variance-stabilized
**L(r) − r** (0 under CSR) over raw K — it's easier to read against the
envelope.

**Two corrections are mandatory on a wafer, or you'll misread the plot:**

1. **Edge correction.** The wafer is a bounded disc, so points near the
   rim have fewer neighbours *purely from the boundary* — uncorrected K
   is biased downward and mimics "regularity". Use an isotropic
   (Ripley) edge correction.
2. **Right null region.** Generate the CSR envelope over the **wafer
   disc**, not the bounding box, or the envelope itself is wrong.

```python
# pointpats/astropy provide implementations; sketch with edge correction:
from pointpats import ripley
# support in mm; hull = the wafer disc, not the point bounding box
k_obs, radii = ripley.k_estimate(pts.T, support=np.linspace(0, 50, 50),
                                 edge_correction="ripley")
env_lo, env_hi = ripley.k_envelope(pts.T, n_permutations=99, hull="disc")
L_minus_r = np.sqrt(k_obs / np.pi) - radii     # variance-stabilized
```

**CSR is a *naive* null for wafers.** Real wafers have structure from die
layout, edge-exclusion zones, and reticle fields, so a CSR rejection is
expected even for a healthy process. Treat departures from CSR as a
*screen* that sends you to the pattern taxonomy below — not as proof of
a special cause.

A wafer showing clustering at r=5-15 mm suggests a localized process
event (a fixture scratch, a nozzle plug). Clustering at r > 50 mm
suggests a global gradient (temperature, film thickness).

## Pattern taxonomy

Wafer patterns fall into a small vocabulary. Recognizing the shape gives
you the physical cause candidate before you run any test.

| Pattern | Likely physical cause |
|---|---|
| **Edge ring** — dense defects at r ≈ r_wafer | Handling damage / edge-bead removal / rinse dispense / edge exclusion misconfigured |
| **Center dense** — hot spot at (0, 0) | Chamber center non-uniformity; deposition or etch rate peaks at center |
| **Radial gradient** — density varies smoothly with r | Rotation-related process (spin coat, CMP); check platen or chuck concentricity |
| **Repeating scan pattern** — periodic hot spots on a rectangular grid | Stepper / lithography exposure fault; shot-level defect |
| **Random / uniform** — CSR envelope contains K̂(r) | Yield floor / random particle background; no localized cause |
| **Cluster / stripe** — one dense cluster or linear streak | Fixture damage, scratch, cassette-transfer contact |

**Diagnostic sequence:**
1. Plot the KDE and eyeball the pattern.
2. Match against the taxonomy above.
3. Run Ripley's K to confirm clustering statistically.
4. Cross-reference the pattern with the process history: which step in
   the flow has the geometry that matches the pattern?

## Synthetic wafer example

When (x, y) data isn't available for the current investigation, generate
a synthetic wafer to calibrate the method:

```python
# 200 defects with a center-hot pattern + 20 uniform-random background
r_wafer = 150
rng = np.random.default_rng(0)

center = rng.normal(loc=(0, 0), scale=25, size=(180, 2))
uniform_r = r_wafer * np.sqrt(rng.random(20))
uniform_th = 2 * np.pi * rng.random(20)
uniform = np.column_stack([uniform_r * np.cos(uniform_th),
                           uniform_r * np.sin(uniform_th)])
pts = np.vstack([center, uniform])
pts = pts[(pts**2).sum(axis=1) < r_wafer**2].T   # clip to wafer disc

# ... run the KDE + Ripley pipeline above
```

The KDE should recover the center-dense signature and Ripley's K should
exceed the CSR envelope at r < 30 mm. Use this as a smoke test whenever
integrating a new spatial data source.
