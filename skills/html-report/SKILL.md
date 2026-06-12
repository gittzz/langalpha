---
name: html-report
description: "Self-contained styled HTML reports written to results/: PDF-exportable research documents with inline data, charts, and theme-aware CSS"
---

# HTML Report

Author a styled, self-contained HTML **document** and write it to `results/` (e.g. `results/report.html`). The file panel renders it with full browser semantics — JavaScript runs, CDN libraries load, relative assets resolve, and the user can view it fullscreen, open it in a new tab, download it, or export it to PDF.

This is the right output when the user wants a **deliverable they can keep, share, or print** — an equity research note, an earnings recap, a screen writeup — not a throwaway answer.

> **Read `skills/ui-design/SKILL.md` before authoring.** It defines the typography, color, and composition standards that keep the report looking like a research desk artifact rather than a generic AI page. This skill covers the mechanics; that one covers the taste.

## Decide: Which Output?

| Want | Use | Why |
|---|---|---|
| A document the user keeps, shares, or exports to PDF | **html-report** (this skill) — `.html` in `results/` | Lives on disk, served with real semantics, PDF-exportable via browser print |
| A quick visualization *inside the chat* (one chart, a metric row, a table) | **inline-widget** (`ShowWidget`) | Appears inline between text; no file, no panel |
| A live, interactive app (filtering, drill-downs, live refresh, multi-page) | **interactive-dashboard** (`GetPreviewUrl`) | A served running app, not a static file |
| A simple, short answer | **plain markdown** | A styled HTML document is overkill for a one-paragraph reply |

**Rule of thumb:** substantial, durable deliverable → html-report. Ephemeral in-chat figure → inline-widget. Running app → interactive-dashboard. Trivial answer → markdown.

## Self-Contained by Default

Write **one** complete HTML file. Everything inline — no external CSS/JS files, no build step.

```python
import json

data = {"labels": ["Q1", "Q2", "Q3", "Q4"], "revenue": [2.1, 2.4, 2.6, 3.0]}

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Acme Q4 Revenue Review</title>
  <style>/* all CSS inline here */</style>
</head>
<body>
  <main>...</main>
  <script>const DATA = {json.dumps(data, ensure_ascii=False)};</script>
  <script>/* render charts from DATA */</script>
</body>
</html>"""

with open("results/report.html", "w", encoding="utf-8") as f:
    f.write(html)
```

Rules:
- Full `<!DOCTYPE html>` document with `<head>`/`<body>` (unlike inline-widget, which is a bare fragment).
- **All CSS in a `<style>` block, all JS in `<script>` blocks** — nothing external except allowlisted CDN libraries.
- **Embed data via `<script>const DATA = {json.dumps(data, ensure_ascii=False)};</script>`** — never inline raw Python dicts, never `fetch()` a local file. `ensure_ascii=False` keeps non-ASCII (names, currencies, CJK) readable and correctly encoded.
- **Sample or aggregate large datasets** before embedding. A report doesn't need every tick — downsample to a sensible resolution, aggregate to the reporting period. Keep the embedded payload lean (target well under ~1MB).

Rationale: a single file means download / share / PDF / "open in new tab" all just work, with nothing to break.

## Multi-File When Warranted

The viewer serves files with **real relative-path semantics**, so a report can reference sibling assets and they resolve correctly:

```
results/
  report.html              # references charts/revenue.png as a relative path
  charts/
    revenue.png
    margins.png
```

```html
<img src="charts/revenue.png" alt="Quarterly revenue" style="width:100%;max-width:720px;">
```

Use multi-file for **image-heavy reports** — e.g. when you've generated high-quality static charts with matplotlib/plotly `savefig` and want to embed them rather than redraw client-side.

Rules:
- Keep all asset paths **relative** (`charts/revenue.png`, not `/results/...` and not absolute filesystem paths).
- Keep every asset **inside the workspace** and under `results/` (or a subdir of it). Do not reference files outside the workspace.
- Prefer self-contained when the charts can reasonably be drawn client-side from embedded `DATA`; reach for multi-file when raster images give materially better output.

## CDN Allowlist

Only these origins are reachable from the rendered document. Anything else (including arbitrary `fetch()`) is blocked.

- `cdnjs.cloudflare.com`
- `cdn.jsdelivr.net`
- `unpkg.com`
- `esm.sh`
- Google Fonts: `fonts.googleapis.com` + `fonts.gstatic.com`

Load chart libraries, fonts, and helpers from these only. Do not call out to data APIs from the document — embed the data instead.

## Theme Variables (Defensive Fallback Form)

The viewer can inject app `--color-*` variables so the report themes with light/dark mode. **Always author colors in the fallback form** so the document also renders correctly standalone, in a downloaded file, and in print:

```css
color: var(--color-text-primary, #1a1a1a);
background: var(--color-bg-card, #ffffff);
border: 1px solid var(--color-border-muted, #e4e1dc);
```

The literal fallback is what shows when no app vars are injected (downloaded file, PDF, plain open). Never write a bare `var(--color-x)` without a fallback, and never hardcode a color with no variable — both break one of the surfaces.

Reuse the same variable names as the inline-widget skill:

| Variable | Purpose | Suggested light fallback |
|---|---|---|
| `--color-bg-page` | Page background | `#fbfaf8` |
| `--color-bg-card` | Card/panel background | `#ffffff` |
| `--color-bg-elevated` | Elevated surface | `#ffffff` |
| `--color-bg-subtle` | Subtle/muted background | `#f4f2ee` |
| `--color-bg-hover` | Hover state background | `#efece7` |
| `--color-text-primary` | Primary text | `#1a1a1a` |
| `--color-text-secondary` | Secondary/muted text | `#5a5a5a` |
| `--color-text-tertiary` | Hint/label text | `#8a8a8a` |
| `--color-border-muted` | Default border (hairline) | `#e4e1dc` |
| `--color-accent-primary` | Brand/accent color | `#1f5fb4` |
| `--color-profit` | Positive/gain (green) | `#1a7f4f` |
| `--color-loss` | Negative/loss (red) | `#b42318` |
| `--color-warning` | Warning (amber) | `#b7791f` |
| `--color-info` | Info (blue) | `#1f5fb4` |
| `--color-success` | Success (green) | `#1a7f4f` |

## Charts

Load Chart.js or ECharts from CDN. Canvas pixels cannot read CSS variables, so resolve colors via `getComputedStyle` with a **literal fallback** for the standalone/print case:

```html
<div style="position: relative; height: 320px;">
  <canvas id="revChart"></canvas>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
  var cs = getComputedStyle(document.documentElement);
  function pick(name, fallback) {
    var v = cs.getPropertyValue(name).trim();
    return v || fallback;
  }
  var accent = pick('--color-accent-primary', '#1f5fb4');
  var border = pick('--color-border-muted', '#e4e1dc');

  new Chart(document.getElementById('revChart'), {
    type: 'line',
    data: { labels: DATA.labels, datasets: [{ data: DATA.revenue, borderColor: accent, backgroundColor: accent + '22', tension: 0.3, fill: true }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,          // REQUIRED
      animation: { duration: 400 },
      scales: { y: { grid: { color: border } }, x: { grid: { display: false } } }
    }
  });
</script>
```

Rules:
- **Set height on the wrapper `<div>`, never on the `<canvas>`.**
- `responsive: true, maintainAspectRatio: false` always.
- Resolve canvas colors with `getComputedStyle` + literal fallback (the `pick()` helper above) — never bare `var()` in canvas color strings.
- Use UMD CDN builds (set the library global).
- For categorical series, follow the restrained palette in `skills/ui-design/SKILL.md` — no rainbow defaults.

## Print / PDF (Mandatory)

PDF export = the browser's print-to-PDF. Every report **must** include an `@media print` block that:

```css
@media print {
  /* hide interactive chrome — buttons, toolbars, nav, anything not part of the document */
  .no-print, button, nav, .toolbar { display: none !important; }

  /* keep logical blocks from splitting across pages */
  section, figure, table, .card, .kpi { break-inside: avoid; page-break-inside: avoid; }
  h1, h2, h3 { break-after: avoid; }

  /* sane page setup */
  @page { margin: 18mm 16mm; }
  body { background: #fff; color: #000; }

  /* never let entrance animations leave content invisible in the PDF */
  *, *::before, *::after { animation: none !important; transition: none !important; opacity: 1 !important; }

  /* collapse side-by-side layouts — paper is ~816px wide; squeezed columns
     overlap charts and crush prose */
  .row, .grid, .columns { display: block !important; }
  .row > *, .grid > *, .columns > * { width: 100% !important; max-width: 100% !important; }
}
```

If any element starts at `opacity: 0` for an entrance animation, the `opacity: 1 !important` rule above is what stops the PDF from exporting blank — keep it. Test the print path before declaring done.

**Multi-column layouts print badly.** Print width is ~816 CSS px — a flex/grid row pairing a chart card with a text column does not fit and will overlap or crush. Either keep the document single-column throughout (safest for a report), or include print rules like the collapse block above for every side-by-side container you create. Chart wrappers keep their fixed height either way.

**Landscape documents must declare it.** If the content is genuinely wide (a comparison matrix, a wide timeline, a dashboard-style sheet), declare the orientation in the print block — PDF export honors it and lays the page out at landscape width (~1056 CSS px), so charts and columns size for the real paper:

```css
@page { size: letter landscape; margin: 14mm 16mm; }
```

Named sizes (`a4`, `legal`, ...) with optional `landscape` work too. Without a declaration, export is portrait Letter — don't design landscape-wide content and skip the declaration.

**Print typography.** Screen sizing usually reads too large on paper. Inside `@media print`, set print-affecting sizes in `pt` and tighten slightly:

```css
@media print {
  body { font-size: 10.5pt; line-height: 1.45; }
  h1 { font-size: 17pt; }  h2 { font-size: 13pt; }  h3 { font-size: 11pt; }
  .card, section { padding: 10pt 12pt; }
}
```

Aim for 10–11pt body text — the register of a printed research note. Keep table cell padding compact (`4pt 8pt`) so wide tables fit. Page margins come from `@page { margin: ... }`, not body padding.

## Authoring Workflow

1. **Fetch and validate data** first (check for empty/None results). Sample or aggregate to a sensible size.
2. **Read `skills/ui-design/SKILL.md`** and commit to a typographic pairing + color direction.
3. **Build the document**: full HTML, inline CSS/JS, embed `DATA` via `json.dumps(..., ensure_ascii=False)`, draw charts from `DATA`.
4. **Theme-proof it**: every color in `var(--color-role, #fallback)` form.
5. **Add the mandatory `@media print` block.**
6. **Write to `results/report.html`** (UTF-8). For image-heavy reports, write assets to `results/charts/*.png` and reference them relatively.
7. **Sanity-check**: open the path; confirm charts draw, numbers are formatted, layout holds, and the print preview is clean (no blank charts, no clipped sections).
8. **Cite the report** to the user as a clickable link to the file.

## Quality Checklist

- [ ] Full `<!DOCTYPE html>` document; CSS and JS inline; only allowlisted CDNs referenced
- [ ] Data embedded via `<script>const DATA = {json.dumps(..., ensure_ascii=False)}</script>`; large datasets sampled/aggregated
- [ ] Multi-file (if used): all asset paths relative, all assets under `results/`
- [ ] Every color in `var(--color-role, #literalFallback)` form — no bare `var()`, no unvariabled hardcodes
- [ ] Charts: wrapper-div heights, `maintainAspectRatio: false`, `getComputedStyle` + literal fallback for canvas colors
- [ ] Mandatory `@media print` block present: hides chrome, `break-inside: avoid`, sane `@page` margins, animations/opacity neutralized
- [ ] Design follows `skills/ui-design/SKILL.md` (typography, single accent, profit/loss color discipline, no AI slop)
- [ ] Written to `results/`; numbers correctly formatted; opened and print-previewed; cited to the user as a link
