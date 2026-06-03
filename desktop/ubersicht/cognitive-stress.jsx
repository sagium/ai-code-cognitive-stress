/*
 * Cognitive Stress — macOS desktop widget (Übersicht).
 *
 * Renders TODAY's full daily view — the same data and format as the HTML day
 * drill-down and the KDE Plasma widget — produced by `aicogstress --emit-json`
 * (schema ai-code-cognitive-stress.dayview.v1). Shows the composite, the
 * intraday score-progression sparkline, the off-hours nag, the per-hour
 * concurrency chart, and the three axis tiles with zone range bars
 * (baseline / optimum / you markers + boundary ticks). Colours and 0..1
 * fractions come straight from the JSON (dayview.py / scales.py), so the
 * surfaces can't drift.
 *
 * Local-only: shells out to a local CLI and parses its stdout. No network —
 * fonts are the system stack (SF Pro on macOS), nothing is fetched.
 *
 * Install: copy or symlink this file into
 *   ~/Library/Application Support/Übersicht/widgets/
 * (or run `python install.py --ubersicht` from the repo on a Mac).
 *
 * Preview without a Mac: see preview.html next to this file — it executes
 * everything above the "Übersicht exports" marker in a browser, so what you
 * preview IS the widget. From this directory:
 *   aicogstress --emit-json > dayview.json
 *   python3 -m http.server 8731     # then open http://localhost:8731/preview.html
 *
 * Everything above the marker is plain JS (no JSX) — the preview depends on
 * that, so keep JSX syntax confined to the exports section at the bottom.
 */

const FONT_UI = `-apple-system, "SF Pro Display", "SF Pro Text", "IBM Plex Sans", "Instrument Sans", "Helvetica Neue", sans-serif`;
const FONT_MONO = `"SF Mono", ui-monospace, "IBM Plex Mono", "Fragment Mono", Menlo, monospace`;

// All rules scoped under .cogstress — Übersicht widgets share one DOM.
const CSS = `
  .cogstress, .cogstress * { margin: 0; padding: 0; box-sizing: border-box; }
  .cogstress {
    position: relative;
    width: 384px;
    border-radius: 24px;
    padding: 20px 20px 14px;
    font-family: ${FONT_UI};
    background: linear-gradient(178deg, rgba(34, 36, 32, .60), rgba(24, 26, 23, .52));
    -webkit-backdrop-filter: blur(32px) saturate(150%);
    backdrop-filter: blur(32px) saturate(150%);
    border: 1px solid rgba(255, 255, 255, .13);
    box-shadow:
      0 36px 80px -24px rgba(0, 0, 0, .70),
      0 8px 24px -12px rgba(0, 0, 0, .50);
    color: rgba(245, 243, 237, .92);
  }
  .cogstress::before { /* top inner highlight — the glass edge */
    content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
    background: linear-gradient(180deg, rgba(255,255,255,.10), transparent 18%);
    -webkit-mask: linear-gradient(180deg, #000 2%, transparent 30%);
    mask: linear-gradient(180deg, #000 2%, transparent 30%);
  }

  .cogstress .head { display: flex; align-items: center; gap: 12px; }
  .cogstress .score { display: flex; align-items: baseline; gap: 5px; }
  .cogstress .score b {
    font-size: 46px; font-weight: 650; letter-spacing: -.035em; line-height: 1;
    font-feature-settings: "tnum";
  }
  .cogstress .score span { font-size: 12px; color: rgba(245,243,237,.38); font-weight: 500; }
  .cogstress .spark { flex: 1; min-width: 0; }
  .cogstress .advice {
    font-size: 10px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase;
    padding: 5px 10px 4px; border-radius: 999px; white-space: nowrap;
  }
  .cogstress .subhead {
    display: flex; justify-content: space-between; align-items: baseline;
    margin: 8px 2px 0; font-family: ${FONT_MONO}; font-size: 9px;
    color: rgba(245,243,237,.38); letter-spacing: .02em;
  }

  .cogstress .nag {
    margin-top: 12px; padding: 8px 12px; border-radius: 12px;
    font-size: 10.5px; font-weight: 600; line-height: 1.45; color: #e8b27d;
    background: rgba(217, 144, 88, .14); border: 1px solid rgba(217, 144, 88, .22);
  }
  .cogstress .error {
    margin-top: 12px; padding: 8px 12px; border-radius: 12px;
    font-size: 10.5px; font-weight: 600; line-height: 1.45; color: #d98c80;
    background: rgba(176, 74, 58, .16); border: 1px solid rgba(176, 74, 58, .26);
  }

  .cogstress .chart { margin-top: 14px; }
  .cogstress .chart-title, .cogstress .tile-name {
    font-size: 12.5px; font-weight: 600; letter-spacing: -.01em;
  }
  .cogstress .chart-title { margin: 0 2px 6px; }
  .cogstress svg { display: block; }

  .cogstress .tile {
    margin-top: 10px; padding: 11px 14px 10px; border-radius: 16px;
    background: rgba(255, 255, 255, .045);
    border: 1px solid rgba(255, 255, 255, .085);
  }
  .cogstress .tile-head { display: flex; justify-content: space-between; align-items: baseline; }
  .cogstress .tile-zone { font-size: 10px; font-weight: 700; letter-spacing: .01em; }
  .cogstress .tile-desc {
    margin-top: 3px; font-size: 10.5px; line-height: 1.45; color: rgba(245,243,237,.60);
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .cogstress .tile-foot { display: flex; justify-content: space-between; align-items: baseline; margin-top: 2px; }
  .cogstress .tile-value { font-size: 15px; font-weight: 700; font-feature-settings: "tnum"; }
  .cogstress .tile-unit { font-size: 9.5px; color: rgba(245,243,237,.38); }

  .cogstress .foot {
    display: flex; justify-content: space-between; margin: 12px 2px 0;
    font-family: ${FONT_MONO}; font-size: 8.5px; color: rgba(245,243,237,.38);
  }
`;

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// --- header: composite / 100 · sparkline · advice pill ----------------------
function sparkline(dv, W = 150, H = 40) {
  const s = dv.score_progression;
  if (!s || s.length < 2) return `<div class="spark"></div>`;
  const p = 5, n = s.length;
  const sx = (i) => p + (i / (n - 1)) * (W - 2 * p);
  const sy = (v) => H - p - (Math.max(0, Math.min(100, v)) / 100) * (H - 2 * p);
  let segs = "";
  for (let i = 0; i < n - 1; i++) {
    segs += `<line x1="${sx(i)}" y1="${sy(s[i].value)}" x2="${sx(i + 1)}" y2="${sy(s[i + 1].value)}"
      stroke="${s[i + 1].color}" stroke-width="2.2" stroke-linecap="round"
      style="filter: drop-shadow(0 0 5px ${s[i + 1].color}66)"/>`;
  }
  const last = s[n - 1];
  return `<div class="spark"><svg viewBox="0 0 ${W} ${H}" width="100%">
    <line x1="${p}" y1="${H - p}" x2="${W - p}" y2="${H - p}" stroke="rgba(255,255,255,.10)"/>
    ${segs}
    <circle cx="${sx(n - 1)}" cy="${sy(last.value)}" r="3" fill="${last.color}"
      style="filter: drop-shadow(0 0 6px ${last.color})"/>
  </svg></div>`;
}

function header(dv) {
  const c = dv.has_activity ? dv.composite_color : "rgba(245,243,237,.35)";
  return `
  <div class="head">
    <div class="score"><b style="color:${c}">${esc(dv.composite_label)}</b><span>/ 100</span></div>
    ${sparkline(dv)}
    <div class="advice" style="color:${c}; background:${c}22; border:1px solid ${c}33">${esc(dv.advice)}</div>
  </div>
  <div class="subhead"><span>${esc(dv.day_label)}</span><span>${esc(dv.work_window_label || "")}</span></div>`;
}

// --- per-hour concurrency chart ----------------------------------------------
function hourChart(dv) {
  if (!dv.has_activity) return "";
  const W = 344, H = 116, mL = 16, mR = 2, mT = 16, mB = 13;
  const pw = W - mL - mR, ph = H - mT - mB;
  const peak = Math.max(dv.peak_concurrent, 1), bw = pw / 24;
  let out = "";

  const ww = dv.work_window;
  if (ww && ww.end_hour > ww.start_hour)
    out += `<rect x="${mL + ww.start_hour * bw}" y="${mT}" width="${(ww.end_hour - ww.start_hour) * bw}" height="${ph}"
            fill="rgba(108,154,139,.10)" stroke="rgba(108,154,139,.18)" stroke-width="0.5" rx="3"/>`;

  for (let i = 0; i <= peak; i++) {
    const y = mT + ph - (i / peak) * ph;
    out += `<line x1="${mL}" y1="${y}" x2="${mL + pw}" y2="${y}" stroke="rgba(255,255,255,.07)"/>
            <text x="${mL - 5}" y="${y + 2.5}" text-anchor="end" font-family='${FONT_MONO}' font-size="7.5" fill="rgba(245,243,237,.38)">${i}</text>`;
  }

  dv.hours.forEach((c, h) => {
    if (c <= 0) return;
    const bh = (c / peak) * ph, x = mL + h * bw + bw * 0.14, y = mT + ph - bh;
    const col = (dv.hour_colors || [])[h] || "#d99058";
    out += `<rect x="${x}" y="${y}" width="${bw * 0.72}" height="${bh}" rx="2.5"
              fill="${col}" opacity=".88" style="filter: drop-shadow(0 0 7px ${col}55)"/>
            <text x="${x + bw * 0.36}" y="${y - 4}" text-anchor="middle" font-family='${FONT_MONO}' font-size="8" font-weight="700" fill="rgba(245,243,237,.85)">${c}</text>`;
  });

  out += `<line x1="${mL}" y1="${mT + ph}" x2="${mL + pw}" y2="${mT + ph}" stroke="rgba(255,255,255,.22)"/>`;
  for (let h = 0; h <= 24; h += 3)
    out += `<text x="${mL + h * bw}" y="${H - 2}" text-anchor="middle" font-family='${FONT_MONO}' font-size="7.5" fill="rgba(245,243,237,.38)">${String(h).padStart(2, "0")}</text>`;

  return `<div class="chart"><div class="chart-title">Concurrent agent sessions per hour</div>
          <svg viewBox="0 0 ${W} ${H}" width="100%">${out}</svg></div>`;
}

// --- one axis tile: zone range bar + baseline/optimum/you --------------------
function rangeBar(a) {
  const W = 314, H = 62, pad = 13, inner = W - 2 * pad;
  const barY = 27, barH = 7;
  const baseY = 8, optY = 19, tickY = 45, youY = 57;
  const xAt = (f) => pad + Math.max(0, Math.min(1, f)) * inner;
  const anch = (x) => (x < pad + 22 ? "start" : x > W - pad - 22 ? "end" : "middle");
  const clip = `cogstress-track-${a.key}`;
  let out = `<defs><clipPath id="${clip}"><rect x="${pad}" y="${barY}" width="${inner}" height="${barH}" rx="3.5"/></clipPath></defs><g clip-path="url(#${clip})">`;
  for (const s of a.segments)
    out += `<rect x="${xAt(s.start)}" y="${barY}" width="${xAt(s.end) - xAt(s.start)}" height="${barH}" fill="${s.color}" opacity=".8"/>`;
  out += `</g>`;

  const tick = (x, label) =>
    `<text x="${x}" y="${tickY}" text-anchor="middle" font-family='${FONT_MONO}' font-size="7" fill="rgba(245,243,237,.38)">${esc(label)}</text>`;
  for (const t of a.boundary_ticks) out += tick(xAt(t.fraction), t.label);
  out += tick(pad, "0") + tick(W - pad, String(a.range_max));

  if (a.baseline_fraction !== null) {
    const bx = xAt(a.baseline_fraction);
    out += `<line x1="${bx}" y1="${baseY + 3}" x2="${bx}" y2="${barY + barH + 4}" stroke="rgba(245,243,237,.40)" stroke-dasharray="2 2"/>
            <text x="${bx}" y="${baseY}" text-anchor="${anch(bx)}" font-size="7.5" font-family='${FONT_MONO}' fill="rgba(245,243,237,.50)">${esc(a.baseline_label)}</text>`;
  }
  if (a.optimum_fraction !== null) {
    const ox = xAt(a.optimum_fraction);
    out += `<line x1="${ox}" y1="${optY + 3}" x2="${ox}" y2="${barY + barH + 4}" stroke="#efe9da" stroke-dasharray="3 3" opacity=".8"/>
            <text x="${ox}" y="${optY}" text-anchor="${anch(ox)}" font-size="7.5" font-family='${FONT_MONO}' fill="#efe9da">${esc(a.optimum_label)}</text>`;
  }

  // No-data axis (only a day with no activity at all now): draw the scale for
  // context but no "you" marker — a 0-position marker would read as a perfect
  // score, not "not measured".
  if (a.has_data === false) {
    out += `<text x="${W / 2}" y="${youY}" text-anchor="middle" font-size="8" font-style="italic" fill="rgba(245,243,237,.38)">not measured this day</text>`;
  } else {
    const ux = xAt(Math.min(1, a.fraction));
    out += `<line x1="${ux}" y1="${barY - 6}" x2="${ux}" y2="${barY + barH + 6}" stroke="#fff" stroke-width="2"
              style="filter: drop-shadow(0 0 4px rgba(255,255,255,.6))"/>
            <text x="${ux}" y="${youY}" text-anchor="${anch(ux)}" font-size="8" font-weight="700" font-family='${FONT_MONO}'
              fill="rgba(245,243,237,.92)">you ${a.value.toFixed(2)}${a.off_scale ? " ▶" : ""}</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="margin-top:7px">${out}</svg>`;
}

function axisTile(a) {
  return `<div class="tile">
    <div class="tile-head"><span class="tile-name">${esc(a.name)}</span>
      <span class="tile-zone" style="color:${a.color}">${esc(a.zone_label)}</span></div>
    <div class="tile-desc">${esc(a.description)}</div>
    ${rangeBar(a)}
    <div class="tile-foot"><span class="tile-value" style="color:${a.color}">${esc(a.value_label)}</span>
      <span class="tile-unit">${esc(a.unit_text)}</span></div>
  </div>`;
}

// --- assemble the card --------------------------------------------------------
function renderWidget(dv) {
  return [
    header(dv),
    dv.off_hours_nag ? `<div class="nag">${esc(dv.off_hours_nag)}</div>` : "",
    hourChart(dv),
    ...dv.axes.map(axisTile),
    `<div class="foot"><span>local-only · updates live</span><span>${esc(dv.day.slice(0, 7))}</span></div>`,
  ].join("");
}

function renderError(message) {
  return `<div class="head"><div class="score"><b style="color:rgba(245,243,237,.35)">—</b><span>/ 100</span></div></div>
    <div class="error">${esc(message)}</div>
    <div class="foot"><span>local-only</span><span>cognitive stress</span></div>`;
}

// ── Übersicht exports ── everything below is macOS-only (JSX); preview.html
// executes only the plain-JS half above this marker.

// If the score stays blank, `aicogstress` isn't on Übersicht's PATH — replace
// with the absolute path (`command -v aicogstress` in your terminal).
export const command = "aicogstress --emit-json";

export const refreshFrequency = 60 * 1000; // ms

// Position on the desktop — edit to taste.
export const className = `
  top: 36px;
  right: 36px;
`;

export const render = ({ output, error }) => {
  let html;
  if (error) {
    html = renderError(`${error} — is aicogstress on PATH? Set an absolute path in the widget's "command".`);
  } else {
    try {
      html = renderWidget(JSON.parse(output));
    } catch (e) {
      html = renderError("Could not parse the daily-view output. Run `aicogstress --emit-json` in a terminal to check.");
    }
  }
  return (
    <div>
      <style>{CSS}</style>
      <div className="cogstress" dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
};
