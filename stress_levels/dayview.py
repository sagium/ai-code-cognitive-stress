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

from .aggregate import DayAggregate, get_day_aggregates
from . import i18n
from .i18n import t
from .metrics import (
    CODL_NORMALISATION_CEILING,
    OFF_HOURS_LOAD_CEILING_MIN,
    OFF_HOURS_LOAD_MAX_POINTS,
    DayMetrics,
    StressProfile,
    WorkWindow as _MetricsWorkWindow,
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


def hour_counts(day: date, agg: DayAggregate | None, local_tz: tzinfo) -> list[int]:
    """Per-LOCAL-hour count of concurrent streams (24 buckets), sampled at the
    half-hour. Mirrors render.py's day-chart bucketing exactly."""
    if agg is None or not agg.streams:
        return [0] * 24
    counts: list[int] = []
    for h in range(24):
        local_t = datetime.combine(day, time(h, 30), tzinfo=local_tz)
        utc_t = local_t.astimezone(timezone.utc)
        counts.append(sum(1 for s in agg.streams if s.first_ts <= utc_t <= s.last_ts))
    return counts


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
    ws, we = metrics.work_window_local
    weekday = metrics.day.weekday()
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
    has_activity = metrics.composite > 0
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

    return DayView(
        day=metrics.day,
        day_label=i18n.day_label(metrics.day),
        has_activity=has_activity,
        composite=metrics.composite,
        composite_label=f"{metrics.composite:.0f}" if has_activity else "—",
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
    }


# ---------------------------------------------------------------------------
# Live data layer — TODAY's daily view, recomputed on demand. This is what
# `aicogstress --emit-json` serves to the desktop widgets (and any other
# external display). Pure data: no UI dependency, unit-tested headlessly.

def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def compute_today_dayview(
    baseline_days: int = 30,
    sources=None,
    projects_dir=None,
    cache_dir=None,
    now: datetime | None = None,
) -> DayView:
    """Recompute today's full daily view. Reads only today's session files live
    (past days come from the on-disk cache). `now` is injectable for tests."""
    tz = _local_tz()
    today = (now or datetime.now(tz)).astimezone(tz).date()
    since = today - timedelta(days=baseline_days)
    aggregates, _ = get_day_aggregates(
        since, today,
        projects_dir=projects_dir, cache_dir=cache_dir,
        sources=sources, local_tz=tz, now=now,
    )
    profile = build_profile(
        aggregates, baseline_days=baseline_days, local_tz=tz,
    )
    metrics = profile.days.get(today) or DayMetrics(day=today)
    return build_dayview(metrics, aggregates.get(today), profile, tz)
