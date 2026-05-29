"""Render a StressProfile into a self-contained HTML report.

The output is one .html file with embedded CSS and SVG — no external assets,
no JS, opens identically online or offline. Each metric carries its
technique + research basis + caveat inline, per the project's
"surface-technique" design principle.

This is the v1 renderer. It produces a clean, scannable report with the same
information architecture as the mockup at `templates/report.html`, but the
visual treatment is intentionally plainer until the data pipeline has shaken
out. The mockup remains as the aspirational design target.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from html import escape

from . import __version__
from .aggregate import AggregateStats, DayAggregate
from .citations import load_registry
from . import dayview
from .metrics import (
    DayMetrics,
    StressProfile,
    WorkWindow,
    is_weekend,
)
from .scales import (
    composite_status,
    zone_color,
)

CALIBRATING_LABEL = "calibrating"


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
        f"Cognitive Stress Profile — {label}", body,
        extra_css=_selected_month_style(profile),
    )


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
  <h2>Top focus</h2>
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
    """Workdays in the given month with non-zero composite. Weekends are
    excluded because weekend activity is surfaced as off-hours only and
    doesn't get a per-day drill-down."""
    return sorted(
        d for d, m in profile.days.items()
        if d.year == year and d.month == month
        and m.composite > 0 and not is_weekend(d)
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
        src = (
            f" &middot; {ingest_stats.days_with_activity} active days / "
            f"{ingest_stats.days_in_window} in window &middot; "
            f"{i.events_emitted:,} events from {ingest_stats.ingest.lines_decoded:,} records"
        )
    return f"""
<header class="report">
  <h1>Cognitive Stress Profile</h1>
  <p class="subtitle">
    {escape(label)} &middot; generated {generated_at.strftime("%Y-%m-%d %H:%M %Z")}{src}
  </p>
</header>
""".strip()


# ---------------------------------------------------------------------------
# Section: year overview

def _render_year_overview(profile: StressProfile) -> str:
    """12 monthly cells with the avg composite for that month."""
    if not profile.days:
        return ""
    today = datetime.now(timezone.utc).date()
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
  <h2>{year} &middot; year overview</h2>
  <div class="panel">
    <div class="year-grid">
      {"".join(cells)}
    </div>
    {sparkline}
    <p class="note">
      Monthly cells show the average composite score over the month's active
      workdays. The line below shows each workday as a bar colored by its
      composite score (same palette as the calendar heatmap). The dashed
      green line marks your typical-day composite (p50) — a health reference.
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
    month_name = date(year, month, 1).strftime("%b")
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
        trend = f"{len(days_metrics)} day{'s' if len(days_metrics) != 1 else ''} active"
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
    height implicitly encodes the composite zone (low = blue-green,
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
        f"your typical day {reference:.0f}" if typical is not None
        else "global midpoint 50 (calibrating)"
    )

    def _y_for(composite: float) -> float:
        return height - pad_bot - (composite / 100) * plot_h

    # Active workdays only — weekend composites are 0 by design and would
    # drag the line to the baseline.
    points: list[tuple[date, float]] = sorted(
        (d, m.composite)
        for d, m in profile.days.items()
        if d.year == year and m.composite > 0 and not is_weekend(d)
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
    <stop offset="0%"  stop-color="#b0c4d4"/>
    <stop offset="20%" stop-color="#cfd8d2"/>
    <stop offset="40%" stop-color="#c9d4cd"/>
    <stop offset="55%" stop-color="#d4c897"/>
    <stop offset="65%" stop-color="#d8be7e"/>
    <stop offset="75%" stop-color="#d29c5e"/>
    <stop offset="85%" stop-color="#cd8a4f"/>
    <stop offset="100%" stop-color="#b04a3a"/>
  </linearGradient>
</defs>
"""
        line_svg = (
            gradient
            + f'<polyline points="{polyline_pts}" fill="none" '
            f'stroke="url(#sparkline-grad)" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        # Small dots at each data point so individual workdays are
        # identifiable even when adjacent (and hoverable for the tooltip).
        dots_svg = "".join(
            f'<circle cx="{((d - date(year, 1, 1)).days * day_w):.2f}" '
            f'cy="{_y_for(c):.2f}" r="2.5" '
            f'fill="{_color_for_composite(c, profile)}" '
            f'stroke="#fafaf7" stroke-width="0.6">'
            f'<title>{d.strftime("%a %d %b")}: composite {c:.0f}</title>'
            f'</circle>'
            for d, c in points
        )

    # Vertical "today" marker — calendar anchor for the eye.
    today = datetime.now(timezone.utc).date()
    today_marker = ""
    if today.year == year:
        today_x = (today - date(year, 1, 1)).days * day_w
        today_marker = (
            f'<line x1="{today_x:.2f}" y1="{pad_top}" x2="{today_x:.2f}" '
            f'y2="{height - pad_bot}" stroke="#1f2024" stroke-width="0.6" '
            f'opacity="0.35"/>'
        )

    # Month tick labels under the strip.
    month_ticks = "".join(
        f'<text x="{((date(year, m_idx + 1, 1) - date(year, 1, 1)).days * day_w):.1f}" '
        f'y="{height - 1}" font-size="8" fill="#8a8d96">'
        f'{date(year, m_idx + 1, 1).strftime("%b")}</text>'
        for m_idx in (0, 2, 5, 8, 11)
    )

    return f"""
<svg class="year-sparkline" viewBox="0 0 {width} {height}" preserveAspectRatio="none">
  <rect x="0" y="{pad_top}" width="{width}" height="{plot_h}" fill="#fafaf7" opacity="0.5"/>
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
        return _render_no_data_panel("No activity in window")
    months = _months_with_activity(profile)
    if not months:
        return _render_no_data_panel("No active days in window")
    today = now or datetime.now(timezone.utc).date()
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
    # Weekday-only metrics: averages and peaks exclude weekends so the KPIs
    # reflect work-week patterns, not what happened on Saturday.
    weekday_active = [
        m for d, m in profile.days.items()
        if d.year == year and d.month == month
        and m.composite > 0 and not is_weekend(d)
    ]
    composites = [m.composite for m in weekday_active]
    avg_composite = sum(composites) / len(composites) if composites else 0.0
    peak_day = max(
        weekday_active,
        key=lambda m: m.composite,
        default=None,
    )
    days_over_p75 = sum(
        1 for m in weekday_active
        if profile.composite_p75 is not None and m.composite > profile.composite_p75
    )
    # Off-hours days COUNTS every day with off-hours activity — both weekday
    # extensions past the work window AND any weekend activity at all.
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
  <h2>{date(year, month, 1).strftime("%B %Y")} &middot; month overview</h2>
  <div class="summary-row">
    {_stat_card("Avg composite (work hours)", f"{avg_composite:.0f}", "/100",
                _status_for_composite(avg_composite, profile),
                _composite_target_note(profile))}
    {_stat_card("Peak day",
                f"{peak_day.composite:.0f}" if peak_day else "—",
                f"&nbsp;{peak_day.day.strftime('%a %d %b')}" if peak_day else "",
                _status_for_composite(peak_day.composite if peak_day else 0, profile),
                _peak_target_note(profile),
                href=f"#day-{peak_day.day.isoformat()}" if peak_day else None)}
    {_stat_card("Days > personal p75",
                f"{days_over_p75}",
                f"/ {len(weekday_active)} active workdays",
                _status_for_count(days_over_p75, [3, 6]),
                "healthy &lt; 4")}
    {_stat_card("Off-hours days",
                f"{off_hours_days}",
                "",
                _status_for_count(off_hours_days, [0, 2]),
                "healthy = 0")}
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
    status_label = {"good": "ok", "caution": "watch",
                    "high": "elevated"}.get(status, "")
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
    cells = ['<div class="dow-label">Mon</div><div class="dow-label">Tue</div>'
             '<div class="dow-label">Wed</div><div class="dow-label">Thu</div>'
             '<div class="dow-label">Fri</div><div class="dow-label">Sat</div>'
             '<div class="dow-label">Sun</div>']
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
  Daily composite stress &middot; low to high
  <span class="legend-bar"></span>
</div>
""".strip()


def _heatmap_day_cell(day: date, metrics: DayMetrics | None, profile: StressProfile) -> str:
    weekend = is_weekend(day)

    # Empty: no metrics, no activity at all.
    if metrics is None or (metrics.composite == 0 and metrics.off_hours_minutes == 0):
        classes = "cell zero" + (" weekend" if weekend else "")
        return f'<div class="{classes}"><span class="day-num">{day.day}</span></div>'

    # Weekend with any activity → off-hours only, not clickable, amber tint.
    if weekend:
        mins = metrics.off_hours_minutes
        label = f"{mins // 60}h{mins % 60:02d}" if mins >= 60 else f"{mins}m"
        return (
            f'<div class="cell weekend off-hours" '
            f'title="weekend activity — {mins} min, counted as off-hours">'
            f'<span class="day-num">{day.day}</span>'
            f'<span class="day-val">{label}</span>'
            f'</div>'
        )

    # Weekday with non-zero composite → linked drill-down.
    color = _color_for_composite(metrics.composite, profile)
    text_color = "#fff" if metrics.composite >= 75 else "inherit"
    return (
        f'<a class="cell" href="#day-{day.isoformat()}" '
        f'style="background:{color};color:{text_color}">'
        f'<span class="day-num">{day.day}</span>'
        f'<span class="day-val">{metrics.composite:.0f}</span>'
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
                title=f"Sustained elevated load: {consec} consecutive workdays above your p75",
                severity="high",
                trigger=f"composite &ge; your p75 ({profile.composite_p75:.0f}) "
                        f"for {consec} consecutive days",
                advice=(
                    "Allostatic-load research is clear that cumulative wear "
                    "comes from the absence of low-load periods, not from "
                    "peak load alone. Consider a deliberately low-load day "
                    "in the current week to break the curve."
                ),
                citation="McEwen (1998) — <em>Protective and Damaging Effects of Stress Mediators</em>; recovery framing: Sonnentag &amp; Fritz (2007).",
            ))

    # Pattern: off-hours activity
    off_hours_days = sum(1 for m in profile.days.values() if m.off_hours_minutes > 0)
    if off_hours_days >= 2:
        recs.append(_recommendation(
            title=f"Off-hours engagement on {off_hours_days} days",
            severity="medium",
            trigger=f"&gt; 0 min of activity outside detected work window on "
                    f"{off_hours_days} day(s)",
            advice=(
                "Psychological detachment from work in off-hours predicts "
                "next-day vigor and longer-term well-being; off-hours "
                "engagement after a heavy week compounds rather than "
                "relieves load."
            ),
            citation="Sonnentag, Binnewies &amp; Mojza (2010) — <em>Staying well and engaged when demands are high</em>.",
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
                title=f"Parallel-stream fan-out exceeded WM capacity on {days_at_high_fanout} days",
                severity="medium",
                trigger=f"actively-supervised concurrent streams &ge; 4 on "
                        f"{days_at_high_fanout} workdays (max observed: {max_peak:.1f})",
                advice=(
                    "Supervisory-control studies identify fan-out limits "
                    "where performance degrades non-linearly and subjective "
                    "stress rises. Sequencing two streams often produces "
                    "equal throughput with materially lower cognitive cost."
                ),
                citation="Cummings &amp; Mitchell (2008) — <em>Predicting Controller Capacity in Supervisory Control of Multiple UAVs</em>; WM grounding: Cowan (2001).",
            ))

    if not recs:
        return ""
    disclaimer = (
        '<div class="disclaimer"><strong>Informational only.</strong> '
        'These signals are behavioral proxies for cognitive load and '
        'recovery, not a clinical assessment. For burnout evaluation, '
        'the Maslach Burnout Inventory (Maslach &amp; Jackson, 1981) is '
        'the validated instrument.</div>'
    )
    return f"""
<section class="recommendations">
  <h2>Patterns detected</h2>
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
  <div class="rec-trigger">Triggered by: <code>{trigger}</code></div>
  <div class="rec-advice">{advice}</div>
  <div class="rec-cite">Basis: {citation}</div>
</div>
""".strip()


def _max_consecutive_days_above(profile: StressProfile, threshold: float) -> int:
    """Count the longest run of consecutive *workdays* above threshold.
    A run extends only when `d` is the immediate next workday after `prev`
    (skipping weekends, never skipping weekdays). A missing workday breaks
    the run; a weekend gap does not."""
    from datetime import timedelta as _td

    def next_workday(d: date) -> date:
        d += _td(days=1)
        while is_weekend(d):
            d += _td(days=1)
        return d

    days_sorted = [d for d in sorted(profile.days.keys()) if not is_weekend(d)]
    best = 0
    run = 0
    prev: date | None = None
    for d in days_sorted:
        m = profile.days[d]
        if m.composite > threshold:
            if prev is not None and d == next_workday(prev):
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
    window_label = "auto-detected work hours"
    if metrics.work_window_local:
        ws, we = metrics.work_window_local
        window_label = f"work window: {ws.strftime('%H:%M')} – {we.strftime('%H:%M')}"
    close_href = f"#month-{day.year}-{day.month:02d}"
    return f"""
<section id="day-{day.isoformat()}" class="day-view" role="dialog" aria-modal="true">
  <a class="day-backdrop" href="{close_href}" aria-label="close drill-down"></a>
  <div class="day-modal">
    <h3>
      {day.strftime("%A %d %B %Y")}
      <a class="close-day" href="{close_href}" title="close" aria-label="close">&times;</a>
    </h3>
    <div class="panel">
      <div class="day-meta">
        <div><strong>Composite: {metrics.composite:.0f} / 100</strong></div>
        <div class="work-window">{window_label}</div>
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
    the auto-detected work-window hours so the user can see which bars
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
                f'fill="#6c9a8b" opacity="0.10"/>'
            )
            work_window_legend = (
                f' &middot; <tspan fill="#6c9a8b" font-weight="600">'
                f'&#9632;</tspan> work window '
                f'{ws.strftime("%H:%M")}&#8211;{we.strftime("%H:%M")}'
            )

    # Y-axis: gridlines and integer tick labels.
    y_axis_parts: list[str] = []
    for i in range(max_count + 1):
        y = m_top + plot_h - (i / max_count) * plot_h
        y_axis_parts.append(
            f'<line x1="{m_left}" y1="{y:.1f}" '
            f'x2="{m_left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e6e4dd" stroke-width="1"/>'
        )
        y_axis_parts.append(
            f'<text x="{m_left - 6}" y="{y + 3:.1f}" '
            f'font-size="10" text-anchor="end" fill="#8a8d96">{i}</text>'
        )
    # Y-axis label
    y_axis_parts.append(
        f'<text transform="rotate(-90)" x="{-(m_top + plot_h / 2):.1f}" '
        f'y="14" font-size="10" text-anchor="middle" fill="#8a8d96">'
        f'concurrent sessions</text>'
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
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
            f'height="{bar_px:.1f}" fill="#d99058" opacity="0.85" rx="2"/>'
        )
        bars.append(
            f'<text x="{x + w / 2:.1f}" y="{y - 4:.1f}" font-size="10" '
            f'text-anchor="middle" fill="#1f2024" font-weight="600">{c}</text>'
        )

    # X-axis labels (every 3 hours).
    x_labels = "".join(
        f'<text x="{m_left + h * bar_w:.1f}" y="{m_top + plot_h + 16}" '
        f'font-size="10" text-anchor="middle" fill="#8a8d96">{h:02d}</text>'
        for h in range(0, 25, 3)
    )
    x_caption = (
        f'<text x="{m_left + plot_w / 2:.1f}" y="{chart_h - 6}" '
        f'font-size="10" text-anchor="middle" fill="#8a8d96">'
        f'hour of day (local)</text>'
    )
    x_axis_line = (
        f'<line x1="{m_left}" y1="{m_top + plot_h}" '
        f'x2="{m_left + plot_w}" y2="{m_top + plot_h}" '
        f'stroke="#5a5d66" stroke-width="1"/>'
    )

    title = (
        f'<text x="{m_left}" y="22" font-size="13" font-weight="600" '
        f'fill="#1f2024">Concurrent agent sessions per hour</text>'
        f'<text x="{m_left}" y="42" font-size="11" fill="#5a5d66">'
        f'Bucketed by local hour &middot; peak {max_count} simultaneous'
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
# scales.py — the single source of truth shared with the tkinter + KDE widgets.


def _render_axis_tile(
    meta: dayview.AxisMeta,
    metrics: DayMetrics,
    profile: StressProfile,
) -> str:
    """Render one axis tile from the shared dayview model. Static copy
    (description/technique/basis/caveat), zones, baseline, and the value/unit
    formatting all come from dayview, so this HTML and the widgets agree."""
    t = dayview.build_axis_tile(meta, metrics, profile)
    range_bar = _render_range_bar(
        value=t.value,
        range_max=t.range_max,
        zones=meta.zones,
        baseline=t.baseline,
        baseline_label=t.baseline_label,
        optimum=t.optimum,
        optimum_label=t.optimum_label,
    )
    return f"""
<div class="tile">
  <div class="tile-head">
    <div class="tile-name">{escape(t.name)}</div>
    <div class="tile-status status-{t.status}">{escape(t.zone_label)}</div>
  </div>
  <p class="tile-meaning">{escape(t.description)}</p>
  {range_bar}
  <div class="tile-value-row">
    <div class="tile-value">{t.value_label}</div>
    <div class="tile-unit">{escape(t.unit_text)}</div>
  </div>
  <details class="tile-details">
    <summary>How this is computed &amp; what it can't tell you</summary>
    <div class="tile-detail-section">
      <span class="tile-detail-head">Technique</span>
      <span class="tile-detail-body">{escape(t.technique)}</span>
    </div>
    <div class="tile-detail-section">
      <span class="tile-detail-head">Research basis</span>
      <span class="tile-detail-body">{escape(t.basis)}</span>
    </div>
    <div class="tile-detail-section">
      <span class="tile-detail-head">Caveat</span>
      <span class="tile-detail-body">{escape(t.caveat)}</span>
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
            f'opacity="0.6"/>'
        )
        # Tick under bar at the upper boundary (except the very last).
        if capped_upper < range_max:
            tx = _x(capped_upper)
            tick_label = f"{capped_upper:g}"
            boundary_ticks.append(
                f'<text x="{tx:.1f}" y="54" font-size="9" '
                f'text-anchor="middle" fill="#8a8d96">{tick_label}</text>'
            )
        prev_upper = capped_upper
        if prev_upper >= range_max:
            break

    # Endpoint labels (0 and max) — always shown for context.
    end_labels = (
        f'<text x="{pad}" y="54" font-size="9" text-anchor="start" '
        f'fill="#8a8d96">0</text>'
        f'<text x="{width - pad}" y="54" font-size="9" text-anchor="end" '
        f'fill="#8a8d96">{range_max:g}</text>'
    )

    # Reference markers (baseline, optimum) — drawn above the bar.
    extras: list[str] = []
    if baseline is not None and baseline > 0 and baseline <= range_max:
        bx = _x(baseline)
        extras.append(
            f'<line x1="{bx:.1f}" y1="{bar_y - 4}" x2="{bx:.1f}" '
            f'y2="{bar_y + bar_h + 4}" stroke="#5a5d66" stroke-width="1.2" '
            f'stroke-dasharray="2 2"/>'
            f'<text x="{bx:.1f}" y="10" font-size="9" '
            f'text-anchor="{_anchor(bx)}" fill="#5a5d66">{baseline_label}</text>'
        )
    if optimum is not None and optimum > 0 and optimum <= range_max:
        ox = _x(optimum)
        extras.append(
            f'<line x1="{ox:.1f}" y1="{bar_y - 4}" x2="{ox:.1f}" '
            f'y2="{bar_y + bar_h + 4}" stroke="#355070" stroke-width="1.2" '
            f'stroke-dasharray="3 3"/>'
            f'<text x="{ox:.1f}" y="22" font-size="9" '
            f'text-anchor="{_anchor(ox)}" fill="#355070">{optimum_label}</text>'
        )

    # User marker — the dominant element. Draw last so it sits on top.
    user_x = _x(value)
    off_scale = value > range_max
    user_label = f"you {value:.2f}"
    if off_scale:
        user_label = f"you {value:.2f} ▶"
    user_marker = (
        f'<line x1="{user_x:.1f}" y1="{bar_y - 8}" x2="{user_x:.1f}" '
        f'y2="{bar_y + bar_h + 8}" stroke="#1f2024" stroke-width="2.5"/>'
        f'<text x="{user_x:.1f}" y="68" font-size="10" '
        f'text-anchor="{_anchor(user_x)}" fill="#1f2024" '
        f'font-weight="600">{user_label}</text>'
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
    registry = load_registry()
    citations_html = "\n  ".join(
        f'<li><strong>{escape(c.authors)} ({c.year}).</strong> '
        f'<em>{escape(c.title)}.</em> {escape(c.venue)}.</li>'
        for c in sorted(registry.values(), key=lambda c: (c.authors, c.year))
    )
    cache_summary = ""
    if stats:
        cache_summary = (
            f"<p>Cache: {stats.cache_hits} hits, {stats.cache_misses} misses, "
            f"{stats.cache_write_errors} write errors. "
            f"{stats.ingest.lines_skipped_malformed} malformed lines, "
            f"{stats.ingest.lines_skipped_no_timestamp} skipped (no timestamp).</p>"
        )
    tz_note = ""
    if profile.local_tz_name and profile.local_tz_name != "UTC":
        tz_note = f"Local timezone for work-hours interpretation: <code>{escape(profile.local_tz_name)}</code>."
    return f"""
<footer class="methodology">
  <details class="methodology-fold">
    <summary><h2>Methodology &amp; citations</h2></summary>
    <div class="methodology-body">
      <p>
        Generated by ai-code-cognitive-stress {escape(__version__)}. Inputs:
        local agent-coding session transcripts (Claude Code, Codex CLI, Aider,
    and any source plugins configured via `--source`).
        All processing is local; nothing leaves the machine.
        {tz_note}
      </p>
      {cache_summary}
      <p>
        <strong>Caveats inherent to v1:</strong>
        (1) Supervisory-control research was developed for UAV operators, not LLM
        users — the analogy to LLM oversight is plausible but unvalidated.
        (2) All axes are <em>taskload</em> (objective demand), not <em>workload</em>
        (subjective experience); correlation with felt overload is moderate
        (r &asymp; 0.4–0.6 across the HCI literature).
        (3) The Closure Deficit nets real git commits/merges against the loops
        opened each work window, but closures are attributed by count, not
        linked to specific sessions; with no git repos configured it falls
        back to a concurrency-presence proxy.
        (4) Personal optimum and percentiles require &ge; 14 days of activity;
        fewer days renders as "calibrating".
      </p>
      <p>
        <strong>Foreground vs background sessions.</strong> A session counts at
        full CODL weight only while you're actively driving it (within a short
        grace window of one of your messages). The rest of the time it is alive
        but "cooking" in the background and counts at a reduced weight (default
        0.25) — discounted, because you're not actively tracking it, but never
        zero: holding a pending intention still costs ~15–20% of ongoing-task
        capacity (Smith, 2003) and an open goal keeps occupying working memory
        until it's closed or planned (Masicampo &amp; Baumeister, 2011). So a
        session you leave running while you step away is penalised lightly, not
        as if you were juggling it.
      </p>
      <h3>Research basis</h3>
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
    if score <= 0:
        return "#efece5"
    bands = [
        (20, "#cfd8d2"),
        (40, "#c9d4cd"),
        (55, "#d4c897"),
        (65, "#d8be7e"),
        (75, "#d29c5e"),
        (85, "#cd8a4f"),
        (100, "#b04a3a"),
    ]
    for top, color in bands:
        if score <= top:
            return color
    return "#b04a3a"


def _composite_target_note(profile: StressProfile) -> str:
    if profile.personal_optimum is None:
        return f"optimum: {CALIBRATING_LABEL}"
    return f"optimum {profile.personal_optimum:.1f} CODL &middot; aim to stay near"


def _peak_target_note(profile: StressProfile) -> str:
    if profile.composite_p90 is None:
        return CALIBRATING_LABEL
    return f"your p90 = {profile.composite_p90:.0f}"


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
    today = datetime.now(timezone.utc).date()
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
        "    background: var(--panel);\n"
        "    box-shadow: 0 0 0 1.5px var(--accent), 0 6px 16px -6px rgba(53,80,112,0.35);\n"
        "  }\n"
        f"  {hover} {{\n"
        "    transform: translateY(-1px);\n"
        "    box-shadow: 0 0 0 1.5px var(--accent), 0 10px 22px -6px rgba(53,80,112,0.4);\n"
        "  }\n"
    )


def _wrap_document(title: str, body_html: str, extra_css: str = "") -> str:
    dynamic = f"\n<style>\n{extra_css}</style>" if extra_css else ""
    return f"""<!DOCTYPE html>
<html lang="en">
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
    --bg: #fafaf7;
    --panel: #ffffff;
    --ink: #1f2024;
    --ink-soft: #5a5d66;
    --ink-faint: #8a8d96;
    --rule: #e6e4dd;
    --accent: #355070;
    --good: #6c9a8b;
    --warn: #d99058;
    --bad: #b04a3a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px; line-height: 1.55; }
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
    background: rgba(20, 22, 28, 0.55); cursor: pointer;
    backdrop-filter: blur(2px); -webkit-backdrop-filter: blur(2px); }
  .day-modal { position: relative; z-index: 1;
    max-width: min(1320px, 92vw);
    margin: 24px auto; background: var(--bg); padding: 28px 36px 36px;
    border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35),
                0 4px 12px rgba(0, 0, 0, 0.15); }
  .container { max-width: 1100px; margin: 0 auto; padding: 40px 32px 80px; }
  h1 { font-size: 24px; font-weight: 600; margin: 0 0 4px; }
  h2 { font-size: 17px; font-weight: 600; margin: 36px 0 14px; }
  h3 { font-size: 13px; font-weight: 600; margin: 0 0 6px;
    text-transform: uppercase; letter-spacing: 0.04em; color: var(--ink-soft); }
  .subtitle { color: var(--ink-soft); font-size: 13px; margin: 0; }
  header.report { border-bottom: 1px solid var(--rule); padding-bottom: 20px;
    margin-bottom: 36px; }
  .panel { background: var(--panel); border: 1px solid var(--rule);
    border-radius: 8px; padding: 20px 22px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04), 0 4px 16px rgba(0,0,0,0.04); }
  /* Agent analysis panel — distinct visual: green left rail, soft tinted
     background, so the reader knows this section is interpretation, not
     raw data. */
  section.agent-analysis { background: #f3f6f3;
    border: 1px solid rgba(108,154,139,0.35);
    border-left: 4px solid var(--good);
    border-radius: 8px; padding: 20px 26px;
    margin: 8px 0 36px; }
  section.agent-analysis h2 { margin-top: 0; font-size: 17px;
    color: var(--good); }
  section.agent-analysis h3 { margin: 16px 0 6px; font-size: 13px;
    color: var(--ink); }
  .agent-analysis-body p { margin: 8px 0; }
  .agent-analysis-body ul { padding-left: 22px; margin: 8px 0; }
  .agent-analysis-body li { margin: 4px 0; }
  .agent-analysis-body code { background: rgba(0,0,0,0.05);
    padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  .agent-analysis-body strong { color: var(--ink); }
  .agent-analysis-body a { color: var(--accent); }
  .panel.empty { padding: 40px; text-align: center; color: var(--ink-soft); }
  .note { font-size: 11px; color: var(--ink-faint); margin-top: 10px; font-style: italic; }
  /* Year overview */
  .year-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px;
    margin-bottom: 14px; }
  .year-cell { background: #efece5; border-radius: 6px; padding: 12px 10px;
    border-top: 3px solid var(--rule);
    text-decoration: none; color: inherit; display: block;
    transition: transform 0.08s ease, box-shadow 0.08s ease; }
  a.year-cell:hover { transform: translateY(-1px);
    box-shadow: 0 2px 6px rgba(0,0,0,0.08); cursor: pointer; }
  a.cell { text-decoration: none; color: inherit; display: block;
    transition: transform 0.08s ease, box-shadow 0.08s ease; }
  a.cell:hover { transform: translateY(-1px);
    box-shadow: 0 2px 6px rgba(0,0,0,0.1); cursor: pointer; }
  .year-cell.future { background: transparent; border: 1px dashed var(--rule);
    color: var(--ink-faint); }
  .year-cell.status-good { border-top-color: var(--good); }
  .year-cell.status-caution { border-top-color: var(--warn); }
  .year-cell.status-high { border-top-color: var(--bad); }
  .ym-month { font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--ink-soft); }
  .ym-value { font-size: 20px; font-weight: 600; margin-top: 2px; }
  .ym-trend { font-size: 11px; color: var(--ink-soft); margin-top: 2px; }
  .year-sparkline { height: 60px; width: 100%; margin-top: 10px; }
  /* Summary cards */
  .summary-row { display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 14px; margin-bottom: 18px; }
  .stat-card { background: var(--panel); border: 1px solid var(--rule);
    border-radius: 8px; padding: 14px 16px; position: relative;
    border-top: 3px solid var(--rule);
    text-decoration: none; color: inherit; display: block;
    transition: transform 0.08s ease, box-shadow 0.08s ease; }
  a.stat-card:hover { transform: translateY(-1px); cursor: pointer;
    box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
  .stat-card.status-good { border-top-color: var(--good); }
  .stat-card.status-caution { border-top-color: var(--warn); }
  .stat-card.status-high { border-top-color: var(--bad); }
  .stat-card .label { font-size: 11px; text-transform: uppercase;
    color: var(--ink-faint); letter-spacing: 0.05em; margin-bottom: 4px; }
  .stat-card .value { font-size: 22px; font-weight: 600; line-height: 1.1; }
  .stat-card .unit { font-size: 12px; color: var(--ink-soft); margin-left: 4px;
    font-weight: 400; }
  .stat-card .target-note { font-size: 11px; color: var(--ink-faint);
    margin-top: 6px; }
  .stat-card .status-label { position: absolute; top: 12px; right: 14px;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;
    padding: 2px 6px; border-radius: 8px; background: rgba(0,0,0,0.04);
    color: var(--ink-soft); }
  .stat-card.status-good .status-label { background: rgba(108,154,139,0.15);
    color: var(--good); }
  .stat-card.status-caution .status-label { background: rgba(217,144,88,0.18);
    color: var(--warn); }
  .stat-card.status-high .status-label { background: rgba(176,74,58,0.15);
    color: var(--bad); }
  /* Heatmap */
  .heatmap { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
  .dow-label { font-size: 11px; color: var(--ink-faint); text-transform: uppercase;
    letter-spacing: 0.05em; text-align: center; padding-bottom: 6px; }
  .cell { aspect-ratio: 1.4 / 1; border-radius: 6px; background: #ececec;
    padding: 7px 9px; position: relative; font-size: 11px; }
  .cell.outside { background: transparent; border: 1px dashed var(--rule); }
  .cell.zero { background: #efece5; }
  .cell.weekend { color: var(--ink-faint); }
  .cell.weekend.zero { background: #f3efe5; }
  .cell.weekend.off-hours { background: rgba(217,144,88,0.18);
    color: var(--warn); cursor: help; }
  .cell.weekend.off-hours .day-val { color: var(--warn); font-weight: 600;
    opacity: 1; }
  .cell .day-num { font-weight: 600; font-size: 12px; }
  .cell .day-val { position: absolute; bottom: 7px; right: 9px;
    font-size: 11px; opacity: 0.7; }
  .legend { font-size: 11px; color: var(--ink-soft); margin-top: 14px; }
  .legend-bar { display: inline-block; width: 200px; height: 8px; border-radius: 4px;
    background: linear-gradient(to right, #cfd8d2, #d8be7e, #d29c5e, #b04a3a);
    vertical-align: middle; margin: 0 8px; }
  /* Recommendations */
  .rec-list { display: grid; gap: 12px; }
  .rec { border-left: 3px solid var(--warn); background: #fcf7ef;
    padding: 14px 18px; border-radius: 4px; }
  .rec-high { border-left-color: var(--bad); background: #fbf2ef; }
  .rec-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
  .rec-trigger { font-size: 12px; color: var(--ink-soft); margin-bottom: 6px; }
  .rec-trigger code { background: rgba(0,0,0,0.04); padding: 1px 4px;
    border-radius: 3px; font-size: 11px; }
  .rec-advice { margin: 6px 0 8px; }
  .rec-cite { font-size: 11px; color: var(--ink-faint); font-style: italic; }
  .disclaimer { padding: 10px 14px; background: #f4f1ea; border-radius: 4px;
    font-size: 12px; color: var(--ink-soft); }
  /* Day view */
  .day-modal h3 { display: flex; justify-content: space-between; align-items: center;
    font-size: 16px; text-transform: none; letter-spacing: 0; color: var(--ink);
    margin: 0 0 14px; padding-bottom: 12px;
    border-bottom: 1px solid var(--rule); }
  .day-modal .close-day { font-size: 22px; line-height: 1;
    color: var(--ink-faint); text-decoration: none; font-weight: 400;
    width: 32px; height: 32px; border-radius: 6px;
    display: inline-flex; align-items: center; justify-content: center;
    transition: background 0.08s, color 0.08s; }
  .day-modal .close-day:hover { color: var(--bad); background: var(--rule); }
  .day-meta { display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 6px; }
  .day-meta .work-window { font-size: 12px; color: var(--ink-soft); }
  /* Axis tiles use subgrid for cross-tile row alignment so all three
     descriptions / bars / value rows line up even when the description
     text has slightly different lengths. Subgrid is widely supported in
     modern browsers; the fallback (no-subgrid) still gives a usable
     stacked layout — descriptions just need a min-height. */
  .axis-tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px;
    grid-template-rows: auto; margin-top: 22px;
    align-items: start; }
  .tile { background: var(--panel); border: 1px solid var(--rule);
    border-radius: 8px; padding: 16px 18px;
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
    color: var(--ink); font-weight: 700; flex: 1 1 auto; min-width: 0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tile-status { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 3px 9px; border-radius: 10px; white-space: nowrap; font-weight: 600; }
  .tile-status.status-good { background: rgba(108,154,139,0.16); color: var(--good); }
  .tile-status.status-moderate { background: rgba(197,180,138,0.20); color: #8a7a4b; }
  .tile-status.status-caution { background: rgba(217,144,88,0.18); color: var(--warn); }
  .tile-status.status-high { background: rgba(176,74,58,0.16); color: var(--bad); }
  .tile-meaning { font-size: 12px; color: var(--ink); line-height: 1.5;
    margin: 0; min-height: 4.5em; }
  .range-bar { width: 100%; height: auto; max-height: 86px; display: block; }
  .tile-value-row { display: flex; align-items: baseline; gap: 10px;
    padding-top: 10px; border-top: 1px solid var(--rule); }
  .tile-value { font-size: 22px; font-weight: 600; }
  .tile-unit { font-size: 12px; color: var(--ink-soft); }
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
  code { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px; }
"""
