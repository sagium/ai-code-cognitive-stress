"""Self-contained HTML day card for the desktop widgets — the SINGLE renderer
behind `aicogstress --emit-html-card`.

Both desktop widgets (KDE Plasma, macOS Übersicht) and the browser preview are
thin hosts that inject this module's output verbatim, so the card can only be
drawn one way. It renders the canonical daily-view model (`dayview.py`) — the
same data the HTML report's day drill-down shows — as one fragment:

    <style>…</style><div class="cogstress" data-composite-label="…" …>…</div>

Everything is inline (scoped CSS + SVG, system font stacks, no scripts, no
external references), so the fragment works injected into Übersicht's DOM,
wrapped in a minimal page inside the plasmoid's WebEngineView, or dropped into
any browser. The `data-*` attributes on the root div carry the headline values
so a host can show a compact summary (e.g. the Plasma panel label) without
parsing the markup or running a second command.

Private: pure string building from the DayView — no I/O, no network.
"""

from __future__ import annotations

from html import escape

from .dayview import AxisTile, DayView

CARD_WIDTH = 384  # px — fixed card width shared by every host

FONT_UI = (
    '-apple-system, "SF Pro Display", "SF Pro Text", "IBM Plex Sans", '
    '"Instrument Sans", "Helvetica Neue", sans-serif'
)
FONT_MONO = (
    '"SF Mono", ui-monospace, "IBM Plex Mono", "Fragment Mono", Menlo, monospace'
)

# All rules scoped under .cogstress — Übersicht widgets share one DOM.
# (Plain string + .replace because CSS braces would fight an f-string.)
CSS = """
  .cogstress, .cogstress * { margin: 0; padding: 0; box-sizing: border-box; }
  .cogstress {
    position: relative;
    width: __CARD_WIDTH__px;
    border-radius: 24px;
    padding: 20px 20px 14px;
    font-family: __FONT_UI__;
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
    margin: 8px 2px 0; font-family: __FONT_MONO__; font-size: 9px;
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
  }
  .cogstress .tile-foot { display: flex; justify-content: space-between; align-items: baseline; margin-top: 2px; }
  .cogstress .tile-value { font-size: 15px; font-weight: 700; font-feature-settings: "tnum"; }
  .cogstress .tile-unit { font-size: 9.5px; color: rgba(245,243,237,.38); }

  .cogstress .foot {
    display: flex; justify-content: space-between; margin: 12px 2px 0;
    font-family: __FONT_MONO__; font-size: 8.5px; color: rgba(245,243,237,.38);
  }
""".replace("__CARD_WIDTH__", str(CARD_WIDTH)) \
   .replace("__FONT_UI__", FONT_UI) \
   .replace("__FONT_MONO__", FONT_MONO)

_DIM = "rgba(245,243,237,.35)"  # disabled/no-activity text


def _esc(s: object) -> str:
    return escape(str(s), quote=True)


def _num(x: float) -> str:
    """Format a coordinate/number the way JS template literals would — no
    trailing '.0' on integers, full precision otherwise."""
    return f"{x:g}"


# --- header: composite / 100 · sparkline · advice pill ----------------------

def _sparkline(dv: DayView, w: int = 150, h: int = 40) -> str:
    s = dv.score_progression
    if len(s) < 2:
        return '<div class="spark"></div>'
    p, n = 5, len(s)

    def sx(i: int) -> float:
        return p + (i / (n - 1)) * (w - 2 * p)

    def sy(v: float) -> float:
        return h - p - (max(0.0, min(100.0, v)) / 100) * (h - 2 * p)

    segs = ""
    for i in range(n - 1):
        segs += (
            f'<line x1="{_num(sx(i))}" y1="{_num(sy(s[i].value))}" '
            f'x2="{_num(sx(i + 1))}" y2="{_num(sy(s[i + 1].value))}" '
            f'stroke="{s[i + 1].color}" stroke-width="2.2" stroke-linecap="round" '
            f'style="filter: drop-shadow(0 0 5px {s[i + 1].color}66)"/>'
        )
    last = s[-1]
    return (
        f'<div class="spark"><svg viewBox="0 0 {w} {h}" width="100%">'
        f'<line x1="{p}" y1="{h - p}" x2="{w - p}" y2="{h - p}" stroke="rgba(255,255,255,.10)"/>'
        f'{segs}'
        f'<circle cx="{_num(sx(n - 1))}" cy="{_num(sy(last.value))}" r="3" fill="{last.color}" '
        f'style="filter: drop-shadow(0 0 6px {last.color})"/>'
        f'</svg></div>'
    )


def _header(dv: DayView) -> str:
    c = dv.composite_color if dv.has_activity else _DIM
    return (
        f'<div class="head">'
        f'<div class="score"><b style="color:{c}">{_esc(dv.composite_label)}</b><span>/ 100</span></div>'
        f'{_sparkline(dv)}'
        f'<div class="advice" style="color:{c}; background:{c}22; border:1px solid {c}33">{_esc(dv.advice)}</div>'
        f'</div>'
        f'<div class="subhead"><span>{_esc(dv.day_label)}</span><span>{_esc(dv.work_window_label or "")}</span></div>'
    )


# --- per-hour concurrency chart ----------------------------------------------

def _hour_chart(dv: DayView) -> str:
    if not dv.has_activity:
        return ""
    w, h, m_l, m_r, m_t, m_b = 344, 116, 16, 2, 16, 13
    pw, ph = w - m_l - m_r, h - m_t - m_b
    peak = max(dv.peak_concurrent, 1)
    bw = pw / 24
    out = ""

    ww = dv.work_window
    if ww and ww.end_hour > ww.start_hour:
        out += (
            f'<rect x="{_num(m_l + ww.start_hour * bw)}" y="{m_t}" '
            f'width="{_num((ww.end_hour - ww.start_hour) * bw)}" height="{ph}" '
            f'fill="rgba(108,154,139,.10)" stroke="rgba(108,154,139,.18)" stroke-width="0.5" rx="3"/>'
        )

    for i in range(peak + 1):
        y = m_t + ph - (i / peak) * ph
        out += (
            f'<line x1="{m_l}" y1="{_num(y)}" x2="{m_l + pw}" y2="{_num(y)}" stroke="rgba(255,255,255,.07)"/>'
            f'<text x="{m_l - 5}" y="{_num(y + 2.5)}" text-anchor="end" '
            f"font-family='{FONT_MONO}' font-size=\"7.5\" fill=\"rgba(245,243,237,.38)\">{i}</text>"
        )

    for hour, c in enumerate(dv.hours):
        if c <= 0:
            continue
        bh = (c / peak) * ph
        x = m_l + hour * bw + bw * 0.14
        y = m_t + ph - bh
        col = dv.hour_colors[hour] if hour < len(dv.hour_colors) else "#d99058"
        out += (
            f'<rect x="{_num(x)}" y="{_num(y)}" width="{_num(bw * 0.72)}" height="{_num(bh)}" rx="2.5" '
            f'fill="{col}" opacity=".88" style="filter: drop-shadow(0 0 7px {col}55)"/>'
            f'<text x="{_num(x + bw * 0.36)}" y="{_num(y - 4)}" text-anchor="middle" '
            f"font-family='{FONT_MONO}' font-size=\"8\" font-weight=\"700\" fill=\"rgba(245,243,237,.85)\">{c}</text>"
        )

    out += f'<line x1="{m_l}" y1="{m_t + ph}" x2="{m_l + pw}" y2="{m_t + ph}" stroke="rgba(255,255,255,.22)"/>'
    for hour in range(0, 25, 3):
        out += (
            f'<text x="{_num(m_l + hour * bw)}" y="{h - 2}" text-anchor="middle" '
            f"font-family='{FONT_MONO}' font-size=\"7.5\" fill=\"rgba(245,243,237,.38)\">{hour:02d}</text>"
        )

    return (
        f'<div class="chart"><div class="chart-title">Concurrent agent sessions per hour</div>'
        f'<svg viewBox="0 0 {w} {h}" width="100%">{out}</svg></div>'
    )


# --- one axis tile: zone range bar + baseline/optimum/you --------------------

def _range_bar(a: AxisTile) -> str:
    w, h, pad = 314, 62, 13
    inner = w - 2 * pad
    bar_y, bar_h = 27, 7
    base_y, opt_y, tick_y, you_y = 8, 19, 45, 57

    def x_at(f: float) -> float:
        return pad + max(0.0, min(1.0, f)) * inner

    def anch(x: float) -> str:
        return "start" if x < pad + 22 else "end" if x > w - pad - 22 else "middle"

    clip = f"cogstress-track-{a.key}"
    out = (
        f'<defs><clipPath id="{clip}"><rect x="{pad}" y="{bar_y}" width="{inner}" '
        f'height="{bar_h}" rx="3.5"/></clipPath></defs><g clip-path="url(#{clip})">'
    )
    for s in a.segments:
        out += (
            f'<rect x="{_num(x_at(s.start))}" y="{bar_y}" '
            f'width="{_num(x_at(s.end) - x_at(s.start))}" height="{bar_h}" '
            f'fill="{s.color}" opacity=".8"/>'
        )
    out += "</g>"

    def tick(x: float, label: str) -> str:
        return (
            f'<text x="{_num(x)}" y="{tick_y}" text-anchor="middle" '
            f"font-family='{FONT_MONO}' font-size=\"7\" fill=\"rgba(245,243,237,.38)\">{_esc(label)}</text>"
        )

    for t in a.boundary_ticks:
        out += tick(x_at(t.fraction), t.label)
    out += tick(pad, "0") + tick(w - pad, f"{a.range_max:g}")

    if a.baseline_fraction is not None:
        bx = x_at(a.baseline_fraction)
        out += (
            f'<line x1="{_num(bx)}" y1="{base_y + 3}" x2="{_num(bx)}" y2="{bar_y + bar_h + 4}" '
            f'stroke="rgba(245,243,237,.40)" stroke-dasharray="2 2"/>'
            f'<text x="{_num(bx)}" y="{base_y}" text-anchor="{anch(bx)}" font-size="7.5" '
            f"font-family='{FONT_MONO}' fill=\"rgba(245,243,237,.50)\">{_esc(a.baseline_label)}</text>"
        )
    if a.optimum_fraction is not None:
        ox = x_at(a.optimum_fraction)
        out += (
            f'<line x1="{_num(ox)}" y1="{opt_y + 3}" x2="{_num(ox)}" y2="{bar_y + bar_h + 4}" '
            f'stroke="#efe9da" stroke-dasharray="3 3" opacity=".8"/>'
            f'<text x="{_num(ox)}" y="{opt_y}" text-anchor="{anch(ox)}" font-size="7.5" '
            f"font-family='{FONT_MONO}' fill=\"#efe9da\">{_esc(a.optimum_label)}</text>"
        )

    # No-data axis (only a day with no activity at all now): draw the scale for
    # context but no "you" marker — a 0-position marker would read as a perfect
    # score, not "not measured".
    if not a.has_data:
        out += (
            f'<text x="{w / 2:g}" y="{you_y}" text-anchor="middle" font-size="8" '
            f'font-style="italic" fill="rgba(245,243,237,.38)">not measured this day</text>'
        )
    else:
        ux = x_at(min(1.0, a.fraction))
        marker = "you " + f"{a.value:.2f}" + (" ▶" if a.off_scale else "")
        out += (
            f'<line x1="{_num(ux)}" y1="{bar_y - 6}" x2="{_num(ux)}" y2="{bar_y + bar_h + 6}" '
            f'stroke="#fff" stroke-width="2" style="filter: drop-shadow(0 0 4px rgba(255,255,255,.6))"/>'
            f'<text x="{_num(ux)}" y="{you_y}" text-anchor="{anch(ux)}" font-size="8" '
            f"font-weight=\"700\" font-family='{FONT_MONO}' "
            f'fill="rgba(245,243,237,.92)">{_esc(marker)}</text>'
        )
    return f'<svg viewBox="0 0 {w} {h}" width="100%" style="margin-top:7px">{out}</svg>'


def _axis_tile(a: AxisTile) -> str:
    return (
        f'<div class="tile">'
        f'<div class="tile-head"><span class="tile-name">{_esc(a.name)}</span>'
        f'<span class="tile-zone" style="color:{a.color}">{_esc(a.zone_label)}</span></div>'
        f'<div class="tile-desc">{_esc(a.description)}</div>'
        f'{_range_bar(a)}'
        f'<div class="tile-foot"><span class="tile-value" style="color:{a.color}">{_esc(a.value_label)}</span>'
        f'<span class="tile-unit">{_esc(a.unit_text)}</span></div>'
        f'</div>'
    )


# --- assemble the card --------------------------------------------------------

def _wrap(inner: str, *, label: str, color: str, has_activity: bool) -> str:
    """Scoped stylesheet + root div. The data-* attributes summarise the
    headline so hosts (the Plasma panel label) can read it without a parser."""
    return (
        f"<style>{CSS}</style>"
        f'<div class="cogstress" data-composite-label="{_esc(label)}" '
        f'data-composite-color="{_esc(color)}" '
        f'data-has-activity="{"true" if has_activity else "false"}">{inner}</div>'
    )


def render_card(dv: DayView) -> str:
    """The full day card as one self-contained HTML fragment."""
    inner = "".join([
        _header(dv),
        f'<div class="nag">{_esc(dv.off_hours_nag)}</div>' if dv.off_hours_nag else "",
        _hour_chart(dv),
        *(_axis_tile(a) for a in dv.axes),
        f'<div class="foot"><span>private · updates live</span>'
        f'<span>{_esc(dv.day.isoformat()[:7])}</span></div>',
    ])
    return _wrap(
        inner, label=dv.composite_label, color=dv.composite_color,
        has_activity=dv.has_activity,
    )


def render_error_card(message: str) -> str:
    """An error state in the same card chrome (used by hosts that got a card
    earlier but a failure now, and by the preview's no-data hint)."""
    inner = (
        f'<div class="head"><div class="score"><b style="color:{_DIM}">—</b><span>/ 100</span></div></div>'
        f'<div class="error">{_esc(message)}</div>'
        f'<div class="foot"><span>private</span><span>cognitive stress</span></div>'
    )
    return _wrap(inner, label="—", color=_DIM, has_activity=False)
