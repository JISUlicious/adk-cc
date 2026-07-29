# RCA Reporting — Tier 2 Template + HTML Output

The deliverable is a **causal narrative** with evidence, not a SHAP plot.
This file covers the reporting artifact: template, writing discipline,
and self-contained HTML rendering.

## Contents

- § 7: Tier 2 RCA report template (calibration reference)
- § 7a: Writing the Tier 2 report — form guide + discipline
  - Prereq checklist
  - Section-by-section discipline
  - Cross-domain slot mapping
  - Length budget
  - Quality checklist (pre-ship)
  - Three failure modes
- § 7b: Producing the HTML report — self-contained file spec
  - When HTML vs. markdown
  - Output spec
  - CSS block (verbatim)
  - HTML element per section
  - Generation approach — per-task script
  - Rendered example
  - Quality checklist (HTML-specific)
  - Three failure modes (HTML-specific)

Read this file when working on: writing a Tier 2 RCA report, producing
the HTML deliverable for stakeholders, or auditing a report for the
discipline gaps that turn one into a "Findings memo". Level-1 references
from `SKILL.md`: `root-cause-analysis.md` §§ 1–2, `rca-commonality.md`,
`rca-causal-analysis.md`.

## 7. RCA Reporting Template

A SHAP table is not an RCA report. The deliverable is a **causal
narrative** with evidence:

```
## Excursion: <metric> shifted from <old> to <new> on <date>

### 1. Timeline
- Detection: <when first noticed>
- Estimated change point: <date> (PELT, BIC penalty, MW p < 0.01,
  pre n=200 / post n=180)
- Plot of metric over time with change point marked.

### 2. Candidate causes (ranked by evidence strength)
| Candidate | Occurrence evidence | Escape evidence (why didn't detection catch it?) | Confounders to rule out |
|---|---|---|---|
| Recipe v3.2 deploy on T-2 | Lift=4.1, Fisher p=0.0003 | Shewhart X-bar on s406 insensitive to sub-3σ drift | Tool maintenance same day |
| Chamber 5 (Tool A) | Defect rate 12% vs. baseline 2% | No per-chamber alarm on this metric | Lot mix shift |
| New operator shift | Defect rate 8% vs. baseline 2% | No shift-level SPC | Confounded with night shift hardware |

### 3. Causal analysis
- DiD estimate (recipe v3.2): +5.2pp defect rate, 95% CI [3.1, 7.3]
- Refutation: random common cause Δ=+0.1, placebo Δ=−0.05 → robust
- Parallel-trends check: ✓ (plot in appendix)

### 4. Recommended action (D6 corrective + D7 preventive)
- **D6 corrective:** Roll back recipe v3.2 (highest evidence)
- **D7 preventive:** Switch s406 SPC chart to EWMA λ=0.2 (closes escape)
- Increase sampling on Chamber 5 for next 5 lots (secondary signal)
- Audit night-shift training (lowest priority — confounded)

### 5. Verification plan (D5)
- Power-planned n = 380 wafers post-fix (target Δ = 2pp, α=0.05, power=0.80)
- Success criteria: Δmean ≥ 2pp, post-σ ≤ pre-σ, 20 in-control subgroups,
  no WE rule violations. See `rca-d5-verification.md`.

### 6. What we cannot rule out
- Unobserved supplier-material drift in same week
- Chamber 5 fixture wear (no instrumentation)
```

## 7a. Writing the Tier 2 RCA report

The § 7 template above is the artifact. This subsection is the discipline for
producing it from any analysis result, in any domain.

### Prereq checklist

Copy this into your scratch space and check off. Skipping any item produces a
"Findings memo," not an RCA report — relabel and drop § Causal + § Recommended
action if a prereq fails.

```
RCA prereqs:
- [ ] Confirmed change point: p<0.01, ≥30 samples each side, visually clean
- [ ] Preprocessing log: nulls handled per column, collinear clusters collapsed
- [ ] Ranked top 3 candidate causes with strongest evidence number each
- [ ] Quantile (or per-level) breakdown of the #1 driver vs target
- [ ] Causal estimate for #1 (DiD / matching) with at least one refutation
- [ ] Concrete list of unmeasured variables that could be alternative causes
```

### Section-by-section discipline

Author sections in this order. Write TL;DR last.

| Section | What it enforces |
|---|---|
| **TL;DR** (1–2 sentences) | Commitment. One cause, one action. No hedging, no "or", no undated language. |
| **The change** | The shift is real, not noise. Requires method + p-value + n on each side + visual reference. |
| **Data & methods** (2 tables) | Auditable prep. Table columns: preprocessing → step / action / why; methods → question / method / why-this-one. |
| **Where the signal lives** | Actionable threshold. Quantile-bucket table of #1 driver + threshold sentence: "feature > value carries N× baseline rate." |
| **Top candidate causes** (≤3 rows) | Ranking + integrity. Every row has a Confounders column — empty column means no critical thinking happened. |
| **Causal analysis** (#1 only) | Correlation vs causation. Estimate + CI + ≥1 refutation. Applying to all three candidates dilutes the report. |
| **Recommended action** (numbered) | Something an operator can do. #1 must be reversible and high-evidence. "Investigate further" as #1 signals unfinished analysis. |
| **What we cannot rule out** (≥3 bullets) | Integrity. Empty section reads as overconfident; generic disclaimers ("data may be incomplete") don't count. |

### Cross-domain slot mapping

The template is the same; the nouns change.

| Slot | Manufacturing | Web product | Healthcare | Finance | SRE / ops |
|---|---|---|---|---|---|
| Metric | defect rate | conversion, churn | readmit, complication | default, fraud rate | error rate, p99 latency |
| Change agent | recipe, tool maintenance | feature flag, deploy, A/B | protocol, formulary | model version, policy | deploy, config push |
| Cohort | lot, wafer | user, session | patient, ward | account, transaction | request, host group |
| Driver shape | sensor reading | session duration | lab value, score | risk score, velocity | queue depth, GC pause |
| Causal lever | recipe rollback | flag rollback | protocol revert | model rollback | deploy revert |

### Length budget

- **Sweet spot:** 60–75 lines / 450–550 words.
- **Ceiling:** 90 lines / ~700 words. Above this, move detail to the appendix.
- **Floor:** 40 lines. Below this, a section is missing.

### Quality checklist (pre-ship)

- [ ] TL;DR's stated cause matches Recommended action #1 (same noun, same numbers)
- [ ] Every number in the body appears in the analysis log — no new numbers introduced in the report
- [ ] § Where the signal lives produces a concrete threshold sentence
- [ ] § Top candidates has ≤3 rows; every row has a confounder listed
- [ ] § Causal analysis covers #1 candidate only, with ≥1 refutation test
- [ ] § Recommended action #1 is reversible and high-evidence
- [ ] § Cannot rule out lists concrete gaps, not generic disclaimers
- [ ] TL;DR was written last, not first

### Three failure modes (and the fix)

1. **Report is a SHAP plot wrapped in prose** — no bucket table, no threshold, reader can't act. → Add § Where the signal lives with a quantile table + threshold sentence.
2. **All three candidates get causal analysis** — dilutes attention, reads as fishing. → Restrict § Causal to #1 only. Demote others to the appendix.
3. **Recommended action #1 is "investigate further"** — analysis didn't finish. → Either find a reversible first step (rollback, tighter threshold, increased sampling), or relabel the document "Findings memo" and drop § Causal + § Action.

## 7b. Producing the HTML report

For stakeholder delivery, print-to-PDF, or email attachment, produce the
Tier 2 report as a self-contained HTML file. Everything from § 7a stays;
this subsection covers rendering.

### When HTML vs. markdown

| Capability | Markdown | HTML |
|---|:---:|:---:|
| Interactive Plotly plots inline | ✗ | ✓ |
| Collapsible details / expandable sections | ✗ | ✓ |
| Print to PDF with page breaks | ~ | ✓ |
| Runs offline in any browser | ~ | ✓ |
| Copy-paste into email preserving format | ✗ | ✓ |

Use markdown for scratch analysis and internal notebook artifacts. Use HTML
for anything a stakeholder sees.

### Output spec

**One file. `.html`. Self-contained.**

- Single `<html>` document. No external CSS, no external JS beyond the Plotly CDN.
- Inline `<style>` block with the CSS below (verbatim; adjust palette only if brand-mandated).
- Plotly figures embedded via `fig.to_html(include_plotlyjs="cdn", full_html=False)` — first fragment loads the CDN; subsequent fragments use `include_plotlyjs=False`.
- Matplotlib fallback plots: base64 `<img src="data:image/png;base64,…">`.
- Zero JavaScript beyond Plotly. Report degrades gracefully to numbers+tables if Plotly won't run.
- File size target: < 500 KB. Above that, move heavy plots to `_appendix.html` linked from § Appendix.

### CSS block — verbatim

```css
:root {
  --fg: #1a1a1a; --muted: #6b6b6b; --bg: #ffffff;
  --border: #e5e5e5; --code-bg: #f7f7f7;
  --sev: #c62828; --ok: #2e7d32; --accent: #1565c0;
}
* { box-sizing: border-box; }
body {
  font: 14px/1.55 ui-sans-serif, -apple-system, system-ui, sans-serif;
  color: var(--fg); background: var(--bg);
  max-width: 880px; margin: 32px auto; padding: 0 24px;
}
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; padding-bottom: 4px;
     border-bottom: 1px solid var(--border); }
h3 { font-size: 14px; margin: 20px 0 6px; color: var(--muted); }
.meta { color: var(--muted); font-size: 13px; margin: 0 0 20px; }
.tldr { padding: 12px 16px; border-left: 3px solid var(--sev);
        background: #fff5f5; margin: 16px 0; }
.tldr strong { color: var(--sev); }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px;
        font-size: 13px; }
th, td { border-bottom: 1px solid var(--border); padding: 6px 10px;
         text-align: left; vertical-align: top; }
th { background: #fafafa; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code, pre { background: var(--code-bg); border-radius: 3px;
            font: 12.5px/1.4 ui-monospace, Menlo, monospace; }
code { padding: 1px 5px; }
pre { padding: 10px 12px; overflow-x: auto; }
.threshold { padding: 10px 14px; background: #fffde7;
             border-left: 3px solid #f9a825; margin: 8px 0; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 11px; font-weight: 600; text-transform: uppercase; }
.sev-high { background: var(--sev); color: white; }
.sev-med  { background: #ef6c00; color: white; }
.sev-low  { background: var(--muted); color: white; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--accent); font-weight: 500; }
aside { padding: 10px 14px; background: #f5f9ff;
        border-left: 3px solid var(--accent); margin: 8px 0; }
@media print {
  body { max-width: none; margin: 0; padding: 12mm; }
  h2 { page-break-before: auto; page-break-inside: avoid; }
  details { page-break-inside: avoid; }
  details:not([open]) summary::after { content: " (expand in appendix)"; }
}
```

### HTML element per section

Same 8 sections as § 7a. Element choice per section:

| Section | HTML element |
|---|---|
| Header | `<header>` with `<h1>` + severity `<span class="badge sev-…">` + `<p class="meta">` (author, date, source) |
| TL;DR | `<section class="tldr">` — colored left border, larger font |
| The change | `<section>` with `<dl>` (metric / direction / change point) + one embedded Plotly line chart with change-point vertical line |
| Data & methods | Two `<table>`s inside `<details><summary>` collapsibles (collapsed by default) |
| Where the signal lives | `<table>` for the quintile breakdown + `<p class="threshold">` for the threshold sentence + optional Plotly bar chart |
| Top candidate causes | `<table>` — 3 rows max, numbers in `<code>` |
| Causal analysis | `<section>` with an `<aside>` box for the estimate + CI; refutation as `<ul>` |
| Recommended action | `<ol>` with `<strong>` on action verbs |
| Cannot rule out | `<ul>` |
| Appendix | `<details>` collapsible list of links / embedded thumbnails |

Collapse only Data & methods + Appendix. Never collapse the top candidates,
the threshold sentence, or the recommended action — those must be visible on
the first screen.

### Generation approach — per-task script

Do not bundle a Python helper. Generate a per-analysis script that computes
the payload from your analysis artifacts, renders Plotly fragments, and
formats a single HTML file. Skeleton:

```python
# 1. Compute payload from analysis artifacts (one dict per section)
payload = {
    "metric": "defect_rate",
    "date": "2026-04-12",
    "severity": "high",              # → CSS class for the badge
    "tldr": "defect_rate jumped 2.1% → 10.5% at recipe v3.2 deploy. Roll back.",
    "before": 0.021, "after": 0.105, "delta_pp": 8.4,
    "changepoint": {"ts": "2026-04-12 14:00", "method": "PELT + BIC",
                    "test": "MW", "p": 0.001, "n_pre": 200, "n_post": 180},
    "prep_rows": [...],              # list of (step, action, why)
    "methods_rows": [...],           # list of (question, method, why)
    "quintile_rows": [...],          # list of (label, range, n, rate, delta)
    "threshold_sentence": "s406 > 6.8 → 4× baseline",
    "candidates": [...],             # list of (rank, name, evidence, confounders)
    "causal": {"est": +0.052, "ci_lo": +0.031, "ci_hi": +0.073,
               "refute_random": +0.001, "refute_placebo": -0.005,
               "refute_subset": +0.003, "parallel_ok": True},
    "actions": ["Roll back recipe v3.2 (~2h, reversible)", ...],
    "cannot_rule_out": ["Supplier material drift (no per-lot data)", ...],
}

# 2. Render Plotly fragments (CDN loaded on first fragment only)
fig1_html = fig_metric_over_time.to_html(include_plotlyjs="cdn",  full_html=False)
fig2_html = fig_quintile_bar.to_html(include_plotlyjs=False, full_html=False)

# 3. Assemble the file with an f-string template. Structure follows the
#    element mapping table above verbatim. Keep the template linear —
#    conditional branches deeper than one level become unreadable.
html = f"""<!doctype html><html lang="en"><head>
  <meta charset="utf-8"><title>RCA: {payload['metric']} on {payload['date']}</title>
  <style>{CSS}</style></head><body>
  <header>...</header>
  <section class="tldr">...</section>
  ...
</body></html>"""

# 4. Write to disk
from pathlib import Path
out = Path(f"reports/rca_{payload['metric']}_{payload['date']}.html")
out.parent.mkdir(exist_ok=True); out.write_text(html)
print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
```

**Payload separation pattern:** for reports that will regenerate under
multiple audiences (exec, audit, engineering), write the payload to a
sidecar `payload.json` and load it in the template script. One analysis +
one payload + one template → many rendered variants.

### Rendered example — the header fragment

Calibration reference. Top of a rendered report as HTML source:

```html
<header>
  <h1>RCA: defect_rate shifted on 2026-04-12
      <span class="badge sev-high">High</span></h1>
  <p class="meta">Author: J. Kim &middot; Generated 2026-05-19 &middot;
     Source: fab_metrics.parquet + mes_events.csv</p>
</header>

<section class="tldr">
  <strong>TL;DR.</strong> defect_rate jumped 2.1% → 10.5% at 14:00 on
  April 12, coinciding with recipe v3.2 deploy. DiD estimate +5.2pp
  (95% CI: +3.1 to +7.3), refutation passed.
  <strong>Recommend rollback.</strong>
</section>

<section>
  <h2>The change</h2>
  <dl>
    <dt>Metric</dt> <dd>defect_rate (50-lot rolling window)</dd>
    <dt>Direction</dt> <dd>2.1% → 10.5% (Δ = +8.4pp, 5× baseline)</dd>
    <dt>Change point</dt>
    <dd>2026-04-12 14:00 — PELT (BIC); MW p=0.001;
        pre n=200 / post n=180 </dd>
  </dl>
  <!-- Plotly fragment for metric-over-time with change-point vline -->
  {fig_metric_over_time_html}
</section>
```

### Quality checklist (HTML-specific, on top of § 7a)

- [ ] Opens correctly in Chrome, Safari, and Outlook preview
- [ ] Prints to 2–3 pages with sensible page breaks
- [ ] `<title>` matches `<h1>` (browser tab / email subject preview)
- [ ] Severity badge in header matches TL;DR tone
- [ ] Numbers in TL;DR match numbers in the body verbatim
- [ ] Every table is a real `<table>`, never a plot image of a table
- [ ] `<details>` collapsibles cover only Data & methods + Appendix
- [ ] Plotly `include_plotlyjs="cdn"` on the first figure only
- [ ] No absolute file paths in `<img src>` or `<a href>` (all base64 or relative)
- [ ] File size under 500 KB

### Three failure modes (HTML-specific)

1. **Regenerating the report to change one number requires editing the script.** → Separate `payload.json` from the template script.
2. **Plotly plots don't render in Outlook / Slack preview.** → Ship a `_static.html` companion with matplotlib PNGs base64-embedded.
3. **File grows past 500 KB with embedded plots.** → Move heavy plots to `_appendix.html` linked from § Appendix.
