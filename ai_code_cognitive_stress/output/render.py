"""Render a StressProfile into a self-contained HTML report.

The output is one .html file with embedded CSS and SVG — no external assets,
no JS, opens identically online or offline. Each metric carries its
technique + research basis + caveat inline, per the project's
"surface-technique" design principle.

The visual treatment is the same dark-glass theme as the desktop widget card
(`widget_card.py`): the font stacks are imported from there, and the palette
constants below mirror its rgba ink/rule values, so the report and the widgets
read as one product. (`templates/report.html` is the original light-theme
mockup, kept for the information architecture only.)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from html import escape

from .. import __version__
from ..pipeline.aggregate import AggregateStats, DayAggregate
from ..core.citations import load_registry
from . import dayview
from ..core import i18n
from ..core.i18n import t, tn
from ..pipeline.metrics import (
    DayMetrics,
    StressProfile,
    WorkWindow,
)
from .scales import (
    codl_count_color,
    composite_label,
    composite_status,
    zone_color,
)
from .widget_card import FONT_MONO, FONT_UI

# Dark-glass ink palette, mirroring widget_card.py. SVG presentation
# attributes can't resolve CSS custom properties, so the inline-SVG builders
# below use these Python constants instead of var(--…).
_INK = "rgba(245,243,237,.92)"
_INK_SOFT = "rgba(245,243,237,.60)"
_INK_FAINT = "rgba(245,243,237,.38)"
_GRID = "rgba(255,255,255,.07)"        # chart grid lines
_AXIS = "rgba(255,255,255,.22)"        # chart axis lines
_OPTIMUM = "#efe9da"                   # optimum marker (cream, as in the card)


def report(
    profile: StressProfile,
    aggregates: dict[date, DayAggregate],
    label: str,
    ingest_stats: AggregateStats | None = None,
    generated_at: datetime | None = None,
    agent_analysis_html: str | None = None,
) -> str:
    """Build the full HTML report as a single self-contained string.

    Sections are emitted in the order: header, optional agent analysis,
    year overview, one section per active month, recommendations,
    methodology. `agent_analysis_html` should already be an HTML fragment
    — the CLI converts markdown to HTML before calling.
    """
    generated_at = generated_at or datetime.now(timezone.utc).astimezone()
    sections = [
        _render_header(label, generated_at, ingest_stats),
        _render_agent_analysis(agent_analysis_html),
        _render_year_overview(profile),
        _render_all_months(profile, aggregates),
        _render_recommendations(profile),
        _render_methodology(profile, ingest_stats),
    ]
    body = "\n".join(s for s in sections if s)
    return _wrap_document(
        t("report.title", label=label), body,
        extra_css=_selected_month_style(profile),
    )


def _today_local() -> date:
    """Today's date in the system local timezone. Day bucketing is local
    throughout the pipeline (see aggregate.get_day_aggregates), so every
    "today" anchor in the report must be local too — the UTC date is
    yesterday's between local midnight and the UTC day rollover."""
    return datetime.now().astimezone().date()


def _render_agent_analysis(html_fragment: str | None) -> str:
    """Wrap an already-rendered HTML fragment in the focus-panel shell.

    This section is intentionally for **top-priority advice only** — what
    to act on, not a comprehensive interpretation. Observational summaries
    belong in the data sections below; the methodology footer carries
    coverage caveats. Returns empty string when no advice is provided so
    the report still works end-to-end without an agent in the loop.
    """
    if not html_fragment:
        return ""
    return f"""
<section class="agent-analysis">
  <h2>{escape(t("report.top_focus"))}</h2>
  <div class="agent-analysis-body">
    {html_fragment}
  </div>
</section>
""".strip()


def _months_with_activity(profile: StressProfile) -> list[tuple[int, int]]:
    """Sorted list of (year, month) tuples for months that have any active day."""
    return sorted({
        (d.year, d.month)
        for d, m in profile.days.items()
        if m.composite > 0
    })


def _active_days_in_month(profile: StressProfile, year: int, month: int) -> list[date]:
    """Active days in the given month with non-zero composite."""
    return sorted(
        d for d, m in profile.days.items()
        if d.year == year and d.month == month
        and m.composite > 0
    )


# ---------------------------------------------------------------------------
# Section: header

def _render_header(
    label: str,
    generated_at: datetime,
    ingest_stats: AggregateStats | None,
) -> str:
    src = ""
    if ingest_stats is not None:
        i = ingest_stats.ingest
        src = t(
            "report.source_stats",
            active=ingest_stats.days_with_activity,
            window=ingest_stats.days_in_window,
            events=f"{i.events_emitted:,}",
            records=f"{ingest_stats.ingest.lines_decoded:,}",
        )
    generated = t(
        "report.generated", timestamp=generated_at.strftime("%Y-%m-%d %H:%M %Z"),
    )
    return f"""
<header class="report">
  <h1>{escape(t("report.heading"))}</h1>
  <p class="subtitle">
    {escape(label)} &middot; {generated}{src}
  </p>
</header>
""".strip()


# ---------------------------------------------------------------------------
# Section: year overview

def _render_year_overview(profile: StressProfile) -> str:
    """12 monthly cells with the avg composite for that month."""
    if not profile.days:
        return ""
    today = _today_local()
    # Find the year(s) present in the data; if more than one, default to the
    # most recent.
    years = {d.year for d in profile.days}
    if not years:
        return ""
    year = max(years)

    cells: list[str] = []
    for m in range(1, 13):
        days_in_month = [
            metrics for day, metrics in profile.days.items()
            if day.year == year and day.month == m and metrics.composite > 0
        ]
        first_of_month = date(year, m, 1)
        is_future = first_of_month > today
        cells.append(_year_cell(year, m, days_in_month, is_future, profile))

    sparkline = _year_sparkline(profile, year)

    return f"""
<section class="year-view">
  <h2>{t("report.year_overview", year=year)}</h2>
  <div class="panel">
    <div class="year-grid">
      {"".join(cells)}
    </div>
    {sparkline}
    <p class="note">
      {escape(t("report.year_note"))}
    </p>
  </div>
</section>
""".strip()


def _year_cell(
    year: int,
    month: int,
    days_metrics: list[DayMetrics],
    is_future: bool,
    profile: StressProfile,
) -> str:
    month_name = i18n.month_name(month, short=True)
    classes = ["year-cell"]
    if is_future:
        classes.append("future")
    status = _status_for_composite(
        sum(d.composite for d in days_metrics) / len(days_metrics)
        if days_metrics else 0.0,
        profile,
    )
    if status and not is_future:
        classes.append(f"status-{status}")
    if not days_metrics:
        value = "—"
        trend = ""
    else:
        avg = sum(d.composite for d in days_metrics) / len(days_metrics)
        value = f"{avg:.0f}"
        trend = tn("report.days_active", len(days_metrics))
    inner = (
        f'<div class="ym-month">{month_name}</div>'
        f'<div class="ym-value">{value}</div>'
        f'<div class="ym-trend">{escape(trend)}</div>'
    )
    # Cells for months that have data become anchor links into the
    # corresponding month section below.
    if days_metrics:
        return (
            f'<a class="{" ".join(classes)}" href="#month-{year}-{month:02d}">'
            f'{inner}</a>'
        )
    return f'<div class="{" ".join(classes)}">{inner}</div>'


def _year_sparkline(profile: StressProfile, year: int) -> str:
    """Continuous line through every active workday's composite, plotted at
    its calendar position across the year. The line is stroked with a
    vertical gradient matching the heatmap palette, so its color at any
    height implicitly encodes the composite zone (low = good green,
    high = red). The dashed horizontal line marks the user's typical-day
    composite (p50) — a health reference."""
    width, height = 1100, 80
    pad_top, pad_bot = 6, 10
    plot_h = height - pad_top - pad_bot

    is_leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days_in_year = 366 if is_leap else 365
    day_w = width / days_in_year

    typical = profile.composite_p50
    reference = typical if typical is not None else 50.0
    reference_y = height - pad_bot - (reference / 100) * plot_h
    reference_label = (
        t("report.typical_day_reference", value=f"{reference:.0f}")
        if typical is not None
        else t("report.calibrating_midpoint")
    )

    def _y_for(composite: float) -> float:
        return height - pad_bot - (composite / 100) * plot_h

    # Active days only — days with zero composite are skipped.
    points: list[tuple[date, float]] = sorted(
        (d, m.composite)
        for d, m in profile.days.items()
        if d.year == year and m.composite > 0
    )

    line_svg = ""
    dots_svg = ""
    if points:
        polyline_pts = " ".join(
            f"{((d - date(year, 1, 1)).days * day_w):.2f},{_y_for(c):.2f}"
            for d, c in points
        )
        # Gradient runs from bottom (low composite, cool palette) to top
        # (high composite, hot palette). Offsets mirror _color_for_composite.
        # y1=100% is the bottom of the SVG box (lower composite).
        gradient = """
<defs>
  <linearGradient id="sparkline-grad" x1="0" y1="100%" x2="0" y2="0%">
    <stop offset="0%"  stop-color="#6c9a8b"/>
    <stop offset="20%" stop-color="#6c9a8b"/>
    <stop offset="40%" stop-color="#9cab76"/>
    <stop offset="55%" stop-color="#cfbb62"/>
    <stop offset="65%" stop-color="#d6a75c"/>
    <stop offset="75%" stop-color="#d99058"/>
    <stop offset="85%" stop-color="#c66f49"/>
    <stop offset="100%" stop-color="#b04a3a"/>
  </linearGradient>
</defs>
"""
        line_svg = (
            gradient
            + f'<polyline points="{polyline_pts}" fill="none" '
            f'stroke="url(#sparkline-grad)" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round" '
            f'style="filter: drop-shadow(0 0 5px rgba(216,190,126,.30))"/>'
        )
        # Small dots at each data point so individual workdays are
        # identifiable even when adjacent (and hoverable for the tooltip).
        dots_svg = "".join(
            f'<circle cx="{((d - date(year, 1, 1)).days * day_w):.2f}" '
            f'cy="{_y_for(c):.2f}" r="2.5" '
            f'fill="{_color_for_composite(c, profile)}" '
            f'stroke="#1a1c19" stroke-width="0.6">'
            f'<title>{escape(t("report.sparkline_tooltip", date=i18n.day_month_label(d), value=f"{c:.0f}"))}</title>'
            f'</circle>'
            for d, c in points
        )

    # Vertical "today" marker — calendar anchor for the eye.
    today = _today_local()
    today_marker = ""
    if today.year == year:
        today_x = (today - date(year, 1, 1)).days * day_w
        today_marker = (
            f'<line x1="{today_x:.2f}" y1="{pad_top}" x2="{today_x:.2f}" '
            f'y2="{height - pad_bot}" stroke="#f5f3ed" stroke-width="0.6" '
            f'opacity="0.35"/>'
        )

    # Month tick labels under the strip.
    month_ticks = "".join(
        f'<text x="{((date(year, m_idx + 1, 1) - date(year, 1, 1)).days * day_w):.1f}" '
        f"y=\"{height - 1}\" font-size=\"8\" font-family='{FONT_MONO}' fill=\"{_INK_FAINT}\">"
        f'{escape(i18n.month_name(m_idx + 1, short=True))}</text>'
        for m_idx in (0, 2, 5, 8, 11)
    )

    return f"""
<svg class="year-sparkline" viewBox="0 0 {width} {height}" preserveAspectRatio="none">
  <rect x="0" y="{pad_top}" width="{width}" height="{plot_h}" fill="rgba(255,255,255,.03)"/>
  <line x1="0" y1="{reference_y:.1f}" x2="{width}" y2="{reference_y:.1f}"
        stroke="#6c9a8b" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>
  <text x="{width - 4}" y="{reference_y - 3:.1f}" text-anchor="end" font-size="9" fill="#6c9a8b">{reference_label}</text>
  {today_marker}
  {line_svg}
  {dots_svg}
  {month_ticks}
</svg>
""".strip()


# ---------------------------------------------------------------------------
# Section: month overview

def _render_all_months(
    profile: StressProfile,
    aggregates: dict[date, DayAggregate],
    now: date | None = None,
) -> str:
    """One section per month with activity (chronological), each containing
    its active-day drill-downs inlined directly below the heatmap.

    All month sections are hidden by default via CSS. The "default" month —
    today's month if it's in the data, otherwise the latest month with
    activity — is rendered with class="month-default" so the `:target`
    sibling-rules in CSS show it when nothing else is targeted. Clicking a
    year-overview cell sets a new :target and switches the visible month.
    """
    if not profile.days:
        return _render_no_data_panel(t("report.no_activity"))
    months = _months_with_activity(profile)
    if not months:
        return _render_no_data_panel(t("report.no_active_days"))
    today = now or _today_local()
    default_month: tuple[int, int] | None = None
    if (today.year, today.month) in months:
        default_month = (today.year, today.month)
    else:
        default_month = months[-1]
    return "\n".join(
        _render_month_section(y, m, profile, aggregates,
                              is_default=(y, m) == default_month)
        for y, m in months
    )


def _render_month_section(
    year: int,
    month: int,
    profile: StressProfile,
    aggregates: dict[date, DayAggregate],
    is_default: bool = False,
) -> str:
    """One month's KPIs + heatmap + inline drill-downs for active days."""
    days_in_month = [
        m for d, m in profile.days.items()
        if d.year == year and d.month == month
    ]
    active = [
        m for d, m in profile.days.items()
        if d.year == year and d.month == month
        and m.composite > 0
    ]
    composites = [m.composite for m in active]
    avg_composite = sum(composites) / len(composites) if composites else 0.0
    peak_day = max(
        active,
        key=lambda m: m.composite,
        default=None,
    )
    days_over_p75 = sum(
        1 for m in active
        if profile.composite_p75 is not None and m.composite > profile.composite_p75
    )
    off_hours_days = sum(1 for m in days_in_month if m.off_hours_minutes > 0)
    active_days = _active_days_in_month(profile, year, month)
    drilldowns = "\n".join(
        _render_day_section(d, profile, aggregates) for d in active_days
    )
    classes = "month-view"
    if is_default:
        classes += " month-default"
    return f"""
<section id="month-{year}-{month:02d}" class="{classes}">
  <h2>{t("report.month_overview", month_year=i18n.month_year_label(year, month))}</h2>
  <div class="summary-row">
    {_stat_card(t("stat.avg_composite"), composite_label(avg_composite), t("stat.out_of_100"),
                _status_for_composite(avg_composite, profile),
                _composite_target_note(profile))}
    {_stat_card(t("stat.peak_day"),
                composite_label(peak_day.composite) if peak_day else "—",
                f"&nbsp;{escape(i18n.day_month_label(peak_day.day))}" if peak_day else "",
                _status_for_composite(peak_day.composite if peak_day else 0, profile),
                _peak_target_note(profile),
                href=f"#day-{peak_day.day.isoformat()}" if peak_day else None)}
    {_stat_card(t("stat.days_over_p75"),
                f"{days_over_p75}",
                escape(t("stat.active_days_suffix", count=len(active))),
                _status_for_count(days_over_p75, [3, 6]),
                t("stat.healthy_under_4"))}
    {_stat_card(t("stat.off_hours_days"),
                f"{off_hours_days}",
                "",
                _status_for_count(off_hours_days, [0, 2]),
                t("stat.healthy_zero"))}
  </div>
  <div class="panel">
    {_render_heatmap(year, month, profile)}
  </div>
  {drilldowns}
</section>
""".strip()


def _render_no_data_panel(msg: str) -> str:
    return f'<section class="month-view"><div class="panel empty"><p>{escape(msg)}</p></div></section>'


def _stat_card(
    label: str, value: str, unit: str, status: str, target_note: str,
    href: str | None = None,
) -> str:
    status_class = f"status-{status}" if status else ""
    status_label = escape(t(f"status.{status}")) if status in (
        "good", "caution", "high") else ""
    tag = "a" if href else "div"
    href_attr = f' href="{href}"' if href else ""
    return f"""
<{tag} class="stat-card {status_class}"{href_attr}>
  <span class="status-label">{status_label}</span>
  <div class="label">{escape(label)}</div>
  <div class="value">{value}<span class="unit">{unit}</span></div>
  <div class="target-note">{target_note}</div>
</{tag}>
""".strip()


def _render_heatmap(year: int, month: int, profile: StressProfile) -> str:
    first = date(year, month, 1)
    # Number of days in the month
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    # Leading blanks: cells before the first weekday
    leading = first.weekday()
    cells = ["".join(
        f'<div class="dow-label">{escape(name)}</div>'
        for name in i18n.weekday_names_short()
    )]
    for _ in range(leading):
        cells.append('<div class="cell outside"></div>')
    d = first
    while d <= last:
        m = profile.days.get(d)
        cells.append(_heatmap_day_cell(d, m, profile))
        d += timedelta(days=1)
    # Trailing blanks to fill the last week
    trailing = (7 - (leading + last.day) % 7) % 7
    for _ in range(trailing):
        cells.append('<div class="cell outside"></div>')

    return f"""
<div class="heatmap">
  {"".join(cells)}
</div>
<div class="legend">
  {t("heatmap.legend")}
  <span class="legend-bar"></span>
</div>
""".strip()


def _heatmap_day_cell(day: date, metrics: DayMetrics | None, profile: StressProfile) -> str:
    # Empty: no metrics, no activity at all.
    if metrics is None or (metrics.composite == 0 and metrics.off_hours_minutes == 0):
        return f'<div class="cell zero"><span class="day-num">{day.day}</span></div>'

    # Any day with non-zero composite → linked drill-down, tinted by score
    # the way the widget card tints its pills: translucent fill + border in
    # the ramp colour, with the ramp colour itself as the ink. Solid pastel
    # fills would glare on the dark glass page. The deep-red high band gets
    # a lighter red ink (the card's error tone) for legibility.
    color = _color_for_composite(metrics.composite, profile)
    text_color = "#d98c80" if metrics.composite >= 75 else color
    return (
        f'<a class="cell" href="#day-{day.isoformat()}" '
        f'style="background:{color}26;border:1px solid {color}59;'
        f'color:{text_color}">'
        f'<span class="day-num">{day.day}</span>'
        f'<span class="day-val">{composite_label(metrics.composite)}</span>'
        f'</a>'
    )


# ---------------------------------------------------------------------------
# Section: recommendations

def _render_recommendations(profile: StressProfile) -> str:
    """Surface only patterns that the data actually shows. No false-flagging."""
    if not profile.days:
        return ""
    recs: list[str] = []

    # Pattern: sustained elevation
    if profile.composite_p75 is not None:
        consec = _max_consecutive_days_above(profile, profile.composite_p75)
        if consec >= 5:
            recs.append(_recommendation(
                title=t("recs.sustained.title", days=consec),
                severity="high",
                trigger=t("recs.sustained.trigger",
                          p75=composite_label(profile.composite_p75), days=consec),
                advice=t("recs.sustained.advice"),
                citation=t("recs.sustained.citation"),
            ))

    # Pattern: off-hours activity
    off_hours_days = sum(1 for m in profile.days.values() if m.off_hours_minutes > 0)
    if off_hours_days >= 2:
        recs.append(_recommendation(
            title=t("recs.offhours.title", days=off_hours_days),
            severity="medium",
            trigger=t("recs.offhours.trigger", days=off_hours_days),
            advice=t("recs.offhours.advice"),
            citation=t("recs.offhours.citation"),
        ))

    # Pattern: peak fan-out. Keyed on the engagement-weighted active peak, not
    # raw headcount — sessions left cooking in the background don't count as
    # exceeding working-memory capacity (you weren't actively tracking them).
    if profile.days:
        max_peak = max(m.codl_peak_active for m in profile.days.values())
        days_at_high_fanout = sum(
            1 for m in profile.days.values() if m.codl_peak_active >= 4
        )
        if days_at_high_fanout >= 3:
            recs.append(_recommendation(
                title=t("recs.fanout.title", days=days_at_high_fanout),
                severity="medium",
                trigger=t("recs.fanout.trigger", days=days_at_high_fanout,
                          max_peak=f"{max_peak:.1f}"),
                advice=t("recs.fanout.advice"),
                citation=t("recs.fanout.citation"),
            ))

    if not recs:
        return ""
    disclaimer = f'<div class="disclaimer">{t("recs.disclaimer")}</div>'
    return f"""
<section class="recommendations">
  <h2>{escape(t("recs.heading"))}</h2>
  <div class="rec-list">
    {"".join(recs)}
    {disclaimer}
  </div>
</section>
""".strip()


def _recommendation(title: str, severity: str, trigger: str, advice: str, citation: str) -> str:
    return f"""
<div class="rec rec-{severity}">
  <div class="rec-title">{title}</div>
  <div class="rec-trigger">{escape(t("recs.triggered_by"))} <code>{trigger}</code></div>
  <div class="rec-advice">{advice}</div>
  <div class="rec-cite">{escape(t("recs.basis"))} {citation}</div>
</div>
""".strip()


def _max_consecutive_days_above(profile: StressProfile, threshold: float) -> int:
    """Count the longest run of consecutive calendar days with activity above
    threshold. A run extends only when `d` is the immediate next calendar day
    after `prev`. A missing day (no activity entry) breaks the run."""
    from datetime import timedelta as _td

    days_sorted = sorted(profile.days.keys())
    best = 0
    run = 0
    prev: date | None = None
    for d in days_sorted:
        m = profile.days[d]
        if m.composite > threshold:
            if prev is not None and d == prev + _td(days=1):
                run += 1
            else:
                run = 1
            best = max(best, run)
        else:
            run = 0
        prev = d
    return best


# ---------------------------------------------------------------------------
# Section: day view

def _render_day_section(
    day: date,
    profile: StressProfile,
    aggregates: dict[date, DayAggregate],
) -> str:
    """One day's drill-down: chart + axis tiles, anchored for navigation."""
    metrics = profile.days.get(day)
    if metrics is None:
        return ""
    agg = aggregates.get(day)
    window_label = t("workwindow.unknown")
    if metrics.work_window_local:
        ws, we = metrics.work_window_local
        window_label = t(
            "workwindow.label",
            start=ws.strftime("%H:%M"), end=we.strftime("%H:%M"),
        )
    close_href = f"#month-{day.year}-{day.month:02d}"
    close = escape(t("day.close"), quote=True)
    return f"""
<section id="day-{day.isoformat()}" class="day-view" role="dialog" aria-modal="true">
  <a class="day-backdrop" href="{close_href}" aria-label="{escape(t("day.close_drilldown"), quote=True)}"></a>
  <div class="day-modal">
    <h3>
      {escape(i18n.day_label(day))}
      <a class="close-day" href="{close_href}" title="{close}" aria-label="{close}">&times;</a>
    </h3>
    <div class="panel">
      <div class="day-meta">
        <div><strong>{escape(t("day.composite", value=composite_label(metrics.composite)))}</strong></div>
        <div class="work-window">{escape(window_label)}</div>
      </div>
      {_render_day_chart(day, agg, metrics) if agg else ""}
    </div>
    {_render_axis_tiles(metrics, profile)}
  </div>
</section>
""".strip()


def _render_day_chart(day: date, agg: DayAggregate, metrics: DayMetrics) -> str:
    """Per-hour bar chart of concurrent stream count, bucketed by the
    user's local time so it aligns with the work-window band shown below.

    Includes a Y-axis with integer ticks + grid lines, an X-axis with
    3-hour labels, value labels on each bar, and a soft green shade over
    the configured work-window hours so the user can see which bars
    fall inside their typical work day.
    """
    if not agg.streams:
        return ""
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc

    # Per-LOCAL-hour count of concurrent streams (shared with the widgets).
    hour_counts = dayview.hour_counts(day, agg, local_tz)
    if not any(hour_counts):
        return ""
    max_count = max(hour_counts)

    chart_w, chart_h = 1100, 280
    # m_top gives the title + subtitle + legend their own band; nothing the
    # bars or their value labels can draw into.
    m_top, m_right, m_bottom, m_left = 68, 16, 44, 40
    plot_w = chart_w - m_left - m_right
    plot_h = chart_h - m_top - m_bottom
    bar_w = plot_w / 24

    # Work-window shade (anchored to local-time hours, matches the bar buckets).
    # The legend for it lives in the subtitle, not inside the band — keeps the
    # plot area free of overlapping text.
    work_band = ""
    work_window_legend = ""
    if metrics.work_window_local:
        ws, we = metrics.work_window_local
        ws_h = ws.hour + ws.minute / 60
        we_h = we.hour + we.minute / 60
        if we_h > ws_h:
            wx1 = m_left + ws_h * bar_w
            wx2 = m_left + we_h * bar_w
            work_band = (
                f'<rect x="{wx1:.1f}" y="{m_top}" '
                f'width="{wx2 - wx1:.1f}" height="{plot_h}" '
                f'fill="rgba(108,154,139,.10)" stroke="rgba(108,154,139,.18)" '
                f'stroke-width="0.5" rx="3"/>'
            )
            work_window_legend = (
                f' &middot; <tspan fill="#6c9a8b" font-weight="600">'
                f'&#9632;</tspan> '
                + t("chart.work_window_legend",
                    start=ws.strftime("%H:%M"), end=we.strftime("%H:%M"))
            )

    # Y-axis: gridlines and integer tick labels.
    y_axis_parts: list[str] = []
    for i in range(max_count + 1):
        y = m_top + plot_h - (i / max_count) * plot_h
        y_axis_parts.append(
            f'<line x1="{m_left}" y1="{y:.1f}" '
            f'x2="{m_left + plot_w}" y2="{y:.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>'
        )
        y_axis_parts.append(
            f'<text x="{m_left - 6}" y="{y + 3:.1f}" font-size="10" '
            f"text-anchor=\"end\" font-family='{FONT_MONO}' fill=\"{_INK_FAINT}\">{i}</text>"
        )
    # Y-axis label
    y_axis_parts.append(
        f'<text transform="rotate(-90)" x="{-(m_top + plot_h / 2):.1f}" '
        f'y="14" font-size="10" text-anchor="middle" fill="{_INK_FAINT}">'
        f'{escape(t("chart.y_label"))}</text>'
    )

    # Bars + per-bar value labels.
    bars: list[str] = []
    for h, c in enumerate(hour_counts):
        if c == 0:
            continue
        bar_px = (c / max_count) * plot_h
        x = m_left + h * bar_w + bar_w * 0.08
        y = m_top + plot_h - bar_px
        w = bar_w * 0.84
        col = codl_count_color(c)
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
            f'height="{bar_px:.1f}" fill="{col}" opacity="0.88" rx="2.5" '
            f'style="filter: drop-shadow(0 0 7px {col}55)"/>'
        )
        bars.append(
            f'<text x="{x + w / 2:.1f}" y="{y - 4:.1f}" font-size="10" '
            f"text-anchor=\"middle\" font-family='{FONT_MONO}' "
            f'fill="rgba(245,243,237,.85)" font-weight="700">{c}</text>'
        )

    # X-axis labels (every 3 hours).
    x_labels = "".join(
        f'<text x="{m_left + h * bar_w:.1f}" y="{m_top + plot_h + 16}" '
        f"font-size=\"10\" text-anchor=\"middle\" font-family='{FONT_MONO}' "
        f'fill="{_INK_FAINT}">{h:02d}</text>'
        for h in range(0, 25, 3)
    )
    x_caption = (
        f'<text x="{m_left + plot_w / 2:.1f}" y="{chart_h - 6}" '
        f'font-size="10" text-anchor="middle" fill="{_INK_FAINT}">'
        f'{escape(t("chart.x_caption"))}</text>'
    )
    x_axis_line = (
        f'<line x1="{m_left}" y1="{m_top + plot_h}" '
        f'x2="{m_left + plot_w}" y2="{m_top + plot_h}" '
        f'stroke="{_AXIS}" stroke-width="1"/>'
    )

    title = (
        f'<text x="{m_left}" y="22" font-size="13" font-weight="600" '
        f'fill="{_INK}">{escape(t("card.chart_title"))}</text>'
        f'<text x="{m_left}" y="42" font-size="11" fill="{_INK_SOFT}">'
        f'{t("chart.subtitle", count=max_count)}'
        f'{work_window_legend}'
        f'</text>'
    )

    return f"""
<svg viewBox="0 0 {chart_w} {chart_h}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;max-height:300px">
  {title}
  {work_band}
  {"".join(y_axis_parts)}
  {x_axis_line}
  {"".join(bars)}
  {x_labels}
  {x_caption}
</svg>
""".strip()


def _render_axis_tiles(metrics: DayMetrics, profile: StressProfile) -> str:
    tiles = "\n  ".join(
        _render_axis_tile(meta, metrics, profile) for meta in dayview.AXES
    )
    return f"""
<div class="axis-tiles">
  {tiles}
</div>
""".strip()


# Axis copy, zones, baselines, and the daily-view model live in dayview.py /
# scales.py — the single source of truth shared with the desktop widgets.


def _render_axis_tile(
    meta: dayview.AxisMeta,
    metrics: DayMetrics,
    profile: StressProfile,
) -> str:
    """Render one axis tile from the shared dayview model. Static copy
    (description/technique/basis/caveat), zones, baseline, and the value/unit
    formatting all come from dayview, so this HTML and the widgets agree."""
    tile = dayview.build_axis_tile(meta, metrics, profile)
    range_bar = _render_range_bar(
        value=tile.value,
        range_max=tile.range_max,
        zones=meta.zones,
        baseline=tile.baseline,
        baseline_label=tile.baseline_label,
        optimum=tile.optimum,
        optimum_label=tile.optimum_label,
        show_value=tile.has_data,
    )
    return f"""
<div class="tile">
  <div class="tile-head">
    <div class="tile-name">{escape(tile.name)}</div>
    <div class="tile-status status-{tile.status}">{escape(tile.zone_label)}</div>
  </div>
  <p class="tile-meaning">{escape(tile.description)}</p>
  {range_bar}
  <div class="tile-value-row">
    <div class="tile-value">{tile.value_label}</div>
    <div class="tile-unit">{escape(tile.unit_text)}</div>
  </div>
  <details class="tile-details">
    <summary>{t("tile.details_summary")}</summary>
    <div class="tile-detail-section">
      <span class="tile-detail-head">{escape(t("tile.technique"))}</span>
      <span class="tile-detail-body">{escape(tile.technique)}</span>
    </div>
    <div class="tile-detail-section">
      <span class="tile-detail-head">{escape(t("tile.basis"))}</span>
      <span class="tile-detail-body">{escape(tile.basis)}</span>
    </div>
    <div class="tile-detail-section">
      <span class="tile-detail-head">{escape(t("tile.caveat"))}</span>
      <span class="tile-detail-body">{escape(tile.caveat)}</span>
    </div>
  </details>
</div>
""".strip()


def _render_range_bar(
    *,
    value: float,
    range_max: float,
    zones: list[tuple[float, str, str]],
    baseline: float | None,
    baseline_label: str,
    optimum: float | None,
    optimum_label: str,
    show_value: bool = True,
) -> str:
    """Horizontal SVG range bar.

    Layout (top → bottom):
        y=10  baseline label (above bar)
        y=22  optimum label (above bar, stacked below baseline)
        y=30  bar top
        y=44  bar bottom
        y=54  zone-boundary tick numbers (under bar)
        y=68  "you VALUE" label (under bar, bold)
    """
    width = 320
    pad = 22
    inner = width - 2 * pad
    bar_y = 30
    bar_h = 14

    def _x(v: float) -> float:
        v = max(0.0, min(v, range_max))
        return pad + (v / range_max) * inner

    def _anchor(x: float) -> str:
        if x < pad + 22:
            return "start"
        if x > width - pad - 22:
            return "end"
        return "middle"

    # Zone-colored segments (colours via scales.zone_color, shared with widget).
    prev_upper = 0.0
    segments: list[str] = []
    boundary_ticks: list[str] = []
    for upper, status_class, _ in zones:
        capped_upper = min(upper, range_max)
        if capped_upper <= prev_upper:
            prev_upper = capped_upper
            continue
        x1 = _x(prev_upper)
        x2 = _x(capped_upper)
        segments.append(
            f'<rect x="{x1:.1f}" y="{bar_y}" width="{x2 - x1:.1f}" '
            f'height="{bar_h}" fill="{zone_color(status_class)}" '
            f'opacity="0.8"/>'
        )
        # Tick under bar at the upper boundary (except the very last).
        if capped_upper < range_max:
            tx = _x(capped_upper)
            tick_label = f"{capped_upper:g}"
            boundary_ticks.append(
                f'<text x="{tx:.1f}" y="54" font-size="9" text-anchor="middle" '
                f"font-family='{FONT_MONO}' fill=\"{_INK_FAINT}\">{tick_label}</text>"
            )
        prev_upper = capped_upper
        if prev_upper >= range_max:
            break

    # Endpoint labels (0 and max) — always shown for context.
    end_labels = (
        f'<text x="{pad}" y="54" font-size="9" text-anchor="start" '
        f"font-family='{FONT_MONO}' fill=\"{_INK_FAINT}\">0</text>"
        f'<text x="{width - pad}" y="54" font-size="9" text-anchor="end" '
        f"font-family='{FONT_MONO}' fill=\"{_INK_FAINT}\">{range_max:g}</text>"
    )

    # Reference markers (baseline, optimum) — drawn above the bar.
    extras: list[str] = []
    if baseline is not None and baseline > 0 and baseline <= range_max:
        bx = _x(baseline)
        extras.append(
            f'<line x1="{bx:.1f}" y1="{bar_y - 4}" x2="{bx:.1f}" '
            f'y2="{bar_y + bar_h + 4}" stroke="rgba(245,243,237,.40)" '
            f'stroke-width="1.2" stroke-dasharray="2 2"/>'
            f'<text x="{bx:.1f}" y="10" font-size="9" '
            f"text-anchor=\"{_anchor(bx)}\" font-family='{FONT_MONO}' "
            f'fill="rgba(245,243,237,.50)">{baseline_label}</text>'
        )
    if optimum is not None and optimum > 0 and optimum <= range_max:
        ox = _x(optimum)
        extras.append(
            f'<line x1="{ox:.1f}" y1="{bar_y - 4}" x2="{ox:.1f}" '
            f'y2="{bar_y + bar_h + 4}" stroke="{_OPTIMUM}" stroke-width="1.2" '
            f'stroke-dasharray="3 3" opacity="0.8"/>'
            f'<text x="{ox:.1f}" y="22" font-size="9" '
            f"text-anchor=\"{_anchor(ox)}\" font-family='{FONT_MONO}' "
            f'fill="{_OPTIMUM}">{optimum_label}</text>'
        )

    # User marker — the dominant element. Draw last so it sits on top. When the
    # axis has no data for the day (show_value=False, e.g. a day with no activity
    # at all), the scale is drawn for context but no marker is placed: a
    # 0-position marker would read as a perfect score rather than "not measured".
    if show_value:
        user_x = _x(value)
        off_scale = value > range_max
        user_label = escape(t(
            "marker.you_off_scale" if off_scale else "marker.you",
            value=f"{value:.2f}",
        ))
        user_marker = (
            f'<line x1="{user_x:.1f}" y1="{bar_y - 8}" x2="{user_x:.1f}" '
            f'y2="{bar_y + bar_h + 8}" stroke="#fff" stroke-width="2" '
            f'style="filter: drop-shadow(0 0 4px rgba(255,255,255,.6))"/>'
            f'<text x="{user_x:.1f}" y="68" font-size="10" '
            f"text-anchor=\"{_anchor(user_x)}\" font-family='{FONT_MONO}' "
            f'fill="{_INK}" font-weight="700">{user_label}</text>'
        )
    else:
        user_marker = (
            f'<text x="{width / 2:.1f}" y="68" font-size="10" '
            f'text-anchor="middle" fill="{_INK_FAINT}" font-style="italic">'
            f'{escape(t("marker.not_measured"))}</text>'
        )

    return f"""
<svg class="range-bar" viewBox="0 0 {width} 76" preserveAspectRatio="xMidYMid meet">
  {"".join(segments)}
  {"".join(boundary_ticks)}
  {end_labels}
  {"".join(extras)}
  {user_marker}
</svg>
""".strip()


# ---------------------------------------------------------------------------
# Section: methodology footer

def _render_methodology(profile: StressProfile, stats: AggregateStats | None) -> str:
    from ..core.config import load_config
    sc = load_config().scoring
    wsum = sum(sc.weights) or 1.0
    wpct = tuple(round(100 * w / wsum) for w in sc.weights)
    default_w = abs(sc.weights[0] - sc.weights[1]) < 1e-9 and abs(sc.weights[1] - sc.weights[2]) < 1e-9
    default_c = sc.codl_capacity == 4.0 and sc.codl_dose_horizon_minutes == 240.0 and sc.interruption_ceiling == 10.0
    if default_w and default_c:
        scoring_note = t("methodology.scoring_default")
    else:
        scoring_note = t(
            "methodology.scoring_custom",
            w_codl=wpct[0], w_interruption=wpct[1], w_closure=wpct[2],
            codl_capacity=f"{sc.codl_capacity:g}",
            codl_horizon=f"{sc.codl_dose_horizon_minutes:g}min",
            interruption_ceiling=f"{sc.interruption_ceiling:g}",
        )
    registry = load_registry()
    citations_html = "\n  ".join(
        f'<li><strong>{escape(c.authors)} ({c.year}).</strong> '
        f'<em>{escape(c.title)}.</em> {escape(c.venue)}.</li>'
        for c in sorted(registry.values(), key=lambda c: (c.authors, c.year))
    )
    cache_summary = ""
    if stats:
        cache_summary = "<p>" + t(
            "methodology.cache",
            hits=stats.cache_hits, misses=stats.cache_misses,
            write_errors=stats.cache_write_errors,
            malformed=stats.ingest.lines_skipped_malformed,
            no_timestamp=stats.ingest.lines_skipped_no_timestamp,
        ) + "</p>"
    tz_note = ""
    if profile.local_tz_name and profile.local_tz_name != "UTC":
        tz_note = t("methodology.tz_note", tz=escape(profile.local_tz_name))
    return f"""
<footer class="methodology">
  <details class="methodology-fold">
    <summary><h2>{t("methodology.heading")}</h2></summary>
    <div class="methodology-body">
      <p>
        {t("methodology.intro", version=escape(__version__), tz_note=tz_note)}
      </p>
      {cache_summary}
      <p>
        {t("methodology.caveats")}
      </p>
      <p>
        {t("methodology.fg_bg")}
      </p>
      <p>
        {t("methodology.scoring", scoring_note=scoring_note)}
      </p>
      <h3>{escape(t("methodology.research_basis"))}</h3>
      <ul class="cite-list">
      {citations_html}
      </ul>
    </div>
  </details>
</footer>
""".strip()


# ---------------------------------------------------------------------------
# Shared helpers

def _status_for_composite(score: float, profile: StressProfile) -> str:
    return composite_status(score, profile.composite_p75, profile.composite_p90)


def _status_for_count(n: int, thresholds: list[int]) -> str:
    """thresholds = [good_max, caution_max]. Above caution_max → high."""
    if n <= thresholds[0]:
        return "good"
    if n <= thresholds[1]:
        return "caution"
    return "high"


def _color_for_composite(score: float, profile: StressProfile) -> str:
    """Composite heat ramp: green → yellow → orange → red, anchored on the
    widget card's zone colours (scales.ZONE_COLORS: green/orange/red) with a
    yellow midpoint and blended in-between steps — so the heatmap, sparkline,
    and legend speak the same colour language as the card's zone bars."""
    if score <= 0:
        return "#2a2c27"  # neutral no-activity tone on the dark page
    bands = [
        (20, "#6c9a8b"),
        (40, "#9cab76"),
        (55, "#cfbb62"),
        (65, "#d6a75c"),
        (75, "#d99058"),
        (85, "#c66f49"),
        (100, "#b04a3a"),
    ]
    for top, color in bands:
        if score <= top:
            return color
    return "#b04a3a"


def _composite_target_note(profile: StressProfile) -> str:
    if profile.personal_optimum is None:
        return t("note.optimum_calibrating")
    return t("note.optimum", value=f"{profile.personal_optimum:.1f}")


def _peak_target_note(profile: StressProfile) -> str:
    if profile.composite_p90 is None:
        return t("note.calibrating")
    return t("note.p90", value=composite_label(profile.composite_p90))


# ---------------------------------------------------------------------------
# Document shell

def _selected_month_style(profile: StressProfile) -> str:
    """CSS that highlights the year-overview cell for the month currently in
    view — the default month on load, or whichever month cell was clicked.

    Driven entirely by :target/:has() so the report stays JS-free and the
    highlight always tracks the visible month section (which is shown by the
    same mechanism, see the `section.month-view` rules in `_STYLES`).
    """
    months = _months_with_activity(profile)
    if not months:
        return ""
    today = _today_local()
    default_month = (
        (today.year, today.month)
        if (today.year, today.month) in months
        else months[-1]
    )

    def anchor(year: int, month: int) -> str:
        return f"#month-{year}-{month:02d}"

    # One selector per month: when that month's section is the :target, light
    # up the cell linking to it. Plus the default month when nothing is targeted
    # — mirrors the `.month-default` visibility rule.
    selectors = [
        f':root:has({anchor(y, m)}:target) a.year-cell[href="{anchor(y, m)}"]'
        for y, m in months
    ]
    default_anchor = anchor(*default_month)
    selectors.append(
        ':root:not(:has(.month-view:target, .day-view:target)) '
        f'a.year-cell[href="{default_anchor}"]'
    )
    base = ",\n  ".join(selectors)
    hover = ",\n  ".join(f"{s}:hover" for s in selectors)
    return (
        f"  {base} {{\n"
        "    background: rgba(255,255,255,.09);\n"
        "    box-shadow: 0 0 0 1.5px var(--accent), 0 6px 16px -6px rgba(0,0,0,0.6);\n"
        "  }\n"
        f"  {hover} {{\n"
        "    transform: translateY(-1px);\n"
        "    box-shadow: 0 0 0 1.5px var(--accent), 0 10px 22px -6px rgba(0,0,0,0.65);\n"
        "  }\n"
    )


def _wrap_document(title: str, body_html: str, extra_css: str = "") -> str:
    dynamic = f"\n<style>\n{extra_css}</style>" if extra_css else ""
    return f"""<!DOCTYPE html>
<html lang="{escape(i18n.get_locale(), quote=True)}">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>{_STYLES}</style>{dynamic}
</head>
<body>
<div class="container">
{body_html}
</div>
</body>
</html>
"""


_STYLES = """
  :root {
    --bg: #161814;
    --panel: rgba(255, 255, 255, .045);
    --ink: rgba(245, 243, 237, .92);
    --ink-soft: rgba(245, 243, 237, .60);
    --ink-faint: rgba(245, 243, 237, .38);
    --rule: rgba(255, 255, 255, .085);
    --edge: rgba(255, 255, 255, .13);
    --accent: #efe9da;
    --good: #6c9a8b;
    --warn: #d99058;
    --bad: #b04a3a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; color: var(--ink);
    font-family: __FONT_UI__;
    font-size: 14px; line-height: 1.55; }
  body { background:
      radial-gradient(1100px 600px at 12% -8%, rgba(108,154,139,.10), transparent 60%),
      radial-gradient(900px 520px at 88% 0%, rgba(217,144,88,.07), transparent 55%),
      linear-gradient(178deg, #1d201c, #131512);
    background-attachment: fixed; background-color: var(--bg); }
  html { scroll-behavior: smooth; }
  /* Show one month at a time. By default, only `.month-default` (today's
     month if present, else the latest active month) is visible. Clicking
     a year-overview cell sets a :target on a month section, which
     overrides the default. A :target'd day-view keeps its parent month
     visible behind the modal. Uses :has(), supported in modern browsers
     (Chrome 105+ / Firefox 121+ / Safari 15.4+). */
  section.month-view { display: none; }
  section.month-view:target { display: block; }
  section.month-view:has(.day-view:target) { display: block; }
  :root:not(:has(.month-view:target, .day-view:target))
    section.month-view.month-default { display: block; }

  /* Day-view drill-downs are CSS-only modals. Hidden by default; the
     :target rule reveals them when their anchor is in the URL hash.
     The .day-backdrop link covers the viewport behind the modal — a
     click on it returns to the month anchor, closing the modal. */
  section.day-view { display: none; position: fixed; inset: 0; z-index: 1000;
    overflow-y: auto; }
  section.day-view:target { display: block; animation: fadein 0.12s ease; }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
  .day-backdrop { position: absolute; inset: 0; z-index: 0;
    background: rgba(8, 9, 8, 0.60); cursor: pointer;
    backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
  .day-modal { position: relative; z-index: 1;
    max-width: min(1320px, 92vw);
    margin: 24px auto; padding: 28px 36px 36px;
    background: linear-gradient(178deg, #22241f, #181a17);
    border: 1px solid var(--edge);
    border-radius: 24px;
    box-shadow: 0 36px 80px -24px rgba(0, 0, 0, .70),
                0 8px 24px -12px rgba(0, 0, 0, .50); }
  .container { max-width: 1100px; margin: 0 auto; padding: 40px 32px 80px; }
  h1 { font-size: 26px; font-weight: 650; letter-spacing: -.025em; margin: 0 0 4px; }
  h2 { font-size: 17px; font-weight: 650; letter-spacing: -.01em; margin: 36px 0 14px; }
  h3 { font-size: 13px; font-weight: 600; margin: 0 0 6px;
    text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-soft); }
  .subtitle { color: var(--ink-faint); font-size: 11px; margin: 0;
    font-family: __FONT_MONO__; letter-spacing: .02em; }
  header.report { border-bottom: 1px solid var(--rule); padding-bottom: 20px;
    margin-bottom: 36px; }
  /* Glass panel — the widget card's chrome, page-sized. */
  .panel { position: relative;
    background: linear-gradient(178deg, rgba(34, 36, 32, .60), rgba(24, 26, 23, .52));
    -webkit-backdrop-filter: blur(32px) saturate(150%);
    backdrop-filter: blur(32px) saturate(150%);
    border: 1px solid var(--edge);
    border-radius: 20px; padding: 20px 22px;
    box-shadow: 0 24px 60px -24px rgba(0,0,0,.60), 0 6px 18px -10px rgba(0,0,0,.45); }
  .panel::before { /* top inner highlight — the glass edge */
    content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
    background: linear-gradient(180deg, rgba(255,255,255,.10), transparent 18%);
    -webkit-mask: linear-gradient(180deg, #000 2%, transparent 30%);
    mask: linear-gradient(180deg, #000 2%, transparent 30%); }
  /* Agent analysis panel — distinct visual: green left rail, soft tinted
     background, so the reader knows this section is interpretation, not
     raw data. */
  section.agent-analysis { background: rgba(108,154,139,.10);
    border: 1px solid rgba(108,154,139,0.22);
    border-left: 4px solid var(--good);
    border-radius: 16px; padding: 20px 26px;
    margin: 8px 0 36px; }
  section.agent-analysis h2 { margin-top: 0; font-size: 17px;
    color: var(--good); }
  section.agent-analysis h3 { margin: 16px 0 6px; font-size: 13px;
    color: var(--ink); }
  .agent-analysis-body p { margin: 8px 0; }
  .agent-analysis-body ul { padding-left: 22px; margin: 8px 0; }
  .agent-analysis-body li { margin: 4px 0; }
  .agent-analysis-body code { background: rgba(255,255,255,0.08);
    padding: 1px 5px; border-radius: 4px; font-size: 12px; }
  .agent-analysis-body strong { color: var(--ink); }
  .agent-analysis-body a { color: var(--accent); }
  .panel.empty { padding: 40px; text-align: center; color: var(--ink-soft); }
  .note { font-size: 11px; color: var(--ink-faint); margin-top: 10px; font-style: italic; }
  /* Year overview */
  .year-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px;
    margin-bottom: 14px; }
  .year-cell { background: var(--panel); border: 1px solid var(--rule);
    border-radius: 14px; padding: 12px 10px;
    border-top: 3px solid var(--rule);
    text-decoration: none; color: inherit; display: block;
    transition: transform 0.08s ease, box-shadow 0.08s ease, background 0.08s ease; }
  a.year-cell:hover { transform: translateY(-1px); background: rgba(255,255,255,.07);
    box-shadow: 0 6px 16px rgba(0,0,0,0.35); cursor: pointer; }
  a.cell { text-decoration: none; color: inherit; display: block;
    transition: transform 0.08s ease, box-shadow 0.08s ease; }
  a.cell:hover { transform: translateY(-1px);
    box-shadow: 0 6px 16px rgba(0,0,0,0.4); cursor: pointer; }
  .year-cell.future { background: transparent; border: 1px dashed var(--rule);
    border-top-width: 1px; color: var(--ink-faint); }
  .year-cell.status-good { border-top-color: var(--good); }
  .year-cell.status-caution { border-top-color: var(--warn); }
  .year-cell.status-high { border-top-color: var(--bad); }
  .ym-month { font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--ink-soft); }
  .ym-value { font-size: 20px; font-weight: 650; margin-top: 2px;
    font-feature-settings: "tnum"; }
  .ym-trend { font-size: 11px; color: var(--ink-faint); margin-top: 2px; }
  .year-sparkline { height: 60px; width: 100%; margin-top: 10px; }
  /* Summary cards */
  .summary-row { display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 14px; margin-bottom: 18px; }
  .stat-card { background: var(--panel); border: 1px solid var(--rule);
    border-radius: 16px; padding: 14px 16px; position: relative;
    border-top: 3px solid var(--rule);
    text-decoration: none; color: inherit; display: block;
    transition: transform 0.08s ease, box-shadow 0.08s ease, background 0.08s ease; }
  a.stat-card:hover { transform: translateY(-1px); cursor: pointer;
    background: rgba(255,255,255,.07);
    box-shadow: 0 6px 16px rgba(0,0,0,0.35); }
  .stat-card.status-good { border-top-color: var(--good); }
  .stat-card.status-caution { border-top-color: var(--warn); }
  .stat-card.status-high { border-top-color: var(--bad); }
  .stat-card .label { font-size: 11px; text-transform: uppercase;
    color: var(--ink-faint); letter-spacing: 0.05em; margin-bottom: 4px;
    /* Reserve room for the absolutely-positioned status badge so a long
       label (e.g. "AVG COMPOSITE (WORK HOURS)") wraps clear of it. */
    padding-right: 76px; }
  .stat-card .value { font-size: 22px; font-weight: 650; line-height: 1.1;
    font-feature-settings: "tnum"; }
  .stat-card .unit { font-size: 12px; color: var(--ink-soft); margin-left: 4px;
    font-weight: 400; }
  .stat-card .target-note { font-size: 11px; color: var(--ink-faint);
    margin-top: 6px; }
  /* Status pills — the widget card's advice pill. */
  .stat-card .status-label { position: absolute; top: 12px; right: 14px;
    font-size: 9px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.12em; padding: 4px 9px 3px; border-radius: 999px;
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.09);
    color: var(--ink-soft); white-space: nowrap; }
  .stat-card.status-good .status-label { background: rgba(108,154,139,0.13);
    border-color: rgba(108,154,139,0.20); color: var(--good); }
  .stat-card.status-caution .status-label { background: rgba(217,144,88,0.13);
    border-color: rgba(217,144,88,0.20); color: var(--warn); }
  .stat-card.status-high .status-label { background: rgba(176,74,58,0.16);
    border-color: rgba(176,74,58,0.26); color: #d98c80; }
  /* Heatmap */
  .heatmap { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
  .dow-label { font-size: 11px; color: var(--ink-faint); text-transform: uppercase;
    letter-spacing: 0.05em; text-align: center; padding-bottom: 6px; }
  .cell { aspect-ratio: 1.4 / 1; border-radius: 8px; background: rgba(255,255,255,.03);
    padding: 7px 9px; position: relative; font-size: 11px; }
  .cell.outside { background: transparent; border: 1px dashed rgba(255,255,255,.06); }
  .cell.zero { background: rgba(255,255,255,.03); border: 1px solid var(--rule);
    color: var(--ink-faint); }
  .cell .day-num { font-weight: 600; font-size: 12px; }
  .cell .day-val { position: absolute; bottom: 7px; right: 9px;
    font-size: 11px; opacity: 0.75; font-feature-settings: "tnum"; }
  .legend { font-size: 11px; color: var(--ink-soft); margin-top: 14px; }
  .legend-bar { display: inline-block; width: 200px; height: 8px; border-radius: 4px;
    background: linear-gradient(to right, #6c9a8b, #cfbb62, #d99058, #b04a3a);
    vertical-align: middle; margin: 0 8px; }
  /* Recommendations — the widget card's nag/error boxes, expanded. */
  .rec-list { display: grid; gap: 12px; }
  .rec { border: 1px solid rgba(217,144,88,.22); border-left: 3px solid var(--warn);
    background: rgba(217,144,88,.10);
    padding: 14px 18px; border-radius: 12px; }
  .rec-high { border-color: rgba(176,74,58,.26); border-left-color: var(--bad);
    background: rgba(176,74,58,.12); }
  .rec-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
  .rec-trigger { font-size: 12px; color: var(--ink-soft); margin-bottom: 6px; }
  .rec-trigger code { background: rgba(255,255,255,0.07); padding: 1px 4px;
    border-radius: 4px; font-size: 11px; }
  .rec-advice { margin: 6px 0 8px; }
  .rec-cite { font-size: 11px; color: var(--ink-faint); font-style: italic; }
  .disclaimer { padding: 10px 14px; background: rgba(255,255,255,.04);
    border: 1px solid var(--rule); border-radius: 12px;
    font-size: 12px; color: var(--ink-soft); }
  /* Day view */
  .day-modal h3 { display: flex; justify-content: space-between; align-items: center;
    font-size: 16px; text-transform: none; letter-spacing: 0; color: var(--ink);
    margin: 0 0 14px; padding-bottom: 12px;
    border-bottom: 1px solid var(--rule); }
  .day-modal .close-day { font-size: 22px; line-height: 1;
    color: var(--ink-faint); text-decoration: none; font-weight: 400;
    width: 32px; height: 32px; border-radius: 8px;
    display: inline-flex; align-items: center; justify-content: center;
    transition: background 0.08s, color 0.08s; }
  .day-modal .close-day:hover { color: #d98c80; background: rgba(255,255,255,.07); }
  .day-meta { display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 6px; }
  .day-meta .work-window { font-size: 11px; color: var(--ink-faint);
    font-family: __FONT_MONO__; letter-spacing: .02em; }
  /* Axis tiles use subgrid for cross-tile row alignment so all three
     descriptions / bars / value rows line up even when the description
     text has slightly different lengths. Subgrid is widely supported in
     modern browsers; the fallback (no-subgrid) still gives a usable
     stacked layout — descriptions just need a min-height. */
  .axis-tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px;
    grid-template-rows: auto; margin-top: 22px;
    align-items: start; }
  .tile { background: var(--panel); border: 1px solid var(--rule);
    border-radius: 16px; padding: 16px 18px;
    display: grid;
    grid-template-rows: auto auto auto auto auto;
    row-gap: 8px;
    grid-row: span 5; }
  @supports (grid-template-rows: subgrid) {
    .axis-tiles { grid-template-rows: repeat(5, auto); }
    .tile { display: grid; grid-row: span 5;
      grid-template-rows: subgrid; }
  }
  .tile-head { display: flex; justify-content: space-between; align-items: center;
    gap: 8px; }
  .tile-name { font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--ink); font-weight: 700; flex: 1 1 auto; min-width: 0; }
  .tile-status { font-size: 9px; text-transform: uppercase; letter-spacing: 0.10em;
    padding: 4px 9px 3px; border-radius: 999px; white-space: nowrap; font-weight: 700;
    border: 1px solid transparent; }
  .tile-status.status-good { background: rgba(108,154,139,0.13);
    border-color: rgba(108,154,139,0.20); color: var(--good); }
  .tile-status.status-moderate { background: rgba(197,180,138,0.13);
    border-color: rgba(197,180,138,0.20); color: #c5b48a; }
  .tile-status.status-caution { background: rgba(217,144,88,0.13);
    border-color: rgba(217,144,88,0.20); color: var(--warn); }
  .tile-status.status-high { background: rgba(176,74,58,0.16);
    border-color: rgba(176,74,58,0.26); color: #d98c80; }
  .tile-meaning { font-size: 12px; color: var(--ink-soft); line-height: 1.5;
    margin: 0; min-height: 4.5em; }
  .range-bar { width: 100%; height: auto; max-height: 86px; display: block; }
  .tile-value-row { display: flex; align-items: baseline; gap: 10px;
    padding-top: 10px; border-top: 1px solid var(--rule); }
  .tile-value { font-size: 22px; font-weight: 650; font-feature-settings: "tnum"; }
  .tile-unit { font-size: 11px; color: var(--ink-faint); }
  .tile-details { font-size: 11px; color: var(--ink-soft); }
  .tile-details summary { cursor: pointer; padding: 4px 0;
    color: var(--ink-faint); user-select: none; }
  .tile-details summary:hover { color: var(--ink); }
  .tile-details[open] summary { color: var(--ink); margin-bottom: 6px; }
  .tile-detail-section { margin: 6px 0; }
  .tile-detail-head { font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--ink-faint); display: block;
    margin-bottom: 2px; }
  .tile-detail-body { font-size: 11px; color: var(--ink); display: block; }
  /* Methodology — collapsible, folded by default */
  footer.methodology { margin-top: 60px; border-top: 1px solid var(--rule);
    padding-top: 12px; font-size: 12px; color: var(--ink-soft); }
  footer.methodology h2 { color: var(--ink); font-size: 17px; margin: 0;
    display: inline; font-weight: 600; }
  footer.methodology h3 { color: var(--ink); font-size: 13px; }
  .methodology-fold summary { cursor: pointer; list-style: none;
    padding: 12px 0; user-select: none; display: flex; align-items: center;
    gap: 8px; }
  .methodology-fold summary::-webkit-details-marker { display: none; }
  .methodology-fold summary::before { content: "\\25B8"; color: var(--ink-faint);
    display: inline-block; transition: transform 0.15s ease;
    font-size: 14px; line-height: 1; }
  .methodology-fold[open] summary::before { transform: rotate(90deg); }
  .methodology-fold summary:hover::before { color: var(--ink); }
  .methodology-body { padding: 8px 0 0; }
  .cite-list { list-style: none; padding: 0; margin: 12px 0; }
  .cite-list li { padding: 6px 0; border-bottom: 1px solid var(--rule); }
  .cite-list li:last-child { border-bottom: none; }
  code { font-family: __FONT_MONO__; font-size: 11px; }
""".replace("__FONT_UI__", FONT_UI).replace("__FONT_MONO__", FONT_MONO)
