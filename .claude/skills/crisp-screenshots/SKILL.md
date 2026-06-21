---
name: crisp-screenshots
description: Produce or refresh the repo's visual artifacts (README banner, report screenshots, diagrams) as crisp, scalable, theme-faithful SVGs. MUST be applied whenever a screenshot in docs/screenshots/ is regenerated, a new visual artifact is added to the README/docs, or the report/widget theme changes. Covers the mostly-vector capture pipeline (scripts/capture_screenshots.py), hand-authored SVG rules, the Chrome print-rasterization gotchas, and the zoom-QA step.
user-invocable: true
---

# crisp-screenshots — scalable, theme-faithful visual artifacts

Every image on a public surface (README, docs/) must scale crisply and look
exactly like the app. Two artifact classes, two recipes — plus a QA gate both
must pass.

## Hard rules (all artifacts)

- **Vector SVG by default.** A raster is acceptable only for inherently
  photographic shots (e.g. `ubersicht-widget.png`, a real desktop) — and then
  at ≥ 2x the displayed CSS size.
- **The app's theme is the source of truth.** Dark report surfaces use the
  palette in `ai_code_cognitive_stress/output/render.py` `_STYLES` (`--bg: #161814`, ink
  `rgba(245,243,237,.92)`, accent `#efe9da`, panel gradient
  `178deg, #1d201c → #131512`, glass edges `rgba(255,255,255,.13)`); zone
  colours come from `ai_code_cognitive_stress/output/scales.py` (`#6c9a8b` / `#c5b48a` /
  `#d99058` / `#b04a3a`). Do NOT use the light `REPORT_THEME` dict for a
  banner/screenshot unless the artifact really shows a light surface.
- **Demo data only** — screenshots come from
  `scripts/generate_demo_report.py`; never capture real session data.
- Copy on artifacts follows the repo's wording rules: no coding-tool vendor names, no emoji.

## Recipe 1 — captured screenshots (the HTML report)

Run the committed pipeline; do not hand-roll captures:

```bash
uv run --with playwright --with scour python scripts/capture_screenshots.py
```

It writes `docs/screenshots/report-overview.svg` and `day-modal.svg` as
**mostly-vector** SVGs: text/charts/rules are real vector paths, and only the
glass-blur panels are embedded as raster patches at an effective 2x density.
Needs system Chrome and `pdftocairo` (poppler); playwright + scour are
provided by `uv` at invocation time and are never runtime deps.

How it works: headless Chrome prints the demo report to a single-page vector
PDF (at `body{zoom:2}` in a 2x viewport), `pdftocairo -svg` converts it,
scour minifies, and the script normalizes the root back to 1x CSS pixels
(cropping the day capture to `.day-modal` via the viewBox).

### Chrome print gotchas (each cost real debugging — don't rediscover them)

- **Never print with a `#fragment` / `:target` state active.** Any
  `:target`-driven state (including `:has(:target)`) makes Chrome rasterize
  the ENTIRE page into one bitmap. Reveal hidden sections with inline styles
  instead.
- **`display:none` bulk-hiding + `body{zoom:2}` also triggers the full-page
  raster fallback.** Hide with `visibility:hidden` (content is still omitted
  from the PDF; layout is preserved).
- **`page.pdf(scale=…)` does NOT raise raster-patch density** — patches embed
  at layout resolution regardless. Doubling the layout (`body{zoom:2}` +
  doubled viewport/page size) is what gets 2x patches; going to a plain
  `scale=2` collapses the whole page to one bitmap.
- pdftocairo's viewBox is in **pt** (1 px = 0.75 pt); crops measured in CSS
  px must be multiplied by `0.75 × zoom`.

## Recipe 2 — hand-authored SVGs (banner, pipeline diagram)

Author directly in SVG (`docs/screenshots/banner.svg` is the reference):

- Real `<text>` elements with a system font stack
  (`'Helvetica Neue', Helvetica, Arial, sans-serif`; mono:
  `ui-monospace, 'SF Mono', Menlo, Consolas, monospace`) — never outline text
  by hand, never reference webfonts (GitHub blocks remote loads inside SVG).
- Self-contained: no external images, no scripts, no CSS classes that depend
  on page context.
- Use the app palette above; gradients/markers go in `<defs>`.
- Mind collisions: leave ≥ 25 px between independent text blocks; check every
  label pair at the final aspect ratio (axis labels vs zone words was a real
  overlap).

## QA gate (before considering the artifact done)

1. **Render and look at it** — load the SVG in headless Chrome, screenshot at
   1x full view AND at ~3x width, and inspect both: layout, overlaps, theme
   match against the real report/widget.
2. **Vector regression** — for captured screenshots check the SVG still
   contains real glyph paths and only a bounded set of raster patches
   (`grep -c '<image' file.svg` — expect ~20–30 for the overview, a handful
   for the modal; `1` means the rasterization fallback fired and the capture
   is junk).
3. **Size sanity** — captured SVGs should land well under ~3 MB after scour;
   a multi-MB single-patch file is the raster fallback, not a big vector.
4. README `<img>` sizes: banner and overview at `width="100%"`, day modal and
   widget photo at fixed pixel widths matching their intrinsic 1x size.
