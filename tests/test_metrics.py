"""Tests for the metrics layer: work windows, three axes, composite, optimum."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from stress_levels.aggregate import DayAggregate, StreamDayActivity
from stress_levels.metrics import (
    CODL_NORMALISATION_CEILING,
    COMPOSITE_WEIGHTS,
    INTERRUPTION_NORMALISATION_CEILING,
    OPTIMUM_MIN_DAYS_OF_DATA,
    DayMetrics,
    StressProfile,
    WorkWindow,
    _apportion_to_window,
    _codl_samples,
    _composite_score,
    _count_cross_stream_starts,
    _hour_to_time,
    _percentile,
    _union_active_minutes,
    build_profile,
    derive_personal_optimum,
    detect_work_windows,
    per_day_metrics,
)


UTC = timezone.utc


def _utc(year, month, day, hour=12, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _stream(stream_id, first_ts, last_ts, **counts):
    return StreamDayActivity(
        stream_id=stream_id,
        project="proj",
        first_ts=first_ts,
        last_ts=last_ts,
        user_msg_count=counts.get("user_msg_count", 0),
        assistant_msg_count=counts.get("assistant_msg_count", 0),
        tool_use_count=counts.get("tool_use_count", 0),
        tool_result_count=counts.get("tool_result_count", 0),
        tool_error_count=counts.get("tool_error_count", 0),
        branches=counts.get("branches", ()),
        user_msg_timestamps=counts.get("user_msg_timestamps", ()),
    )


def _agg(day, streams):
    return DayAggregate(day=day, streams=tuple(streams),
                        peak_concurrent_streams=0)


# ---------------------------------------------------------------------------
# Helpers

def test_percentile_handles_empty():
    assert _percentile([], 0.5) == 0.0


def test_percentile_handles_single():
    assert _percentile([7.0], 0.5) == 7.0


def test_percentile_linear_interpolation():
    # [1, 2, 3, 4, 5], q=0.5 → 3
    assert _percentile([1, 2, 3, 4, 5], 0.5) == 3
    # q=0.25 → 2
    assert _percentile([1, 2, 3, 4, 5], 0.25) == 2


def test_percentile_handles_unsorted_input():
    assert _percentile([5, 1, 3, 2, 4], 0.5) == 3


def test_hour_to_time_basic():
    assert _hour_to_time(9.5) == time(9, 30)
    assert _hour_to_time(14.25) == time(14, 15)


def test_hour_to_time_clips_out_of_range():
    assert _hour_to_time(-1) == time(0, 0)
    assert _hour_to_time(25) == time(23, 59, 59)


# ---------------------------------------------------------------------------
# CODL sampling

def test_codl_samples_single_continuous_stream():
    streams = (_stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 10)),)
    samples = _codl_samples(
        streams,
        _utc(2026, 5, 15, 9),
        _utc(2026, 5, 15, 10),
        sample_interval_seconds=60,
    )
    assert len(samples) == 61
    assert all(c == 1 for c in samples)


def test_codl_samples_overlap_pushes_peak():
    streams = (
        _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 11)),
        _stream("b", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12)),
    )
    samples = _codl_samples(
        streams,
        _utc(2026, 5, 15, 9),
        _utc(2026, 5, 15, 12),
        sample_interval_seconds=60 * 30,  # 30-min step
    )
    # 09:00, 09:30, 10:00, 10:30, 11:00, 11:30, 12:00
    # peak overlap from 10:00 to 11:00 inclusive → 2 streams
    assert max(samples) == 2


def test_codl_samples_empty_window():
    samples = _codl_samples(
        (_stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 10)),),
        _utc(2026, 5, 15, 11),
        _utc(2026, 5, 15, 10),  # end before start
    )
    assert samples == []


# ---------------------------------------------------------------------------
# Cross-stream starts

def test_cross_stream_starts_counts_only_when_another_is_active():
    streams = (
        _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 11)),
        _stream("b", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12)),
    )
    cross = _count_cross_stream_starts(
        streams,
        _utc(2026, 5, 15, 9),
        _utc(2026, 5, 15, 18),
    )
    # Only 'b' started while another was active.
    assert cross == 1


def test_cross_stream_starts_sequential_streams_is_zero():
    streams = (
        _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 10)),
        _stream("b", _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 12)),
    )
    cross = _count_cross_stream_starts(
        streams,
        _utc(2026, 5, 15, 9),
        _utc(2026, 5, 15, 18),
    )
    assert cross == 0


def test_cross_stream_starts_outside_window_excluded():
    streams = (
        _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 23)),
        _stream("b", _utc(2026, 5, 15, 22), _utc(2026, 5, 15, 23)),
    )
    cross = _count_cross_stream_starts(
        streams,
        _utc(2026, 5, 15, 9),
        _utc(2026, 5, 15, 18),
    )
    # 'b' started at 22:00 — outside work window
    assert cross == 0


# ---------------------------------------------------------------------------
# Union-of-active-minutes (replaces old _off_hours_minutes head/tail helper)

def test_union_active_minutes_empty_streams_is_zero():
    assert _union_active_minutes(()) == 0


def test_union_active_minutes_single_stream_returns_duration():
    streams = (_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12)),)
    assert _union_active_minutes(streams) == 120


def test_union_active_minutes_overlapping_streams_count_once():
    """The bug we fixed: two streams 14:00-16:00 and 15:00-17:00 used to
    sum to 240 min (double-counting the overlap); now correctly union to
    180 min."""
    streams = (
        _stream("a", _utc(2026, 5, 15, 14), _utc(2026, 5, 15, 16)),
        _stream("b", _utc(2026, 5, 15, 15), _utc(2026, 5, 15, 17)),
    )
    assert _union_active_minutes(streams) == 180


def test_union_active_minutes_disjoint_streams_sum():
    streams = (
        _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 10)),
        _stream("b", _utc(2026, 5, 15, 14), _utc(2026, 5, 15, 16)),
    )
    assert _union_active_minutes(streams) == 60 + 120


def test_union_active_minutes_clip_excludes_outside_segments():
    """When clipped to a work window, only the overlap counts."""
    streams = (_stream("a", _utc(2026, 5, 15, 7), _utc(2026, 5, 15, 20)),)
    union = _union_active_minutes(
        streams,
        start=_utc(2026, 5, 15, 9),
        end=_utc(2026, 5, 15, 18),
    )
    assert union == 9 * 60  # 9 hours of overlap with the work window


# Off-hours minutes derived via union(all) minus union(in window).

def test_off_hours_minutes_stream_entirely_in_window_is_zero():
    streams = (_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 17)),)
    total = _union_active_minutes(streams)
    in_window = _union_active_minutes(
        streams,
        start=_utc(2026, 5, 15, 9),
        end=_utc(2026, 5, 15, 18),
    )
    assert total - in_window == 0


def test_off_hours_minutes_evening_extension_counts():
    streams = (_stream("a", _utc(2026, 5, 15, 17), _utc(2026, 5, 15, 20)),)
    total = _union_active_minutes(streams)
    in_window = _union_active_minutes(
        streams,
        start=_utc(2026, 5, 15, 9),
        end=_utc(2026, 5, 15, 18),
    )
    # 18:00 → 20:00 == 120 minutes outside the window
    assert total - in_window == 120


def test_off_hours_minutes_morning_extension_counts():
    streams = (_stream("a", _utc(2026, 5, 15, 7), _utc(2026, 5, 15, 10)),)
    total = _union_active_minutes(streams)
    in_window = _union_active_minutes(
        streams,
        start=_utc(2026, 5, 15, 9),
        end=_utc(2026, 5, 15, 18),
    )
    # 07:00 → 09:00 == 120 minutes outside the window
    assert total - in_window == 120


# ---------------------------------------------------------------------------
# Apportion-to-window helper (tool_error correction)

def test_apportion_to_window_zero_count_returns_zero():
    streams = (_stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 17),
                       tool_error_count=0),)
    assert _apportion_to_window(
        streams, "tool_error_count",
        _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 0


def test_apportion_to_window_stream_entirely_inside_keeps_all_events():
    streams = (_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
                       tool_error_count=8),)
    assert _apportion_to_window(
        streams, "tool_error_count",
        _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 8


def test_apportion_to_window_stream_entirely_outside_keeps_none():
    """The bug we fixed: a stream at 22:00 with 5 errors used to contribute
    5 errors to the work-hour rate; now correctly contributes 0."""
    streams = (_stream("a", _utc(2026, 5, 15, 22), _utc(2026, 5, 15, 23),
                       tool_error_count=5),)
    assert _apportion_to_window(
        streams, "tool_error_count",
        _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 0


def test_apportion_to_window_half_in_keeps_half():
    """Stream at 16:00-20:00 with 10 errors. Half in window (16:00-18:00),
    half out (18:00-20:00). Should apportion to 5."""
    streams = (_stream("a", _utc(2026, 5, 15, 16), _utc(2026, 5, 15, 20),
                       tool_error_count=10),)
    assert _apportion_to_window(
        streams, "tool_error_count",
        _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 5


# ---------------------------------------------------------------------------
# Composite

def test_composite_score_low_load_is_low():
    # codl=0.5, interruption=1, closure=0
    s = _composite_score(0.5, 1.0, 0.0)
    # codl_norm=0.1, int_norm=0.1, closure=0 → blend ≈ 0.0667 → 6.67
    assert 5.0 <= s <= 10.0


def test_composite_score_all_max_caps_at_100():
    s = _composite_score(
        CODL_NORMALISATION_CEILING * 2,
        INTERRUPTION_NORMALISATION_CEILING * 2,
        1.5,
    )
    assert s == 100.0


def test_composite_score_zero_inputs_yield_zero():
    assert _composite_score(0.0, 0.0, 0.0) == 0.0


def test_composite_weights_sum_to_one():
    assert abs(sum(COMPOSITE_WEIGHTS) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Work-window detection

def test_work_window_is_fixed_from_config_for_all_weekdays():
    # Default config (config.json) is the fixed 09:00–19:00 working day.
    windows = detect_work_windows({}, local_tz=UTC)
    assert len(windows) == 7
    assert all(w.start == time(9, 0) and w.end == time(19, 0)
               for w in windows.values())


def test_work_window_ignores_activity_distribution():
    # Even with all activity clustered at 14:00, the window stays the fixed band.
    fri = date(2026, 5, 1)
    timestamps = tuple(_utc(2026, 5, 1, 14, m) for m in range(0, 60, 6))
    agg = _agg(fri, [
        _stream("s1", _utc(2026, 5, 1, 14, 0), _utc(2026, 5, 1, 14, 59),
                user_msg_timestamps=timestamps),
    ])
    windows = detect_work_windows({fri: agg}, local_tz=UTC)
    assert windows[fri.weekday()].start == time(9, 0)
    assert windows[fri.weekday()].end == time(19, 0)


def test_work_window_default_comes_from_config_not_code(tmp_path):
    """The window is configurable via config.json, not hardcoded — a custom
    config file changes it."""
    from stress_levels.config import _CONFIG_CACHE, load_config

    cfg = tmp_path / "config.json"
    cfg.write_text('{"work_window": {"start": "07:30", "end": "16:00"}}', encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.work_window.start == time(7, 30)
    assert loaded.work_window.end == time(16, 0)
    _CONFIG_CACHE.clear()


# ---------------------------------------------------------------------------
# per_day_metrics integration

def test_per_day_metrics_empty_day_returns_zeros():
    agg = DayAggregate(day=date(2026, 5, 15))
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    assert m.day == date(2026, 5, 15)
    assert m.codl_avg == 0.0
    assert m.codl_peak == 0
    assert m.interruption_rate == 0.0
    assert m.closure_deficit == 0.0
    assert m.off_hours_minutes == 0
    assert m.composite == 0.0


def test_per_day_metrics_single_stream_low_load():
    streams = [
        _stream("s1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 11),
                user_msg_count=2,
                user_msg_timestamps=(_utc(2026, 5, 15, 10), _utc(2026, 5, 15, 11))),
    ]
    agg = _agg(date(2026, 5, 15), streams)
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    # CODL == 1 for the in-window minute samples that overlap the stream
    assert m.codl_peak == 1
    assert 0 < m.codl_avg <= 1.0
    # closure_deficit measures samples with CODL > 1 — single stream → 0
    assert m.closure_deficit == 0.0
    # off_hours_minutes should be 0 (stream entirely inside window)
    assert m.off_hours_minutes == 0
    # composite low but non-zero
    assert 0.0 < m.composite < 30.0


def test_per_day_metrics_two_parallel_streams_raises_deficit():
    streams = [
        _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14)),
        _stream("b", _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 13)),
    ]
    agg = _agg(date(2026, 5, 15), streams)
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    # codl_avg is time-weighted across the full work window; with only 2hr
    # of two-stream overlap out of 9hr, it sits well below 1 — peak and
    # deficit are the right signals here.
    assert m.codl_peak == 2
    assert m.codl_avg > 0
    assert m.closure_deficit > 0


def test_per_day_metrics_evening_session_adds_off_hours():
    streams = [
        _stream("s1", _utc(2026, 5, 15, 19), _utc(2026, 5, 15, 21)),
    ]
    agg = _agg(date(2026, 5, 15), streams)
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    # 19:00–21:00 == 120 minutes after work window ended at 18:00
    assert m.off_hours_minutes == 120


def test_per_day_metrics_assigns_work_window_local():
    agg = DayAggregate(day=date(2026, 5, 15))
    window = WorkWindow(weekday=4, start=time(8, 30), end=time(17, 45))
    m = per_day_metrics(agg, window, UTC)
    assert m.work_window_local == (time(8, 30), time(17, 45))


# ---------------------------------------------------------------------------
# Personal optimum

def test_personal_optimum_returns_none_when_too_few_days():
    days = {
        date(2026, 5, i): DayMetrics(day=date(2026, 5, i), codl_avg=1.0)
        for i in range(1, 5)  # 4 days
    }
    assert derive_personal_optimum(days) is None


def test_personal_optimum_returns_none_when_no_activity():
    days = {
        date(2026, 5, i): DayMetrics(day=date(2026, 5, i), codl_avg=0)
        for i in range(1, 31)
    }
    assert derive_personal_optimum(days) is None


def _weekdays_from(start: date, n: int) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_personal_optimum_returns_bucket_with_best_score():
    """Bucket at codl=1.0–1.5 has low closure-deficit; bucket at 3+ has high
    deficit and off-hours. Optimum should fall in the lower bucket.

    Uses only weekdays — weekends are excluded from optimum derivation."""
    days = {}
    workdays = _weekdays_from(date(2026, 5, 4), 16)  # 16 weekdays
    # First 8: low load
    for d in workdays[:8]:
        days[d] = DayMetrics(day=d, codl_avg=1.2,
                             closure_deficit=0.1, off_hours_minutes=5)
    # Next 8: high load
    for d in workdays[8:]:
        days[d] = DayMetrics(day=d, codl_avg=3.5,
                             closure_deficit=0.7, off_hours_minutes=120)
    optimum = derive_personal_optimum(days, min_days=14)
    assert optimum is not None
    assert 0.5 < optimum < 2.5


def test_personal_optimum_requires_two_days_per_bucket():
    """Single-day buckets are ignored so a single outlier doesn't win."""
    days = {}
    workdays = _weekdays_from(date(2026, 5, 4), 14)
    # 13 days at codl=2.0
    for d in workdays[:13]:
        days[d] = DayMetrics(day=d, codl_avg=2.0,
                             closure_deficit=0.3, off_hours_minutes=10)
    # 1 outlier at codl=0.5
    days[workdays[13]] = DayMetrics(
        day=workdays[13], codl_avg=0.5,
        closure_deficit=0.0, off_hours_minutes=0,
    )
    optimum = derive_personal_optimum(days, min_days=14)
    # Optimum must come from the codl=2.0 bucket — outlier had only 1 day
    assert optimum is not None
    assert 1.5 < optimum < 2.5


def test_personal_optimum_excludes_weekends_from_active_days():
    """Even with many weekend days at low CODL, optimum derivation uses
    only weekday samples."""
    days = {}
    # 13 weekdays at high load
    for d in _weekdays_from(date(2026, 5, 4), 13):
        days[d] = DayMetrics(day=d, codl_avg=2.5,
                             closure_deficit=0.5, off_hours_minutes=20)
    # 10 Saturdays/Sundays at fake-low CODL (shouldn't influence the optimum)
    weekend = date(2026, 5, 2)  # a Saturday
    weekend_count = 0
    while weekend_count < 10:
        if weekend.weekday() >= 5:
            days[weekend] = DayMetrics(day=weekend, codl_avg=0.1,
                                       closure_deficit=0.0, off_hours_minutes=0)
            weekend_count += 1
        weekend += timedelta(days=1)
    optimum = derive_personal_optimum(days, min_days=10)
    # Optimum must reflect the weekday cluster only
    assert optimum is not None
    assert 2.0 < optimum < 3.0


def test_per_day_metrics_saturday_is_off_hours_only():
    """Saturday activity sets off_hours_minutes but leaves composite at 0
    and other axes untouched — weekends are never working days."""
    sat = date(2026, 5, 9)  # Saturday
    assert sat.weekday() == 5
    streams = [
        _stream("s1", _utc(2026, 5, 9, 14, 0), _utc(2026, 5, 9, 16, 30)),
    ]
    agg = _agg(sat, streams)
    window = WorkWindow(weekday=5, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    assert m.composite == 0.0
    assert m.codl_avg == 0.0
    assert m.interruption_rate == 0.0
    assert m.closure_deficit == 0.0
    # 14:00 → 16:30 == 150 min
    assert m.off_hours_minutes == 150
    # No work-window on weekends
    assert m.work_window_local is None


def test_per_day_metrics_sunday_with_no_streams_is_clean():
    sun = date(2026, 5, 10)
    assert sun.weekday() == 6
    agg = DayAggregate(day=sun)
    window = WorkWindow(weekday=6, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    assert m.composite == 0.0
    assert m.off_hours_minutes == 0
    assert m.work_window_local is None


def test_build_profile_percentiles_exclude_weekend_activity():
    """A high-stress Saturday must not move the weekly p75/p90."""
    from stress_levels.aggregate import StreamDayActivity
    fri = date(2026, 5, 8)  # Friday
    sat = date(2026, 5, 9)  # Saturday
    fri_streams = [
        _stream("f1", _utc(2026, 5, 8, 9, 0), _utc(2026, 5, 8, 17, 0),
                user_msg_timestamps=tuple(_utc(2026, 5, 8, h, 0) for h in range(9, 17))),
    ]
    sat_streams = [
        _stream("s1", _utc(2026, 5, 9, 9, 0), _utc(2026, 5, 9, 23, 0)),
    ]
    aggs = {
        fri: _agg(fri, fri_streams),
        sat: _agg(sat, sat_streams),
    }
    profile = build_profile(aggs, local_tz=UTC)
    # Friday has composite > 0; Saturday's composite is 0 by design.
    assert profile.days[fri].composite > 0
    assert profile.days[sat].composite == 0
    # Saturday's off-hours minutes are recorded
    assert profile.days[sat].off_hours_minutes > 0
    # Percentiles are computed only over the workday — single sample
    assert profile.composite_p50 is not None
    assert profile.composite_p50 == profile.days[fri].composite


# ---------------------------------------------------------------------------
# build_profile end-to-end

def test_build_profile_empty_aggregates_yields_empty_profile():
    profile = build_profile({}, baseline_days=30, local_tz=UTC)
    assert profile.days == {}
    assert profile.personal_optimum is None
    assert profile.composite_p50 is None


def test_build_profile_one_active_day():
    streams = [
        _stream("s1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12),
                user_msg_count=2,
                user_msg_timestamps=(_utc(2026, 5, 15, 10), _utc(2026, 5, 15, 11))),
    ]
    agg = _agg(date(2026, 5, 15), streams)
    profile = build_profile({date(2026, 5, 15): agg}, local_tz=UTC)
    assert date(2026, 5, 15) in profile.days
    m = profile.days[date(2026, 5, 15)]
    assert m.codl_peak == 1
    assert m.composite > 0


def test_build_profile_computes_percentiles_over_active_days():
    streams_lo = [_stream("a", _utc(2026, 5, 4, 10), _utc(2026, 5, 4, 11))]
    streams_hi = [
        _stream("a", _utc(2026, 5, 5, 10), _utc(2026, 5, 5, 16)),
        _stream("b", _utc(2026, 5, 5, 11), _utc(2026, 5, 5, 15)),
    ]
    aggs = {
        date(2026, 5, 4): _agg(date(2026, 5, 4), streams_lo),
        date(2026, 5, 5): _agg(date(2026, 5, 5), streams_hi),
    }
    profile = build_profile(aggs, local_tz=UTC)
    assert profile.composite_p50 is not None
    assert profile.composite_p90 is not None
    assert profile.composite_p50 <= profile.composite_p90


def test_build_profile_includes_work_windows_per_weekday():
    profile = build_profile({}, local_tz=UTC)
    # All 7 weekdays should be populated with the configured fixed window.
    assert set(profile.work_windows) == set(range(7))
    assert all(w.start == time(9, 0) and w.end == time(19, 0)
               for w in profile.work_windows.values())
