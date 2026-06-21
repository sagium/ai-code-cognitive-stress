"""Canonical daily-view model — the single source of truth for the per-day
drill-down shown in the HTML report and BOTH desktop widgets (KDE Plasma,
macOS Übersicht).

UI-agnostic: plain text only (Unicode, no HTML entities, no SVG, no QML).
`render.py` renders this to HTML/SVG for the report; `widget_card.py` renders
it to the widgets' HTML card (`aicogstress --emit-html-card`); and
`dayview_to_dict` serialises it as JSON for any other external display
(`aicogstress --emit-json`). Sharing this module is what keeps the report and
the widgets from drifting — the same role `scales.py` plays for zones/colours.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from ..pipeline.aggregate import DayAggregate, get_day_aggregates
from ..core import i18n
from ..core.config import load_config
from ..core.i18n import t, tn
from ..pipeline.metrics import (
    CODL_NORMALISATION_CEILING,
    OFF_HOURS_LOAD_CEILING_MIN,
    OFF_HOURS_LOAD_MAX_POINTS,
    DayMetrics,
    StressProfile,
    WorkWindow as _MetricsWorkWindow,
    _default_window,
    alive_intervals,
    build_profile,
    per_day_metrics,
)
from .scales import (
    CLOSURE_RANGE_MAX,
    CLOSURE_ZONES,
    CODL_ZONES,
    INTERRUPTION_RANGE_MAX,
    INTERRUPTION_ZONES,
    codl_count_color,
    composite_advice,
    composite_color,
    composite_label,
    composite_status,
    zone_color,
    zone_for,
)

Zone = tuple[float, str, str]


# ---------------------------------------------------------------------------
# Per-axis static metadata. The display copy (name/description/technique/
# basis/caveat) lives in the i18n catalog (locales/<locale>.json) under
# `axis.<key>.*` and is resolved at tile-build time, so the report and the
# widgets share one translated source and the locale can be chosen at runtime.
# The translated text is plain (the HTML layer escapes it, the widget shows it
# verbatim).

@dataclass(frozen=True, slots=True)
class AxisMeta:
    key: str
    range_max: float
    zones: list[Zone]
    has_optimum: bool


AXES: tuple[AxisMeta, ...] = (
    AxisMeta(
        key="codl",
        range_max=CODL_NORMALISATION_CEILING,
        zones=CODL_ZONES,
        has_optimum=True,
    ),
    AxisMeta(
        key="interruption",
        range_max=INTERRUPTION_RANGE_MAX,
        zones=INTERRUPTION_ZONES,
        has_optimum=False,
    ),
    AxisMeta(
        key="closure",
        range_max=CLOSURE_RANGE_MAX,
        zones=CLOSURE_ZONES,
        has_optimum=False,
    ),
)
AXES_BY_KEY: dict[str, AxisMeta] = {a.key: a for a in AXES}


def _axis_value(key: str, m: DayMetrics) -> float:
    return {
        "codl": m.codl_avg,
        "interruption": m.interruption_rate,
        "closure": m.closure_deficit,
    }[key]


def _axis_unit(key: str, m: DayMetrics) -> str:
    if key == "codl":
        return t("axis.unit.codl", peak=m.codl_peak)
    if key == "interruption":
        return t("axis.unit.interruption")
    if m.closure_deficit is None:
        return t("axis.unit.closure_not_scored")
    return t("axis.unit.closure", percent=f"{m.closure_deficit * 100:.0f}")


# ---------------------------------------------------------------------------
# Structured model

@dataclass(frozen=True, slots=True)
class Segment:
    start: float        # 0..1 fraction of range_max
    end: float
    color: str
    status: str


@dataclass(frozen=True, slots=True)
class Tick:
    fraction: float     # 0..1
    label: str


@dataclass(frozen=True, slots=True)
class AxisTile:
    key: str
    name: str
    description: str
    value: float
    value_label: str
    unit_text: str
    range_max: float
    has_data: bool      # False → axis had no data this day (e.g. no activity at all)
    status: str
    zone_label: str
    color: str
    fraction: float
    off_scale: bool
    baseline: float | None
    baseline_fraction: float | None
    baseline_label: str
    optimum: float | None
    optimum_fraction: float | None
    optimum_label: str
    segments: list[Segment]
    boundary_ticks: list[Tick]
    technique: str
    basis: str
    caveat: str


@dataclass(frozen=True, slots=True)
class WorkWindow:
    start: str          # "HH:MM"
    end: str
    start_hour: float   # 0..24 (for chart shading)
    end_hour: float


@dataclass(frozen=True, slots=True)
class DayView:
    day: date
    day_label: str          # "Friday 29 May 2026"
    has_activity: bool
    composite: float
    composite_label: str    # "37" | "—"
    composite_status: str
    composite_color: str
    advice: str             # one-to-two word read on the level
    work_window: WorkWindow | None
    work_window_label: str  # "work window: 09:00 – 17:00" | "work window: (unknown)"
    hours: list[int]        # 24 per-local-hour concurrent-stream counts
    hour_colors: list[str]  # 24 per-hour bar colours, by CODL zone of the count
    peak_concurrent: int
    # Cumulative composite at each hour-end of the work window (how the score
    # built up over the day), each point carrying its zone colour. The last
    # point equals `composite`.
    score_progression: list["ScorePoint"]
    axes: list[AxisTile]
    off_hours_minutes: int      # engaged minutes past the window end (or
                                # outlier-early, beyond the early-start grace)
    # Non-empty when off-hours work is contributing meaningfully to the composite
    # (≥ 15 min, ≥ ~5 pts). Rendered as an amber nag banner by the widget so
    # the user understands why the score just jumped. Empty → no banner.
    off_hours_nag: str
    # True only once the current wall-clock time is PAST the work window end for
    # this day — the 3 axis scores are final and won't update from ongoing
    # sessions. Before the window start the day is still ahead, so axes are live
    # (not frozen) and early-grace work already counts toward the score.
    axes_frozen: bool = False
    # Subjective grade prompt: True when it's the final work hour, the day has
    # activity, and no grade has been recorded yet. False at all other times and
    # for all non-today views (Week/Month/Year).
    grade_prompt: bool = False
    # Today's stored subjective grade (0=chill, 1=heated, 2=cooked), or None
    # when no grade has been recorded. Non-today views always carry None.
    grade_value: int | None = None


# ---------------------------------------------------------------------------
# Builders

def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def personal_baseline(profile: StressProfile, attr: str) -> float | None:
    """Median of `attr` across active days (>=3 samples), else None.
    Lifted verbatim from render.py so both UIs show the same 'typical day'."""
    values = sorted(
        getattr(m, attr) for m in profile.days.values()
        if m.composite > 0
        and getattr(m, attr, None) is not None and getattr(m, attr) > 0
    )
    if len(values) < 3:
        return None
    n = len(values)
    if n % 2:
        return values[n // 2]
    return (values[n // 2 - 1] + values[n // 2]) / 2


def hour_counts(
    day: date,
    agg: DayAggregate | None,
    local_tz: tzinfo,
    idle_close_minutes: int | None = None,
) -> list[int]:
    """Per-LOCAL-hour PEAK count of concurrent streams (24 buckets).

    For each local hour we take the maximum number of streams alive at once
    *anywhere within that hour*, computed from the streams' run intervals
    (`alive_intervals`: each session's [first_ts, last_ts] span minus any idle
    gap long enough — `idle_close_minutes` — that the app was closed and
    relaunched) clipped to the hour — not a single snapshot. A single mid-hour
    sample (e.g. at :30) silently dropped any session that began and ended
    between samples, so brief sessions, and sessions worked before the work
    window starts, vanished from the chart. Peak-over-the-hour guarantees every
    session that touched an hour registers there (≥ 1), while keeping true
    concurrency semantics: two *sequential* sessions in one hour read as 1, not
    2, and a session counts only while its app was plausibly open. Shared by the
    report (render.py) and both widgets, so they stay in sync.

    `idle_close_minutes` defaults to the configured value (config.json).
    """
    if agg is None or not agg.streams:
        return [0] * 24
    if idle_close_minutes is None:
        idle_close_minutes = load_config().idle_close_minutes
    idle_close_seconds = idle_close_minutes * 60
    intervals_by_stream = [
        alive_intervals(s, idle_close_seconds) for s in agg.streams
    ]
    counts: list[int] = []
    for h in range(24):
        h0 = datetime.combine(day, time(h, 0), tzinfo=local_tz).astimezone(timezone.utc)
        h1 = h0 + timedelta(hours=1)
        # Each run interval's overlap with this hour. Positive-duration overlaps
        # clip to the hour; a zero-duration run (single-event session) counts as a
        # point when it falls inside the hour. A run ending exactly on the hour
        # boundary has no presence in the next hour (clip is empty).
        spans: list[tuple[datetime, datetime]] = []
        for intervals in intervals_by_stream:
            for a0, b0 in intervals:
                a = max(a0, h0)
                b = min(b0, h1)
                if a < b:
                    spans.append((a, b))
                elif a0 == b0 and h0 <= a0 < h1:
                    spans.append((a0, a0))
        counts.append(_peak_overlap(spans))
    return counts


def _peak_overlap(spans: list[tuple[datetime, datetime]]) -> int:
    """Maximum number of intervals overlapping at any instant (sweep line).
    A zero-length span [t, t] still contributes 1 at t (opens are processed
    before closes on ties)."""
    if not spans:
        return 0
    points: list[tuple[datetime, int]] = []
    for a, b in spans:
        points.append((a, +1))
        points.append((b, -1))
    points.sort(key=lambda p: (p[0], -p[1]))
    peak = running = 0
    for _, delta in points:
        running += delta
        if running > peak:
            peak = running
    return peak


def _zone_segments_and_ticks(
    zones: list[tuple[float, str, str]], rmax: float,
) -> tuple[list[Segment], list[Tick]]:
    """Zone fill segments + inner boundary ticks for the range bar (same walk as
    the HTML range bar). Shared by the scored and no-data tile paths."""
    segments: list[Segment] = []
    ticks: list[Tick] = []
    prev_upper = 0.0
    for upper, status_class, _ in zones:
        capped = min(upper, rmax)
        if capped <= prev_upper:
            prev_upper = capped
            continue
        segments.append(Segment(
            start=_clamp01(prev_upper / rmax), end=_clamp01(capped / rmax),
            color=zone_color(status_class), status=status_class,
        ))
        if capped < rmax:
            ticks.append(Tick(fraction=_clamp01(capped / rmax), label=f"{capped:g}"))
        prev_upper = capped
        if prev_upper >= rmax:
            break
    return segments, ticks


def build_axis_tile(meta: AxisMeta, m: DayMetrics, profile: StressProfile) -> AxisTile:
    raw = _axis_value(meta.key, m)
    rmax = meta.range_max
    segments, ticks = _zone_segments_and_ticks(meta.zones, rmax)

    # No-data axis: render the empty scale with a neutral "not scored" state
    # rather than a 0 that reads as a perfect score. With the resumption-based
    # Closure Deficit this only fires on a day with no activity at all; the path
    # is kept for that edge and for any future axis that can genuinely lack data.
    if raw is None:
        return AxisTile(
            key=meta.key, name=t(f"axis.{meta.key}.name"),
            description=t(f"axis.{meta.key}.description"),
            value=0.0, value_label="—", unit_text=_axis_unit(meta.key, m),
            range_max=rmax, has_data=False, status="",
            zone_label=t("zone.not_scored"),
            color=zone_color(""), fraction=0.0, off_scale=False,
            baseline=None, baseline_fraction=None,
            baseline_label=t("axis.baseline_label"),
            optimum=None, optimum_fraction=None,
            optimum_label=t("axis.optimum_label") if meta.has_optimum else "",
            segments=segments, boundary_ticks=ticks,
            technique=t(f"axis.{meta.key}.technique"),
            basis=t(f"axis.{meta.key}.basis"),
            caveat=t(f"axis.{meta.key}.caveat"),
        )

    value = raw
    status, zone_label_key = zone_for(value, meta.zones)

    baseline = personal_baseline(profile, {
        "codl": "codl_avg", "interruption": "interruption_rate",
        "closure": "closure_deficit",
    }[meta.key])
    baseline_frac = (
        _clamp01(baseline / rmax)
        if baseline is not None and 0 < baseline <= rmax else None
    )
    optimum = profile.personal_optimum if meta.has_optimum else None
    optimum_frac = (
        _clamp01(optimum / rmax)
        if optimum is not None and 0 < optimum <= rmax else None
    )

    return AxisTile(
        key=meta.key, name=t(f"axis.{meta.key}.name"),
        description=t(f"axis.{meta.key}.description"),
        value=value, value_label=f"{value:.2f}", unit_text=_axis_unit(meta.key, m),
        range_max=rmax, has_data=True, status=status,
        zone_label=t(zone_label_key),
        color=zone_color(status), fraction=_clamp01(value / rmax) if rmax else 0.0,
        off_scale=value > rmax,
        baseline=baseline, baseline_fraction=baseline_frac,
        baseline_label=t("axis.baseline_label"),
        optimum=optimum, optimum_fraction=optimum_frac,
        optimum_label=t("axis.optimum_label") if meta.has_optimum else "",
        segments=segments, boundary_ticks=ticks,
        technique=t(f"axis.{meta.key}.technique"),
        basis=t(f"axis.{meta.key}.basis"),
        caveat=t(f"axis.{meta.key}.caveat"),
    )


@dataclass(frozen=True, slots=True)
class ScorePoint:
    value: float        # cumulative composite 0–100 at this hour-end
    color: str          # zone colour for that level (drives the gradient)


# ---------------------------------------------------------------------------
# Period (week / month) model. A period reuses the DayView container — the same
# header, axis tiles, and sparkline — but its composite is the mean over the
# window's ACTIVE days, its axis tiles are built from the period-mean of each
# axis (so they share the day card's zones / optimum / baseline), and its body
# shows a per-day composite chart instead of the per-hour concurrency chart.

# Colour for a no-activity day in the per-day series — a dim neutral so empty
# days don't read as a (green) low-stress score.
_ZERO_DAY_COLOR = "rgba(245,243,237,.16)"


@dataclass(frozen=True, slots=True)
class DailyPoint:
    day: date
    composite: float    # 0–100; 0 on a day with no activity
    color: str          # zone colour for that level (dim neutral when 0)


@dataclass(frozen=True, slots=True)
class MonthlyPoint:
    year: int
    month: int          # 1–12
    composite: float    # mean over the month's active days; 0 when none
    color: str          # zone colour for that level (dim neutral when 0)


@dataclass(frozen=True, slots=True)
class TimeframeView:
    """One selectable tab in the widget card: a label plus the DayView to draw.
    `daily` carries the per-day composite series for the week/month body chart;
    `monthly` carries the per-month series for the year chart. Both are empty
    for the live 'today' view (which draws the per-hour chart)."""
    key: str            # "today" | "week" | "month" | "year"
    tab_label: str      # localized short label for the tab button
    view: DayView
    daily: tuple[DailyPoint, ...] = ()
    monthly: tuple[MonthlyPoint, ...] = ()


def _truncate_aggregate(agg: DayAggregate, cutoff: datetime) -> DayAggregate:
    """Copy of `agg` containing only activity at or before `cutoff` (UTC) — the
    day as it existed at that instant. Streams born later are dropped; surviving
    streams have their last event, message timestamps, and resume gaps clipped.
    Per-stream counts stay whole-day, but per_day_metrics apportions them by
    lifetime overlap with the window, so the clipped lifetime scales them to
    'so far' under a uniform-rate assumption."""
    streams = tuple(
        replace(
            s,
            last_ts=min(s.last_ts, cutoff),
            user_msg_timestamps=tuple(
                t for t in s.user_msg_timestamps if t <= cutoff
            ),
            resume_gaps=tuple(g for g in s.resume_gaps if g[0] <= cutoff),
        )
        for s in agg.streams
        if s.first_ts <= cutoff
    )
    return replace(agg, streams=streams)


def score_progression(
    metrics: DayMetrics,
    agg: DayAggregate | None,
    profile: StressProfile,
    local_tz: tzinfo,
) -> list[ScorePoint]:
    """Cumulative composite (0–100) at each hour-end of the work window — the
    score 'so far' as the day fills, computed with the same engine as the
    headline composite. Each point carries its zone colour so the sparkline can
    render as a severity gradient. Both the window AND the event data are
    truncated at each hour-end: without the data cut, the afternoon's activity
    would count as 'off-hours past the window end' at the morning points,
    painting the early sparkline red on a perfectly ordinary day. The final
    point equals the day's composite as of the window end (off-hours work after
    the window can still lift the headline above it). Empty when there is no
    work window or no activity."""
    if agg is None or not agg.streams or metrics.work_window_local is None:
        return []
    weekday = metrics.day.weekday()
    # Use the INFERRED band (w0, w1), NOT metrics.work_window_local — whose start
    # may have dipped into the early-start grace zone. per_day_metrics derives
    # both the scored-start extension and the off-hours cutoff from the window
    # start it is handed, so passing the already-extended start would slide the
    # off-hours boundary down and misclassify pre-grace activity. Feeding the
    # inferred band makes each truncated point reproduce the headline composite's
    # classification exactly (the final point then equals the headline). Mirrors
    # build_profile's own window selection.
    iw = profile.work_windows.get(weekday) or _default_window(weekday)
    ws, we = iw.start, iw.end
    points: list[ScorePoint] = []
    for h in range(ws.hour + 1, we.hour + 1):
        cutoff = datetime.combine(
            metrics.day, time(h, 0), tzinfo=local_tz,
        ).astimezone(timezone.utc)
        window = _MetricsWorkWindow(weekday=weekday, start=ws, end=time(h, 0))
        value = per_day_metrics(
            _truncate_aggregate(agg, cutoff), window, local_tz,
        ).composite
        status = composite_status(value, profile.composite_p75, profile.composite_p90)
        points.append(ScorePoint(value=value, color=composite_color(status)))
    return points


def _off_hours_when_label(ranges: tuple[tuple[time, time], ...]) -> str:
    """Local-time ranges of the off-hours minutes, e.g. "11:12–11:50".
    Caps at 3 ranges ("+N more") so the banner stays one readable line."""
    if not ranges:
        return ""
    shown = ranges[:3]
    label = ", ".join(
        f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}" for s, e in shown
    )
    extra = len(ranges) - len(shown)
    return label + (t("nag.more", count=extra) if extra > 0 else "")


def build_dayview(
    metrics: DayMetrics,
    agg: DayAggregate | None,
    profile: StressProfile,
    local_tz: tzinfo,
    now: datetime | None = None,
    archive_dir=None,
) -> DayView:
    counts = hour_counts(metrics.day, agg, local_tz)
    ww = None
    ww_label = t("workwindow.unknown")
    if metrics.work_window_local:
        ws, we = metrics.work_window_local
        ww = WorkWindow(
            start=ws.strftime("%H:%M"), end=we.strftime("%H:%M"),
            start_hour=ws.hour + ws.minute / 60, end_hour=we.hour + we.minute / 60,
        )
        ww_label = t("workwindow.label", start=ww.start, end=ww.end)
    # A short or single-event live session can produce an occupied hour while
    # its composite still rounds to 0.0.  Presence is an ingest fact, not a
    # stress-score threshold, so keep that session visible in the daily view.
    has_activity = any(counts) or metrics.composite > 0
    status = composite_status(metrics.composite, profile.composite_p75, profile.composite_p90)

    # Off-hours nag: shown when off-hours engagement is meaningfully driving the
    # composite (≥ 15 min → ≥ ~5 pts). States the contribution explicitly — and
    # *when* the off-hours minutes happened (local time) — so the user
    # understands why the live score jumped and doesn't misread an
    # earlier-in-the-day accumulation as "you're off-hours right now".
    off_min = metrics.off_hours_minutes
    off_pts = round(OFF_HOURS_LOAD_MAX_POINTS * min(1.0, off_min / OFF_HOURS_LOAD_CEILING_MIN))
    if off_min >= 15:
        when = _off_hours_when_label(metrics.off_hours_ranges_local)
        off_hours_nag = (
            t("nag.off_hours", points=off_pts, minutes=off_min)
            + (t("nag.when", when=when) if when else "")
            + (t("nag.window", start=ww.start, end=ww.end) if ww else "")
        )
    else:
        off_hours_nag = ""

    # Axes are frozen only once the current wall-clock time is PAST the work
    # window end — the day is done and ongoing sessions won't move the 3 axis
    # scores any further. Before the window starts they are NOT frozen: the day
    # is still ahead, early-grace work already counts toward the live score, and
    # graying it out would wrongly read as "final / nothing happened".
    axes_frozen = False
    if ww is not None and now is not None:
        now_local = now.astimezone(local_tz)
        if now_local.date() == metrics.day:
            ct = now_local.time()
            _ws_t, we_t = metrics.work_window_local  # type: ignore[misc]
            axes_frozen = ct >= we_t

    # Subjective grade — only for the today view (when now is given and the
    # view's day matches today). The grader is shown from the start of the final
    # work hour through midnight ((end_hour − 1) ≤ now); the calendar-day
    # rollover ends it naturally (a new day's view starts with no activity). It
    # stays visible whether or not a grade exists, so the user can change their
    # pick — grade_value carries the current choice for highlighting.
    grade_value: int | None = None
    grade_prompt = False
    if now is not None and archive_dir is not None:
        now_local_d = now.astimezone(local_tz)
        if now_local_d.date() == metrics.day:
            from ..pipeline.subjective import read_grade
            grade_value = read_grade(archive_dir, metrics.day)
            if has_activity and ww is not None:
                now_frac = now_local_d.hour + now_local_d.minute / 60
                grade_prompt = now_frac >= (ww.end_hour - 1)

    return DayView(
        day=metrics.day,
        day_label=i18n.day_label(metrics.day),
        has_activity=has_activity,
        composite=metrics.composite,
        composite_label=composite_label(metrics.composite) if has_activity else "—",
        composite_status=status,
        composite_color=composite_color(status),
        advice=composite_advice(status),
        work_window=ww, work_window_label=ww_label,
        hours=counts,
        hour_colors=[codl_count_color(c) for c in counts],
        peak_concurrent=max(counts) if counts else 0,
        score_progression=score_progression(metrics, agg, profile, local_tz),
        axes=[build_axis_tile(meta, metrics, profile) for meta in AXES],
        off_hours_minutes=off_min,
        off_hours_nag=off_hours_nag,
        axes_frozen=axes_frozen,
        grade_prompt=grade_prompt,
        grade_value=grade_value,
    )


# ---------------------------------------------------------------------------
# JSON serialisation (consumed by the desktop widgets)

def dayview_to_dict(dv: DayView) -> dict:
    """Serialise a DayView to a JSON-friendly dict. Colours and 0..1 fractions
    are precomputed so the widget is a dumb renderer that can't drift from the
    report's zone/scale semantics."""
    return {
        "schema": "ai-code-cognitive-stress.dayview.v1",
        "day": dv.day.isoformat(),
        "day_label": dv.day_label,
        "has_activity": dv.has_activity,
        "composite": dv.composite,
        "composite_label": dv.composite_label,
        "composite_status": dv.composite_status,
        "composite_color": dv.composite_color,
        "advice": dv.advice,
        "work_window": (
            {
                "start": dv.work_window.start, "end": dv.work_window.end,
                "start_hour": dv.work_window.start_hour,
                "end_hour": dv.work_window.end_hour,
            } if dv.work_window else None
        ),
        "work_window_label": dv.work_window_label,
        "hours": dv.hours,
        "hour_colors": dv.hour_colors,
        "peak_concurrent": dv.peak_concurrent,
        "score_progression": [
            {"value": p.value, "color": p.color} for p in dv.score_progression
        ],
        "axes": [
            {
                "key": a.key, "name": a.name, "description": a.description,
                "value": a.value, "value_label": a.value_label,
                "unit_text": a.unit_text, "range_max": a.range_max,
                "has_data": a.has_data,
                "status": a.status, "zone_label": a.zone_label, "color": a.color,
                "fraction": a.fraction, "off_scale": a.off_scale,
                "baseline": a.baseline, "baseline_fraction": a.baseline_fraction,
                "baseline_label": a.baseline_label,
                "optimum": a.optimum, "optimum_fraction": a.optimum_fraction,
                "optimum_label": a.optimum_label,
                "segments": [
                    {"start": s.start, "end": s.end, "color": s.color, "status": s.status}
                    for s in a.segments
                ],
                "boundary_ticks": [
                    {"fraction": t.fraction, "label": t.label} for t in a.boundary_ticks
                ],
                "technique": a.technique, "basis": a.basis, "caveat": a.caveat,
            }
            for a in dv.axes
        ],
        "off_hours_minutes": dv.off_hours_minutes,
        "off_hours_nag": dv.off_hours_nag,
        "axes_frozen": dv.axes_frozen,
        "grade_prompt": dv.grade_prompt,
        "grade_value": dv.grade_value,
    }


# ---------------------------------------------------------------------------
# Live data layer — TODAY's daily view, recomputed on demand. This is what
# `aicogstress --emit-json` serves to the desktop widgets (and any other
# external display). Pure data: no UI dependency, unit-tested headlessly.

def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def compute_today_dayview(
    baseline_days: int = 90,
    sources=None,
    projects_dir=None,
    cache_dir=None,
    now: datetime | None = None,
    archive_dir=None,
) -> DayView:
    """Recompute today's full daily view. Reads only today's session files live
    (past days come from the on-disk cache, recovered from the durable archive
    when their source logs have been recycled). `now` is injectable for tests;
    `archive_dir` enables the durable archive (None = disabled — see
    get_day_aggregates)."""
    tz = _local_tz()
    _now = now or datetime.now(tz)
    today = _now.astimezone(tz).date()
    since = today - timedelta(days=baseline_days)
    aggregates, _ = get_day_aggregates(
        since, today,
        projects_dir=projects_dir, cache_dir=cache_dir,
        sources=sources, local_tz=tz, now=now, archive_dir=archive_dir,
    )
    profile = build_profile(
        aggregates, baseline_days=baseline_days, local_tz=tz,
        as_of=_now,
    )
    metrics = profile.days.get(today) or DayMetrics(day=today)
    return build_dayview(
        metrics, aggregates.get(today), profile, tz,
        now=_now, archive_dir=archive_dir,
    )


# ---------------------------------------------------------------------------
# Period views + the multi-timeframe bundle the tabbed widget card renders.

def build_period_view(
    profile: StressProfile,
    days_back: int,
    title: str,
    today: date,
) -> tuple[DayView, tuple[DailyPoint, ...]]:
    """Summarise the last `days_back` days (ending today) as a DayView.

    Composite is the mean over ACTIVE days; the axis tiles are built from the
    period-mean of each axis through the same `build_axis_tile` the day card
    uses (so zones / optimum / baseline match exactly). The header sparkline and
    the returned `daily` series both carry one point per calendar day in the
    window (0 on days with no activity)."""
    window = [today - timedelta(days=i) for i in range(days_back - 1, -1, -1)]

    daily: list[DailyPoint] = []
    progression: list[ScorePoint] = []
    for d in window:
        m = profile.days.get(d)
        comp = m.composite if m else 0.0
        if comp > 0:
            color = composite_color(
                composite_status(comp, profile.composite_p75, profile.composite_p90)
            )
        else:
            color = _ZERO_DAY_COLOR
        daily.append(DailyPoint(day=d, composite=comp, color=color))
        progression.append(ScorePoint(value=comp, color=color))

    active = [
        profile.days[d] for d in window
        if d in profile.days and profile.days[d].composite > 0
    ]
    has_activity = bool(active)
    if has_activity:
        n = len(active)
        codl_avg = sum(m.codl_avg for m in active) / n
        codl_peak = max(m.codl_peak for m in active)
        interruption = sum(m.interruption_rate for m in active) / n
        closures = [m.closure_deficit for m in active if m.closure_deficit is not None]
        closure = sum(closures) / len(closures) if closures else None
        composite = sum(m.composite for m in active) / n
    else:
        codl_avg = interruption = composite = 0.0
        codl_peak = 0
        closure = None

    # Synthetic "average day" — only the fields the three axis tiles read.
    synthetic = DayMetrics(
        day=today, codl_avg=codl_avg, codl_peak=codl_peak,
        interruption_rate=interruption, closure_deficit=closure,
        composite=composite,
    )
    status = composite_status(composite, profile.composite_p75, profile.composite_p90)

    view = DayView(
        day=today,
        day_label=title,
        has_activity=has_activity,
        composite=composite,
        composite_label=composite_label(composite) if has_activity else "—",
        composite_status=status,
        composite_color=composite_color(status),
        advice=composite_advice(status),
        work_window=None,
        work_window_label=(
            tn("period.active_days", len(active)) if has_activity else ""
        ),
        hours=[], hour_colors=[], peak_concurrent=0,
        score_progression=progression if has_activity else [],
        axes=[build_axis_tile(meta, synthetic, profile) for meta in AXES],
        off_hours_minutes=0,
        off_hours_nag="",
    )
    return view, tuple(daily)


def _last_12_months(today: date) -> list[tuple[int, int]]:
    """The 12 (year, month) pairs ending in today's month, oldest first."""
    months: list[tuple[int, int]] = []
    y, mo = today.year, today.month
    for _ in range(12):
        months.append((y, mo))
        mo -= 1
        if mo == 0:
            mo, y = 12, y - 1
    months.reverse()
    return months


def build_year_view(
    data_profile: StressProfile,
    marker_profile: StressProfile,
    title: str,
    today: date,
) -> tuple[DayView, tuple[MonthlyPoint, ...]]:
    """Summarise the last 12 calendar months. The per-month bars carry each
    month's mean composite (over its active days); the headline composite and
    axis tiles are the mean across every active day in those months.

    `data_profile` supplies the per-day metrics across the year; `marker_profile`
    (the recent baseline window) supplies the zones / 'typical day' / optimum, so
    those markers match the Today/Week/Month tabs exactly."""
    months = _last_12_months(today)
    month_set = set(months)
    p75, p90 = marker_profile.composite_p75, marker_profile.composite_p90

    by_month: dict[tuple[int, int], list[float]] = {m: [] for m in months}
    for d, m in data_profile.days.items():
        key = (d.year, d.month)
        if key in month_set and m.composite > 0:
            by_month[key].append(m.composite)

    monthly: list[MonthlyPoint] = []
    progression: list[ScorePoint] = []
    for yy, mm in months:
        vals = by_month[(yy, mm)]
        if vals:
            mean = sum(vals) / len(vals)
            color = composite_color(composite_status(mean, p75, p90))
        else:
            mean, color = 0.0, _ZERO_DAY_COLOR
        monthly.append(MonthlyPoint(year=yy, month=mm, composite=mean, color=color))
        progression.append(ScorePoint(value=mean, color=color))

    active = [
        m for d, m in data_profile.days.items()
        if (d.year, d.month) in month_set and m.composite > 0
    ]
    has_activity = bool(active)
    if has_activity:
        n = len(active)
        codl_avg = sum(m.codl_avg for m in active) / n
        codl_peak = max(m.codl_peak for m in active)
        interruption = sum(m.interruption_rate for m in active) / n
        closures = [m.closure_deficit for m in active if m.closure_deficit is not None]
        closure = sum(closures) / len(closures) if closures else None
        composite = sum(m.composite for m in active) / n
    else:
        codl_avg = interruption = composite = 0.0
        codl_peak = 0
        closure = None

    synthetic = DayMetrics(
        day=today, codl_avg=codl_avg, codl_peak=codl_peak,
        interruption_rate=interruption, closure_deficit=closure, composite=composite,
    )
    status = composite_status(composite, p75, p90)
    view = DayView(
        day=today,
        day_label=title,
        has_activity=has_activity,
        composite=composite,
        composite_label=composite_label(composite) if has_activity else "—",
        composite_status=status,
        composite_color=composite_color(status),
        advice=composite_advice(status),
        work_window=None,
        work_window_label=(
            tn("period.active_days", len(active)) if has_activity else ""
        ),
        hours=[], hour_colors=[], peak_concurrent=0,
        score_progression=progression if has_activity else [],
        axes=[build_axis_tile(meta, synthetic, marker_profile) for meta in AXES],
        off_hours_minutes=0,
        off_hours_nag="",
    )
    return view, tuple(monthly)


def compute_timeframe_views(
    baseline_days: int = 90,
    sources=None,
    projects_dir=None,
    cache_dir=None,
    now: datetime | None = None,
    archive_dir=None,
) -> list[TimeframeView]:
    """Today + Week (7d) + Month (30d) + Year (12 months), all from a single
    aggregation pass so the tabbed widget card needs only one CLI invocation.

    Every tab's zones / 'typical day' baseline / optimum come from one reference
    profile over the recent `baseline_days` window (so they're identical across
    tabs and unchanged from the day-only widget); the year view's per-month bars
    use a second profile spanning the whole year. `now` is injectable for tests.
    """
    tz = _local_tz()
    _now = now or datetime.now(tz)
    today = _now.astimezone(tz).date()
    # A bit over a year so the oldest displayed month is fully covered.
    since = today - timedelta(days=400)
    aggregates, _ = get_day_aggregates(
        since, today,
        projects_dir=projects_dir, cache_dir=cache_dir,
        sources=sources, local_tz=tz, now=_now, archive_dir=archive_dir,
    )
    # Reference profile: the recent baseline window only. Drives the zones,
    # "typical day" baseline and optimum for ALL tabs — so adding the year view
    # never shifts the Today/Week/Month numbers.
    ref_since = today - timedelta(days=max(baseline_days, 30))
    ref_aggregates = {d: a for d, a in aggregates.items() if d >= ref_since}
    ref_profile = build_profile(
        ref_aggregates, baseline_days=baseline_days, local_tz=tz,
        as_of=_now,
    )
    # Full-window profile: only its per-day metrics are used, for the year bars.
    year_profile = build_profile(
        aggregates, baseline_days=baseline_days, local_tz=tz,
        as_of=_now,
    )

    today_metrics = ref_profile.days.get(today) or DayMetrics(day=today)
    today_view = build_dayview(today_metrics, aggregates.get(today), ref_profile, tz, now=_now, archive_dir=archive_dir)
    week_view, week_daily = build_period_view(ref_profile, 7, t("tab.week_title"), today)
    month_view, month_daily = build_period_view(ref_profile, 30, t("tab.month_title"), today)
    year_view, year_monthly = build_year_view(
        year_profile, ref_profile, t("tab.year_title"), today,
    )
    return [
        TimeframeView(key="today", tab_label=t("tab.today"), view=today_view),
        TimeframeView(key="week", tab_label=t("tab.week"), view=week_view, daily=week_daily),
        TimeframeView(key="month", tab_label=t("tab.month"), view=month_view, daily=month_daily),
        TimeframeView(key="year", tab_label=t("tab.year"), view=year_view, monthly=year_monthly),
    ]
