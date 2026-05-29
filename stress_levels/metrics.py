"""Reduce per-day aggregates into the StressProfile consumed by the renderer.

Three axes (per day, computed during work hours only):
    CODL              avg / peak count of concurrently-active streams during
                      the day's work window. Sampled at 1-minute resolution
                      via sweep over per-stream (first_ts, last_ts) intervals.
                      Grounded in Cowan (2001) — WM capacity for concurrent
                      decision tracking — and Cummings & Mitchell (2008) on
                      supervisory-control fan-out.
    Interruption Idx  weighted attention-pulling events per work hour.
                      Weights from Mark, Gudith & Klocke (2008) and
                      Mark, Gonzalez & Harris (2005).
    Closure Deficit   fraction of work-hour samples with CODL > 1 — proxy
                      for "time spent with multiple open loops". JD-R framing
                      (Demerouti et al. 2001).

Composite stress = equal-weighted blend of the three axes mapped to 0..100.
Equal weights are the null hypothesis for v1; we don't have evidence to favor
one axis over another. The choice and its caveat are surfaced explicitly in
the rendered report's methodology footer.

Personal optimum (Yerkes-Dodson 1908 / Csíkszentmihályi 1990):
    bucket historical days by CODL, score each bucket by closure × inverse
    off-hours-engagement, and report the CODL midpoint of the best bucket
    as the user's flow channel target. Returns None when there's
    insufficient data ("calibrating").

Work windows are auto-detected per local weekday from the p10/p90 of
user-message hours-of-day across the baseline window. Days with too few
samples fall back to a default 09:00–18:00. Detection runs in the user's
local timezone — that is the only place TZ enters the metrics pipeline.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterable

from .aggregate import DayAggregate, StreamDayActivity
from .config import load_config

# ---------------------------------------------------------------------------
# Citation-anchored constants. Bump tunables here, not at call sites.

# Cowan (2001) — WM cap ≈ 4. We use 5 as the CODL normalisation ceiling so
# values at the working-memory limit land at 0.8 (warning band) rather than
# saturating at 1.0.
CODL_NORMALISATION_CEILING: float = 5.0

# Mark, Gudith & Klocke (2008) reported 4.36 interruptions/hour on average in
# their office study; >10/hr is well into "fragmented" territory. We use 10/hr
# as the normalisation ceiling so a moderate day lands well below 1.0.
INTERRUPTION_NORMALISATION_CEILING: float = 10.0

# Interruption-event weights — source-by-source.
#
# tool_use events are deliberately NOT counted as interruptions. When Claude
# invokes a tool inside an active session the supervisor is in a "Waiting"
# state — attention can detach until the result comes back. Counting every
# tool call as an interruption inflates the rate by 1-2 orders of magnitude
# on heavy Claude Code days and violates Mark's definition of interruption
# as "an unscheduled task event that requires immediate attention".
#
# Two events that genuinely do pull attention remain:
#   - tool_error: the tool failed and the user may need to intervene
#   - cross_stream_start: a new session lit up while another was active —
#     forces a context switch between two open conversations (the
#     LLM-supervision-specific signal, no direct prior in Mark/Leroy)
W_TOOL_ERROR: float = 1.5
W_CROSS_STREAM: float = 3.0

# v1 composite weights — equal (null hypothesis). The methodology footer of
# the rendered report names this choice and links to the relevant citations.
COMPOSITE_WEIGHTS: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)

# The working day is FIXED (not auto-detected) and configurable: its default
# lives in config.json (work_window), not hardcoded here. It is used for every
# calculation (interruption rate per work hour, closure deficit, in-window
# CODL) and every visualisation.
WORK_WINDOW_MIN_SAMPLES: int = 5  # kept for detect_work_windows signature compat

# Personal optimum derivation — calibration period.
OPTIMUM_MIN_DAYS_OF_DATA: int = 14
OPTIMUM_BUCKET_WIDTH: float = 0.5  # CODL units


def is_weekend(day: date) -> bool:
    """Saturdays and Sundays are not treated as working days.

    All weekend activity is surfaced as off-hours-minutes only — it is
    excluded from weekday-only averages, percentiles, the personal
    optimum, and the peak-day KPI. This is the v1 hard-coded behaviour;
    if shift workers ever need different handling, plumb it through here.
    """
    return day.weekday() >= 5


# ---------------------------------------------------------------------------
# Data shapes

@dataclass(frozen=True, slots=True)
class WorkWindow:
    """One weekday's auto-detected work-hour band (in local time)."""
    weekday: int          # Mon=0 … Sun=6
    start: time
    end: time
    is_default: bool = False  # True when fallback default was used


@dataclass(frozen=True, slots=True)
class DayMetrics:
    """The three axes plus composite, computed for a single UTC day."""
    day: date
    codl_avg: float = 0.0
    codl_peak: int = 0
    interruption_rate: float = 0.0   # per work hour
    closure_deficit: float = 0.0     # 0..1
    off_hours_minutes: int = 0
    composite: float = 0.0           # 0..100
    work_window_local: tuple[time, time] | None = None


@dataclass(frozen=True, slots=True)
class StressProfile:
    """Per-day metrics + profile-level derivatives."""
    days: dict[date, DayMetrics] = field(default_factory=dict)
    work_windows: dict[int, WorkWindow] = field(default_factory=dict)
    local_tz_name: str = "UTC"
    baseline_window_days: int = 30
    personal_optimum: float | None = None
    # Personal percentiles over the baseline window (used to color days).
    composite_p50: float | None = None
    composite_p75: float | None = None
    composite_p90: float | None = None


# ---------------------------------------------------------------------------
# Public entry point

def build_profile(
    aggregates: dict[date, DayAggregate],
    baseline_days: int = 30,
    local_tz: tzinfo | None = None,
) -> StressProfile:
    """Reduce a window of DayAggregates into a StressProfile.

    `local_tz` is the timezone in which to interpret "work hours" and
    "weekday". Defaults to the system local timezone.
    """
    local_tz = local_tz or datetime.now().astimezone().tzinfo or timezone.utc
    work_windows = detect_work_windows(aggregates, local_tz=local_tz)

    days: dict[date, DayMetrics] = {}
    for day, agg in sorted(aggregates.items()):
        weekday = day.weekday()
        window = work_windows.get(weekday) or _default_window(weekday)
        days[day] = per_day_metrics(agg, window, local_tz)

    # Percentiles are computed across active *workdays* only. Weekend
    # activity is excluded by design (see is_weekend()).
    composites = [
        m.composite for d, m in days.items()
        if m.composite > 0 and not is_weekend(d)
    ]
    p50 = _percentile(composites, 0.5) if composites else None
    p75 = _percentile(composites, 0.75) if composites else None
    p90 = _percentile(composites, 0.9) if composites else None

    optimum = derive_personal_optimum(days)

    return StressProfile(
        days=days,
        work_windows=work_windows,
        local_tz_name=str(local_tz),
        baseline_window_days=baseline_days,
        personal_optimum=optimum,
        composite_p50=p50,
        composite_p75=p75,
        composite_p90=p90,
    )


# ---------------------------------------------------------------------------
# Work-window detection

def detect_work_windows(
    aggregates: dict[date, DayAggregate],
    local_tz: tzinfo,
    min_samples: int = WORK_WINDOW_MIN_SAMPLES,
) -> dict[int, WorkWindow]:
    """Return the configured fixed work window for every weekday.

    The working day is fixed (not auto-detected) and its value comes from
    config.json (work_window), so every calculation and visualisation uses one
    stable band. `aggregates`, `local_tz`, and `min_samples` are accepted for
    signature compatibility but no longer influence the result.
    """
    ww = load_config().work_window
    return {
        wd: WorkWindow(weekday=wd, start=ww.start, end=ww.end, is_default=False)
        for wd in range(7)
    }


# ---------------------------------------------------------------------------
# Per-day reduction

def per_day_metrics(
    agg: DayAggregate,
    work_window: WorkWindow,
    local_tz: tzinfo,
) -> DayMetrics:
    """Compute the three axes + composite for one day.

    Weekend days (Sat/Sun) are surfaced as off-hours only: composite is
    zero, axis values are zero, and all stream-active minutes count as
    `off_hours_minutes`. They never contribute to weekday percentiles,
    averages, peak-day, or the personal optimum.
    """
    if is_weekend(agg.day):
        if not agg.streams:
            return DayMetrics(day=agg.day, work_window_local=None)
        # Weekend: every active minute counts as off-hours. Use the union
        # (overlapping sessions counted once) instead of summing per-stream
        # durations, which would double-count parallel sessions.
        return DayMetrics(
            day=agg.day,
            off_hours_minutes=_union_active_minutes(agg.streams),
            work_window_local=None,
        )

    if not agg.streams:
        return DayMetrics(
            day=agg.day,
            work_window_local=(work_window.start, work_window.end),
        )

    window_start_utc, window_end_utc = _window_utc_bounds(
        agg.day, work_window, local_tz,
    )
    work_seconds = max(1, int((window_end_utc - window_start_utc).total_seconds()))
    work_hours = work_seconds / 3600.0

    samples = _codl_samples(agg.streams, window_start_utc, window_end_utc)
    if samples:
        codl_avg = sum(samples) / len(samples)
        codl_peak = max(samples)
        closure_deficit = sum(1 for c in samples if c > 1) / len(samples)
    else:
        codl_avg = 0.0
        codl_peak = 0
        closure_deficit = 0.0

    cross_starts = _count_cross_stream_starts(
        agg.streams, window_start_utc, window_end_utc,
    )
    # Apportion per-stream tool errors by the fraction of each stream's
    # lifetime that overlaps the work window. A stream that lived entirely
    # outside the window contributes none of its errors to the work-hour
    # rate; a stream half-in contributes half. The old formula counted
    # every error all day, inflating the work-hour rate when the user had
    # off-hours activity with errors.
    in_window_errors = _apportion_to_window(
        agg.streams, "tool_error_count",
        window_start_utc, window_end_utc,
    )
    interruption_count = (
        in_window_errors * W_TOOL_ERROR
        + cross_starts * W_CROSS_STREAM
    )
    interruption_rate = interruption_count / work_hours

    # Off-hours minutes = wall-clock minutes of stream activity outside the
    # work window. Computed as union(all stream activity) minus union(in
    # work window) so simultaneous streams count once, not twice.
    total_active = _union_active_minutes(agg.streams)
    in_window_active = _union_active_minutes(
        agg.streams,
        start=window_start_utc, end=window_end_utc,
    )
    off_hours_minutes = max(0, total_active - in_window_active)

    composite = _composite_score(codl_avg, interruption_rate, closure_deficit)

    return DayMetrics(
        day=agg.day,
        codl_avg=round(codl_avg, 3),
        codl_peak=codl_peak,
        interruption_rate=round(interruption_rate, 3),
        closure_deficit=round(closure_deficit, 3),
        off_hours_minutes=off_hours_minutes,
        composite=round(composite, 1),
        work_window_local=(work_window.start, work_window.end),
    )


def _composite_score(
    codl_avg: float,
    interruption_rate: float,
    closure_deficit: float,
) -> float:
    """Weighted blend of the three axes, mapped to 0..100. Each axis is
    clamped to [0, 1] before weighting."""
    codl_norm = min(1.0, codl_avg / CODL_NORMALISATION_CEILING)
    int_norm = min(1.0, interruption_rate / INTERRUPTION_NORMALISATION_CEILING)
    closure_norm = max(0.0, min(1.0, closure_deficit))
    w_codl, w_int, w_clo = COMPOSITE_WEIGHTS
    blend = w_codl * codl_norm + w_int * int_norm + w_clo * closure_norm
    return 100.0 * blend


def _window_utc_bounds(
    day: date, window: WorkWindow, local_tz: tzinfo,
) -> tuple[datetime, datetime]:
    """Convert a local-tz work-hour band on `day` into UTC datetimes."""
    local_start = datetime.combine(day, window.start, tzinfo=local_tz)
    local_end = datetime.combine(day, window.end, tzinfo=local_tz)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _codl_samples(
    streams: tuple[StreamDayActivity, ...],
    window_start_utc: datetime,
    window_end_utc: datetime,
    sample_interval_seconds: int = 60,
) -> list[int]:
    """Sample concurrent stream count at fixed intervals within the window.

    Each stream is treated as continuously alive across
    [first_ts, last_ts]. This is a deliberate over-approximation — a long
    session with a multi-hour gap is treated as one continuously-open
    attentional locus. Documented in the rendered report's methodology.
    """
    if window_end_utc <= window_start_utc:
        return []
    samples: list[int] = []
    t = window_start_utc
    step = timedelta(seconds=sample_interval_seconds)
    while t <= window_end_utc:
        count = 0
        for s in streams:
            if s.first_ts <= t <= s.last_ts:
                count += 1
        samples.append(count)
        t += step
    return samples


def _count_cross_stream_starts(
    streams: tuple[StreamDayActivity, ...],
    window_start_utc: datetime,
    window_end_utc: datetime,
) -> int:
    """Count stream openings (first_ts) that occurred during the work window
    AND while at least one other stream was already active."""
    in_window = [s for s in streams
                 if window_start_utc <= s.first_ts <= window_end_utc]
    cross = 0
    for s in in_window:
        for other in streams:
            if other.stream_id == s.stream_id:
                continue
            if other.first_ts < s.first_ts <= other.last_ts:
                cross += 1
                break
    return cross


def _union_active_minutes(
    streams: tuple[StreamDayActivity, ...],
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Wall-clock minutes covered by *any* stream's [first_ts, last_ts]
    interval, with overlaps counted once. Optionally clip the union to
    [start, end].

    Sweep-line merge of sorted intervals — same shape as `_peak_concurrent`
    but accumulating duration instead of peak count. Returns 0 on an empty
    stream tuple or an empty clip range.
    """
    intervals: list[tuple[datetime, datetime]] = []
    for s in streams:
        first = s.first_ts if start is None else max(s.first_ts, start)
        last = s.last_ts if end is None else min(s.last_ts, end)
        if first < last:
            intervals.append((first, last))
    if not intervals:
        return 0
    intervals.sort()
    total_seconds = 0
    cur_start, cur_end = intervals[0]
    for s_start, s_end in intervals[1:]:
        if s_start <= cur_end:
            if s_end > cur_end:
                cur_end = s_end
        else:
            total_seconds += int((cur_end - cur_start).total_seconds())
            cur_start, cur_end = s_start, s_end
    total_seconds += int((cur_end - cur_start).total_seconds())
    return total_seconds // 60


def _apportion_to_window(
    streams: tuple[StreamDayActivity, ...],
    count_attr: str,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Apportion a per-stream-aggregated event count (e.g. tool_error_count)
    by the fraction of each stream's lifetime that falls inside the window.

    Errors are assumed uniformly distributed across the stream's
    [first_ts, last_ts] interval — defensible default since the aggregate
    doesn't track per-event timestamps for tool events. A stream entirely
    outside the window contributes zero of its events to the in-window
    total; a stream half-in contributes half.

    Returns the rounded integer count for the window.
    """
    total = 0.0
    for s in streams:
        n = getattr(s, count_attr)
        if n <= 0:
            continue
        dur = max(1, int((s.last_ts - s.first_ts).total_seconds()))
        overlap_start = max(s.first_ts, window_start)
        overlap_end = min(s.last_ts, window_end)
        if overlap_end <= overlap_start:
            continue
        overlap_seconds = int((overlap_end - overlap_start).total_seconds())
        total += n * (overlap_seconds / dur)
    return int(round(total))


# ---------------------------------------------------------------------------
# Personal optimum

def derive_personal_optimum(
    days: dict[date, DayMetrics],
    min_days: int = OPTIMUM_MIN_DAYS_OF_DATA,
    bucket_width: float = OPTIMUM_BUCKET_WIDTH,
) -> float | None:
    """Find the CODL band that historically yielded the lowest combined
    (closure_deficit + off-hours pressure). Anchored in Yerkes-Dodson (1908)
    and Csíkszentmihályi (1990): performance follows an inverted-U with
    cognitive load. Returns None when there is insufficient data — the
    rendered report treats that as "calibrating"."""
    # Weekdays only — the optimum is "where do I perform best at work?"
    active_days = [
        m for m in days.values()
        if m.codl_avg > 0 and not is_weekend(m.day)
    ]
    if len(active_days) < min_days:
        return None

    buckets: dict[int, list[DayMetrics]] = defaultdict(list)
    for m in active_days:
        bucket_idx = int(m.codl_avg / bucket_width)
        buckets[bucket_idx].append(m)

    best_score = -1.0
    best_midpoint: float | None = None
    for idx, metrics in buckets.items():
        # Need at least 2 days in a bucket to trust the average.
        if len(metrics) < 2:
            continue
        avg_closure = sum(m.closure_deficit for m in metrics) / len(metrics)
        avg_off = sum(m.off_hours_minutes for m in metrics) / len(metrics)
        # Higher is better — low closure-deficit AND low off-hours.
        score = (1.0 - avg_closure) / (1.0 + avg_off / 60.0)
        if score > best_score:
            best_score = score
            best_midpoint = (idx + 0.5) * bucket_width

    return best_midpoint


# ---------------------------------------------------------------------------
# Small helpers

def _percentile(sorted_values: Iterable[float], q: float) -> float:
    """Linear-interpolation percentile on a sorted (ascending) iterable.
    For values that aren't sorted, the caller should sort first."""
    xs = list(sorted_values)
    if not xs:
        return 0.0
    if not _is_sorted(xs):
        xs.sort()
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def _is_sorted(xs: list[float]) -> bool:
    return all(xs[i] <= xs[i + 1] for i in range(len(xs) - 1))


def _hour_to_time(hour_float: float) -> time:
    """Clamp a fractional hour-of-day to a time(). Out-of-range values are
    clipped to 00:00:00 / 23:59:59."""
    if hour_float < 0:
        return time(0, 0, 0)
    if hour_float >= 24:
        return time(23, 59, 59)
    h = int(hour_float)
    m = int((hour_float - h) * 60)
    return time(h, m, 0)
