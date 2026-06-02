"""Canonical daily-view model — the single source of truth for the per-day
drill-down shown in BOTH the HTML report and the KDE Plasma widget.

UI-agnostic: plain text only (Unicode, no HTML entities, no SVG, no QML).
`render.py` renders this to HTML/SVG; `dayview_to_dict` serialises it for the
plasmoid (`aicogstress --emit-json`). Sharing this module is what keeps the report and
the widget from drifting — the same role `scales.py` plays for zones/colours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone, tzinfo

from .aggregate import DayAggregate
from .metrics import (
    CODL_NORMALISATION_CEILING,
    DayMetrics,
    StressProfile,
    WorkWindow as _MetricsWorkWindow,
    per_day_metrics,
)
from .scales import (
    CLOSURE_RANGE_MAX,
    CLOSURE_ZONES,
    CODL_ZONES,
    INTERRUPTION_RANGE_MAX,
    INTERRUPTION_ZONES,
    composite_advice,
    composite_color,
    composite_status,
    zone_color,
    zone_for,
)

Zone = tuple[float, str, str]


# ---------------------------------------------------------------------------
# Per-axis static metadata (plain text — the HTML layer escapes it, the widget
# shows it verbatim). Descriptions/technique/basis/caveat are the SAME copy the
# report shows; render.py reads them from here so the two cannot diverge.

@dataclass(frozen=True, slots=True)
class AxisMeta:
    key: str
    name: str
    description: str
    range_max: float
    zones: list[Zone]
    has_optimum: bool
    technique: str
    basis: str
    caveat: str


AXES: tuple[AxisMeta, ...] = (
    AxisMeta(
        key="codl",
        name="CODL",
        description=(
            "How many agent sessions you're supervising at once. "
            "Working memory caps at about 4 concurrent threads (Cowan 2001)."
        ),
        range_max=CODL_NORMALISATION_CEILING,
        zones=CODL_ZONES,
        has_optimum=True,
        technique=(
            "Sweep over per-stream (first_ts, last_ts) intervals sampled at "
            "1-min resolution within the configured work window."
        ),
        basis=(
            "Cowan (2001); Cummings & Mitchell (2008) on supervisory-control "
            "fan-out."
        ),
        caveat=(
            "Streams treated as continuously alive between first/last event; "
            "long mid-session gaps are over-counted as engaged time."
        ),
    ),
    AxisMeta(
        key="interruption",
        name="Interruption Index",
        description=(
            "Weighted attention-pulling events per work hour: tool errors "
            "(need user intervention) and cross-session switches. Mark (2008) "
            "showed interrupted work is faster but more stressful."
        ),
        range_max=INTERRUPTION_RANGE_MAX,
        zones=INTERRUPTION_ZONES,
        has_optimum=False,
        technique=(
            "tool_error × 1.5 + cross-stream-start × 3.0, divided by work-hour "
            "duration. Tool calls inside a session are intentionally NOT "
            "counted — when the agent is using a tool the supervisor is in a "
            "Waiting state, not being interrupted."
        ),
        basis=(
            "Mark, Gudith & Klocke (2008); Mark, Gonzalez & Harris (2005); "
            "Leroy (2009) on attention residue."
        ),
        caveat=(
            "Tool-use timestamps not preserved at the aggregate layer; events "
            "are apportioned by stream-active-time overlap with the work window."
        ),
    ),
    AxisMeta(
        key="closure",
        name="Closure Deficit",
        description=(
            "Of the loops git can see you open, the share you never closed. "
            "0 = every such loop got your push/commit, 1 = none landed. A day "
            "with no git activity of your own is not scored at all (shown as —), "
            "not counted as zero. Lower is better."
        ),
        range_max=CLOSURE_RANGE_MAX,
        zones=CLOSURE_ZONES,
        has_optimum=False,
        technique=(
            "1 − closed / correlatable, where a loop = a stream started in the "
            "work window and it is closed by YOUR OWN git push/commit/merge in "
            "its repo within the loop's active span + grace. Loops we can't "
            "correlate to your git activity (no tracked repo, or a repo you "
            "didn't touch that day) are dropped; a day with none is not scored. "
            "A session under 5 min that caught no commit is treated as a trivial "
            "check and dropped too, so it can't raise the deficit. "
            "Per-session correlation — independent of the CODL shape."
        ),
        basis=(
            "Demerouti et al. (2001) Job Demands-Resources; Masicampo & "
            "Baumeister (2011) open goals; Leroy (2009) attention residue; "
            "Sonnentag & Fritz (2007) closure as recovery."
        ),
        caveat=(
            "A closure is matched to a session by repo + author + time overlap, "
            "not a guaranteed link. Closures count only when you authored them "
            "(or pushed them), so a shared repo's teammate/bot commits are "
            "ignored. Days with no git activity of your own are omitted as data "
            "(not scored 0), and the composite renormalises over the remaining "
            "axes. The axis has meaning only on git repositories."
        ),
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
        return f"avg · peak {m.codl_peak} streams"
    if key == "interruption":
        return "weighted events per work hour"
    if m.closure_deficit is None:
        return "disabled — no active git repo this day"
    return f"{m.closure_deficit * 100:.0f}% of opened loops left unclosed"


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
    has_data: bool      # False → axis had no data this day (e.g. no git activity)
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
    peak_concurrent: int
    # Cumulative composite at each hour-end of the work window (how the score
    # built up over the day), each point carrying its zone colour. The last
    # point equals `composite`.
    score_progression: list["ScorePoint"]
    axes: list[AxisTile]


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

    # No-data axis (currently only Closure, when the day had no git-correlatable
    # activity): render the empty scale with a neutral "not scored" state rather
    # than a 0 that reads as a perfect score. The value is omitted as data.
    if raw is None:
        return AxisTile(
            key=meta.key, name=meta.name, description=meta.description,
            value=0.0, value_label="—", unit_text=_axis_unit(meta.key, m),
            range_max=rmax, has_data=False, status="", zone_label="not scored",
            color=zone_color(""), fraction=0.0, off_scale=False,
            baseline=None, baseline_fraction=None, baseline_label="typical day",
            optimum=None, optimum_fraction=None,
            optimum_label="optimum" if meta.has_optimum else "",
            segments=segments, boundary_ticks=ticks,
            technique=meta.technique, basis=meta.basis, caveat=meta.caveat,
        )

    value = raw
    status, zone_label = zone_for(value, meta.zones)

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
        key=meta.key, name=meta.name, description=meta.description,
        value=value, value_label=f"{value:.2f}", unit_text=_axis_unit(meta.key, m),
        range_max=rmax, has_data=True, status=status, zone_label=zone_label,
        color=zone_color(status), fraction=_clamp01(value / rmax) if rmax else 0.0,
        off_scale=value > rmax,
        baseline=baseline, baseline_fraction=baseline_frac, baseline_label="typical day",
        optimum=optimum, optimum_fraction=optimum_frac,
        optimum_label="optimum" if meta.has_optimum else "",
        segments=segments, boundary_ticks=ticks,
        technique=meta.technique, basis=meta.basis, caveat=meta.caveat,
    )


@dataclass(frozen=True, slots=True)
class ScorePoint:
    value: float        # cumulative composite 0–100 at this hour-end
    color: str          # zone colour for that level (drives the gradient)


def score_progression(
    metrics: DayMetrics,
    agg: DayAggregate | None,
    profile: StressProfile,
    local_tz: tzinfo,
) -> list[ScorePoint]:
    """Cumulative composite (0–100) at each hour-end of the work window — the
    score 'so far' as the day fills, computed with the same engine as the
    headline composite. Each point carries its zone colour so the sparkline can
    render as a severity gradient. The final point equals the day's composite.
    Empty when there is no work window or no activity."""
    if agg is None or not agg.streams or metrics.work_window_local is None:
        return []
    ws, we = metrics.work_window_local
    weekday = metrics.day.weekday()
    points: list[ScorePoint] = []
    for h in range(ws.hour + 1, we.hour + 1):
        window = _MetricsWorkWindow(weekday=weekday, start=ws, end=time(h, 0))
        value = per_day_metrics(agg, window, local_tz).composite
        status = composite_status(value, profile.composite_p75, profile.composite_p90)
        points.append(ScorePoint(value=value, color=composite_color(status)))
    return points


def build_dayview(
    metrics: DayMetrics,
    agg: DayAggregate | None,
    profile: StressProfile,
    local_tz: tzinfo,
) -> DayView:
    counts = hour_counts(metrics.day, agg, local_tz)
    ww = None
    ww_label = "work window: (unknown)"
    if metrics.work_window_local:
        ws, we = metrics.work_window_local
        ww = WorkWindow(
            start=ws.strftime("%H:%M"), end=we.strftime("%H:%M"),
            start_hour=ws.hour + ws.minute / 60, end_hour=we.hour + we.minute / 60,
        )
        ww_label = f"work window: {ws.strftime('%H:%M')} – {we.strftime('%H:%M')}"
    has_activity = metrics.composite > 0
    status = composite_status(metrics.composite, profile.composite_p75, profile.composite_p90)
    return DayView(
        day=metrics.day,
        day_label=metrics.day.strftime("%A %d %B %Y"),
        has_activity=has_activity,
        composite=metrics.composite,
        composite_label=f"{metrics.composite:.0f}" if has_activity else "—",
        composite_status=status,
        composite_color=composite_color(status),
        advice=composite_advice(status),
        work_window=ww, work_window_label=ww_label,
        hours=counts, peak_concurrent=max(counts) if counts else 0,
        score_progression=score_progression(metrics, agg, profile, local_tz),
        axes=[build_axis_tile(meta, metrics, profile) for meta in AXES],
    )


# ---------------------------------------------------------------------------
# JSON serialisation (consumed by the KDE Plasma widget)

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
    }
