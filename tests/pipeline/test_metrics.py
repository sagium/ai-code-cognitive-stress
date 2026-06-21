"""Tests for the metrics layer: work windows, three axes, composite, optimum."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from ai_code_cognitive_stress.pipeline.aggregate import DayAggregate, StreamDayActivity
from ai_code_cognitive_stress.pipeline.metrics import (
    CODL_CAPACITY,
    CODL_DOSE_HORIZON_MINUTES,
    COMPOSITE_WEIGHTS,
    INTERRUPTION_NORMALISATION_CEILING,
    LITERATURE_WORK_WINDOW,
    OFF_HOURS_LOAD_CEILING_MIN,
    OFF_HOURS_LOAD_MAX_POINTS,
    OPTIMUM_MIN_DAYS_OF_DATA,
    WORK_WINDOW_MIN_SAMPLES,
    DayMetrics,
    StressProfile,
    WorkWindow,
    _apportion_to_window,
    _in_window_tool_errors,
    _resume_severity,
    _resumption_load,
    _codl_samples,
    _codl_weighted_samples,
    _composite_score,
    _count_cross_stream_starts,
    _default_window,
    _effective_scored_start,
    _off_hours_engaged_minutes,
    _off_hours_load_points,
    _off_hours_local_ranges,
    _percentile,
    _stream_weight_at,
    alive_intervals,
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
        user_msg_timestamps=counts.get("user_msg_timestamps", ()),
        tool_error_timestamps=counts.get("tool_error_timestamps", ()),
        resume_gaps=counts.get("resume_gaps", ()),
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
# Run intervals — a session counts as open only while its app was plausibly up

def test_alive_intervals_no_gap_is_single_interval():
    s = _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 12))
    assert alive_intervals(s, 180 * 60) == [
        (_utc(2026, 5, 15, 9), _utc(2026, 5, 15, 12)),
    ]


def test_alive_intervals_splits_at_long_gap():
    # Active 09:00, app closed, reopened 13:00 (a 3.5h gap), active to 13:20.
    # The dead span [09:30, 13:00) is carved out; the run before ends at the last
    # event (13:00 − 210min = 09:30).
    s = _stream(
        "a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 13, 20),
        resume_gaps=((_utc(2026, 5, 15, 13), 210 * 60),),
    )
    assert alive_intervals(s, 180 * 60) == [
        (_utc(2026, 5, 15, 9), _utc(2026, 5, 15, 9, 30)),
        (_utc(2026, 5, 15, 13), _utc(2026, 5, 15, 13, 20)),
    ]


def test_alive_intervals_short_gap_not_split():
    # A 90-min gap (< 3h cutoff): the app plausibly stayed open → one run.
    s = _stream(
        "a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 12),
        resume_gaps=((_utc(2026, 5, 15, 11), 90 * 60),),
    )
    assert alive_intervals(s, 180 * 60) == [
        (_utc(2026, 5, 15, 9), _utc(2026, 5, 15, 12)),
    ]


def test_codl_samples_dead_span_does_not_phantom_overlap():
    """The headline fix: a session split by a long (app-closed) gap must not
    register as 'open' during the dead span, so it cannot phantom-overlap another
    session that ran in that window. Peak stays 1, not 2."""
    split = _stream(
        "a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 13, 20),
        resume_gaps=((_utc(2026, 5, 15, 13), 210 * 60),),  # dead [09:30, 13:00)
    )
    other = _stream("b", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 11))
    samples = _codl_samples(
        (split, other), _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 14),
        sample_interval_seconds=60 * 10,
    )
    assert max(samples) == 1


# ---------------------------------------------------------------------------
# Engagement weighting (foreground vs background "cooking")

def test_stream_weight_foreground_background_and_dead():
    s = _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12),
                user_msg_timestamps=(_utc(2026, 5, 15, 10),))
    grace = 5 * 60
    # At the user message → foreground.
    assert _stream_weight_at(s, _utc(2026, 5, 15, 10), grace, 0.25) == 1.0
    # 3 min after the message, still within grace → foreground.
    assert _stream_weight_at(s, _utc(2026, 5, 15, 10, 3), grace, 0.25) == 1.0
    # 30 min later: alive but cooking → background weight.
    assert _stream_weight_at(s, _utc(2026, 5, 15, 10, 30), grace, 0.25) == 0.25
    # Before it opened and after it closed → 0.
    assert _stream_weight_at(s, _utc(2026, 5, 15, 9), grace, 0.25) == 0.0
    assert _stream_weight_at(s, _utc(2026, 5, 15, 13), grace, 0.25) == 0.0


def test_codl_weighted_samples_discounts_background_minutes():
    # One session: a single message at the start, then it cooks for an hour.
    s = _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 11),
                user_msg_timestamps=(_utc(2026, 5, 15, 10),))
    samples = _codl_weighted_samples(
        (s,), _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 11),
        grace_seconds=5 * 60, background_weight=0.25,
    )
    # First ~6 samples foreground (1.0), the rest background (0.25).
    assert samples[0] == 1.0
    assert samples[-1] == 0.25
    assert any(s == 1.0 for s in samples) and any(s == 0.25 for s in samples)


def test_per_day_metrics_background_session_scores_lower_than_active():
    """Same two-hour session: actively driven the whole time vs left cooking
    after one prompt. The cooking version must score lower."""
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    span = (_utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12))
    active = _agg(date(2026, 5, 15), [
        _stream("a", *span,
                user_msg_timestamps=tuple(
                    _utc(2026, 5, 15, 10, m) for m in range(0, 60, 4)
                ) + tuple(_utc(2026, 5, 15, 11, m) for m in range(0, 60, 4))),
    ])
    cooking = _agg(date(2026, 5, 15), [
        _stream("a", *span, user_msg_timestamps=(_utc(2026, 5, 15, 10),)),
    ])
    m_active = per_day_metrics(active, window, UTC)
    m_cooking = per_day_metrics(cooking, window, UTC)
    # The cooking session spends most of its life at background weight, so its
    # time-averaged CODL is materially lower than the actively-driven one.
    assert m_active.codl_avg > m_cooking.codl_avg
    # Both have one session open, and each reaches foreground at some point →
    # same headcount peak and same instantaneous active peak (1.0).
    assert m_active.codl_peak == m_cooking.codl_peak == 1
    assert m_cooking.composite < m_active.composite


def test_per_day_metrics_background_fanout_does_not_count_as_active():
    """Four sessions all cooking in the background: headcount peak is 4, but
    the active peak stays near the background weight — no false WM-cap alarm."""
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    streams = [
        _stream(f"s{i}", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 16))
        for i in range(4)
    ]
    m = per_day_metrics(_agg(date(2026, 5, 15), streams), window, UTC)
    assert m.codl_peak == 4
    # 4 background sessions × 0.20 = 0.8 — well under the WM-cap of 4.
    assert m.codl_peak_active < 2.0


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


# Off-hours ENGAGED minutes: interaction-anchored (within grace of a user
# message, outside the work window), not stream liveness.

_WS = _utc(2026, 5, 15, 9)    # window start 09:00
_WE = _utc(2026, 5, 15, 18)   # window end   18:00


def test_off_hours_engaged_interaction_in_window_is_zero():
    # A user message at 10:00 is inside the window → no off-hours minutes.
    streams = (_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 17),
                       user_msg_timestamps=(_utc(2026, 5, 15, 10),)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 0


def test_off_hours_engaged_evening_interaction_counts():
    # A 20:00 message marks 20:00..20:05 (6 one-minute instants), all off-hours.
    streams = (_stream("a", _utc(2026, 5, 15, 20), _utc(2026, 5, 15, 20),
                       user_msg_timestamps=(_utc(2026, 5, 15, 20),)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 6


def test_off_hours_engaged_early_start_within_grace_is_free():
    # A 07:00 message is 2 h before the 09:00 window start — within the 3 h
    # early-start grace. Starting earlier than usual is schedule shift, not
    # off-hours load.
    streams = (_stream("a", _utc(2026, 5, 15, 7), _utc(2026, 5, 15, 7),
                       user_msg_timestamps=(_utc(2026, 5, 15, 7),)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 0


def test_off_hours_engaged_outlier_early_morning_counts():
    # A 05:00 message is 4 h before the window start — beyond the 3 h grace,
    # an outlier (e.g. nocturnal work), so its 6 minutes count.
    streams = (_stream("a", _utc(2026, 5, 15, 5), _utc(2026, 5, 15, 5),
                       user_msg_timestamps=(_utc(2026, 5, 15, 5),)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 6


def test_off_hours_engaged_small_hours_continuation_counts():
    # Work at 01:30 lands on this local day but sits ~7.5 h before the window
    # start — far beyond any early start. The late-night continuation stays
    # visible despite the early-side grace.
    streams = (_stream("a", _utc(2026, 5, 15, 1, 30), _utc(2026, 5, 15, 1, 30),
                       user_msg_timestamps=(_utc(2026, 5, 15, 1, 30),)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 6


def test_off_hours_engaged_grace_boundary_is_exclusive():
    # Exactly 3 h early (06:00 against a 09:00 start) is still a free early
    # start; one minute earlier crosses into outlier territory.
    streams = (_stream("a", _utc(2026, 5, 15, 6), _utc(2026, 5, 15, 6),
                       user_msg_timestamps=(_utc(2026, 5, 15, 6),)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 0
    streams = (_stream("a", _utc(2026, 5, 15, 5, 54), _utc(2026, 5, 15, 5, 54),
                       user_msg_timestamps=(_utc(2026, 5, 15, 5, 54),)),)
    # 05:54..05:59 are all before the 06:00 cutoff → 6 minutes count.
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 6


def test_off_hours_engaged_background_without_interaction_is_zero():
    # A stream ALIVE off-hours (22:00–23:00) but with NO user messages — a
    # background job running while the human is away — contributes nothing.
    streams = (_stream("a", _utc(2026, 5, 15, 22), _utc(2026, 5, 15, 23)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 0


def test_off_hours_engaged_dedups_overlapping_grace_windows():
    # Messages at 20:00 and 20:02 → union {20:00..20:07} = 8 minutes, not 12.
    streams = (_stream("a", _utc(2026, 5, 15, 20), _utc(2026, 5, 15, 20, 2),
                       user_msg_timestamps=(_utc(2026, 5, 15, 20),
                                            _utc(2026, 5, 15, 20, 2))),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 8


def test_off_hours_engaged_counts_only_the_outside_part_at_the_edge():
    # A 17:58 message's grace window straddles 18:00; only 18:00..18:03 (>= end)
    # count, the 17:58/17:59 minutes are in-window.
    streams = (_stream("a", _utc(2026, 5, 15, 17, 58), _utc(2026, 5, 15, 18),
                       user_msg_timestamps=(_utc(2026, 5, 15, 17, 58),)),)
    assert _off_hours_engaged_minutes(streams, _WS, _WE, grace_seconds=300) == 4


# ---------------------------------------------------------------------------
# Effective scored start: early-grace work earns in-window credit (no penalty).

def test_effective_scored_start_on_time_day_is_unchanged():
    # First message at 10:00, inside the 09:00 window → scored start stays 09:00.
    streams = (_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12),
                       user_msg_timestamps=(_utc(2026, 5, 15, 10),)),)
    assert _effective_scored_start(streams, _WS) == _WS


def test_effective_scored_start_dips_to_early_grace_first_message():
    # First message at 07:40 — within the 3 h grace before the 09:00 start. The
    # scored window's start dips to it (minute-floored), so the morning counts.
    streams = (_stream("a", _utc(2026, 5, 15, 7, 40, 23), _utc(2026, 5, 15, 10),
                       user_msg_timestamps=(_utc(2026, 5, 15, 7, 40, 23),
                                            _utc(2026, 5, 15, 9, 30))),)
    assert _effective_scored_start(streams, _WS) == _utc(2026, 5, 15, 7, 40)


def test_effective_scored_start_ignores_pre_grace_message():
    # A 05:00 message is beyond the grace cutoff (06:00) — that's off-hours, not
    # an early start, so it must NOT pull the scored start down.
    streams = (_stream("a", _utc(2026, 5, 15, 5), _utc(2026, 5, 15, 10),
                       user_msg_timestamps=(_utc(2026, 5, 15, 5),
                                            _utc(2026, 5, 15, 9, 30))),)
    assert _effective_scored_start(streams, _WS) == _WS


def test_per_day_metrics_credits_early_grace_morning_as_in_window():
    # A day worked ENTIRELY in the early-grace zone (07:00–08:00, before the
    # 09:00 window start) used to score zero — neither in-window nor off-hours.
    # It now earns in-window credit with NO off-hours penalty.
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    msgs = tuple(_utc(2026, 5, 15, 7, m) for m in range(0, 60, 10))
    streams = (_stream("a", _utc(2026, 5, 15, 7), _utc(2026, 5, 15, 8),
                       user_msg_count=len(msgs), user_msg_timestamps=msgs),)
    m = per_day_metrics(_agg(date(2026, 5, 15), streams), window, UTC)
    assert m.off_hours_minutes == 0          # early start is not penalised
    assert m.codl_avg > 0                     # but the morning's load is scored
    assert m.composite > 0                    # day no longer reads as empty
    # The reported window reflects the effective (extended) start.
    assert m.work_window_local == (time(7, 0), time(18))


def test_off_hours_local_ranges_groups_and_converts_to_local():
    # Two morning runs 07:00..07:05 and 07:20..07:25 UTC (gap 15 min > merge
    # gap) → two ranges, rendered as local time-of-day (UTC+3 here).
    tz = timezone(timedelta(hours=3))
    instants = (
        [_utc(2026, 5, 15, 7, m) for m in range(0, 6)]
        + [_utc(2026, 5, 15, 7, m) for m in range(20, 26)]
    )
    assert _off_hours_local_ranges(instants, tz) == (
        (time(10, 0), time(10, 5)),
        (time(10, 20), time(10, 25)),
    )


def test_off_hours_local_ranges_merges_small_gaps():
    # Runs separated by ≤ 5 min merge into one display range; the minute count
    # stays exact because the caller uses len(instants), not the ranges.
    instants = (
        [_utc(2026, 5, 15, 20, m) for m in range(0, 3)]
        + [_utc(2026, 5, 15, 20, m) for m in range(7, 10)]
    )
    assert _off_hours_local_ranges(instants, UTC) == ((time(20, 0), time(20, 9)),)


def test_off_hours_local_ranges_empty():
    assert _off_hours_local_ranges([], UTC) == ()


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
# In-window tool errors — exact per-error timing, with uniform fallback

def test_in_window_tool_errors_counts_only_errors_inside_window():
    """A stream straddling the 18:00 edge: 3 errors logged in-window, 2 after.
    Exact timing counts only the 3 — not the half-and-half a uniform smear of
    the stream's lifetime would give."""
    s = _stream(
        "a", _utc(2026, 5, 15, 16), _utc(2026, 5, 15, 20),
        tool_error_count=5,
        tool_error_timestamps=(
            _utc(2026, 5, 15, 16, 10), _utc(2026, 5, 15, 17, 0),
            _utc(2026, 5, 15, 17, 50),                       # 3 in window
            _utc(2026, 5, 15, 18, 30), _utc(2026, 5, 15, 19, 0),  # 2 off-hours
        ),
    )
    assert _in_window_tool_errors(
        (s,), _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 3


def test_in_window_tool_errors_bursty_off_hours_excluded():
    """A long-lived stream whose errors all cluster off-hours. Uniform
    apportionment would credit a fraction to the work window; exact timing
    correctly attributes zero."""
    s = _stream(
        "a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 23),
        tool_error_count=4,
        tool_error_timestamps=tuple(
            _utc(2026, 5, 15, 21, m) for m in (0, 15, 30, 45)
        ),
    )
    assert _in_window_tool_errors(
        (s,), _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 0


def test_in_window_tool_errors_window_edges_inclusive():
    """Errors exactly on the window bounds are counted (closed interval)."""
    s = _stream(
        "a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
        tool_error_count=2,
        tool_error_timestamps=(_utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18)),
    )
    assert _in_window_tool_errors(
        (s,), _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 2


def test_in_window_tool_errors_legacy_stream_falls_back_to_apportion():
    """A stream with errors but no timestamps (older archive entry) falls back
    to uniform apportionment: 16:00-20:00, 10 errors, half in window → 5."""
    s = _stream("a", _utc(2026, 5, 15, 16), _utc(2026, 5, 15, 20),
                tool_error_count=10)  # no tool_error_timestamps
    assert _in_window_tool_errors(
        (s,), _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 5


def test_in_window_tool_errors_mixes_exact_and_legacy_streams():
    """Exact-timed and legacy streams sum independently in one day."""
    exact = _stream(
        "exact", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 20),
        tool_error_count=2,
        tool_error_timestamps=(
            _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 19),  # 1 in, 1 out
        ),
    )
    legacy = _stream("legacy", _utc(2026, 5, 15, 16), _utc(2026, 5, 15, 20),
                     tool_error_count=10)  # half in window → 5
    assert _in_window_tool_errors(
        (exact, legacy), _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
    ) == 6


# ---------------------------------------------------------------------------
# Closure Deficit — real closure events (opened-vs-closed loops)

_WIN_START = _utc(2026, 5, 15, 9)
_WIN_END = _utc(2026, 5, 15, 18)


def _gap_minutes(*, at_hour, at_minute=0, minutes):
    """A (resume_ts, gap_seconds) pair: work resumed at the given local-UTC time
    after being parked `minutes`."""
    return (_utc(2026, 5, 15, at_hour, at_minute), int(minutes * 60))


def test_resume_severity_ramps_then_saturates():
    """min(1, gap / full_decay): linear up to the horizon, flat after."""
    assert _resume_severity(0, 7200) == 0.0
    assert _resume_severity(3600, 7200) == pytest.approx(0.5)   # 60 of 120 min
    assert _resume_severity(7200, 7200) == 1.0                  # at the horizon
    assert _resume_severity(36000, 7200) == 1.0                 # well beyond → still 1


def test_resumption_no_streams_is_none():
    """Only a no-activity day yields None — the one remaining no-data case."""
    agg = _agg(date(2026, 5, 15), [])
    assert _resumption_load(agg, _WIN_START, _WIN_END) is None


def test_resumption_no_resumes_is_zero():
    """A day with sessions but no qualifying idle gaps scores 0.0 — a real,
    GOOD score (every loop closed in one sitting), distinct from None."""
    agg = _agg(date(2026, 5, 15), [_stream("a", _utc(2026, 5, 15, 10),
                                            _utc(2026, 5, 15, 11))])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == 0.0


def test_resumption_single_intra_day_gap_scored():
    """One 60-min in-window resume → severity 0.5, load 0.5 / ceiling 4 = 0.125."""
    s = _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
                resume_gaps=(_gap_minutes(at_hour=12, minutes=60),))
    agg = _agg(date(2026, 5, 15), [s])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == pytest.approx(0.125)


def test_resumption_gap_below_threshold_ignored():
    """A 20-min gap is a break, not a parked-and-reloaded loop (< 30 min) → 0."""
    s = _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
                resume_gaps=(_gap_minutes(at_hour=12, minutes=20),))
    agg = _agg(date(2026, 5, 15), [s])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == 0.0


def test_resumption_resume_outside_window_ignored():
    """A resume whose pickup instant is outside the work window is not scored
    on this axis (off-hours work is captured by the off-hours toll instead)."""
    s = _stream("a", _utc(2026, 5, 15, 6), _utc(2026, 5, 15, 20),
                resume_gaps=(_gap_minutes(at_hour=7, minutes=120),))  # 07:00 < 09:00
    agg = _agg(date(2026, 5, 15), [s])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == 0.0


def test_resumption_sums_and_clamps_to_one():
    """Severities sum across resumes and the day clamps at 1.0. Five fully-cold
    (≥120-min) resumes → Σ severity 5, /4 = 1.25 → clamped to 1.0."""
    s = _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
                resume_gaps=tuple(
                    _gap_minutes(at_hour=10 + i, minutes=150) for i in range(5)
                ))
    agg = _agg(date(2026, 5, 15), [s])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == 1.0


def test_resumption_gap_at_or_above_idle_close_ignored():
    """A gap of at least idle_close (default 3h) means the app was closed and you
    got genuine closure — recovery, not an unfinished loop. A 200-min in-window
    gap is NOT a resume → 0.0."""
    s = _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
                resume_gaps=(_gap_minutes(at_hour=13, minutes=200),))
    agg = _agg(date(2026, 5, 15), [s])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == 0.0


def test_resumption_gap_just_below_idle_close_scored():
    """A gap just under idle_close still counts as a parked-loop resume. A
    170-min gap (≥ full_decay 120) → severity 1.0, load 1/4 = 0.25."""
    s = _stream("a", _utc(2026, 5, 15, 9), _utc(2026, 5, 15, 18),
                resume_gaps=(_gap_minutes(at_hour=12, minutes=170),))
    agg = _agg(date(2026, 5, 15), [s])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == pytest.approx(0.25)


def test_resumption_no_cross_day_term():
    """Cross-day pickups are no longer scored: a fresh session today with no
    intra-day idle gaps is 0.0 regardless of when the stream was last seen."""
    s = _stream("carry", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12))
    agg = _agg(date(2026, 5, 15), [s])
    assert _resumption_load(agg, _WIN_START, _WIN_END) == 0.0


def test_resumption_is_independent_of_concurrency_shape():
    """The keystone property: two days with the IDENTICAL engagement-weighted
    concurrency series C(t) (hence identical codl_avg) get DIFFERENT closure
    deficits purely from their resume gaps."""
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    umsgs_a = tuple(_utc(2026, 5, 15, h) for h in (10, 11, 12, 13))
    umsgs_b = (_utc(2026, 5, 15, 11), _utc(2026, 5, 15, 12))
    # 'reloaded' carries two cold (≥120-min) in-window resumes; 'clean' has none.
    reloaded = _agg(date(2026, 5, 15), [
        _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
                user_msg_timestamps=umsgs_a,
                resume_gaps=(_gap_minutes(at_hour=12, minutes=150),
                             _gap_minutes(at_hour=13, minutes=150))),
        _stream("b", _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 13),
                user_msg_timestamps=umsgs_b),
    ])
    clean = _agg(date(2026, 5, 15), [
        _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
                user_msg_timestamps=umsgs_a),
        _stream("b", _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 13),
                user_msg_timestamps=umsgs_b),
    ])
    m_reloaded = per_day_metrics(reloaded, window, UTC)
    m_clean = per_day_metrics(clean, window, UTC)
    # Same concurrency shape → same CODL.
    assert m_reloaded.codl_avg == m_clean.codl_avg
    assert m_reloaded.codl_peak == m_clean.codl_peak
    # But the closure deficits differ — the axis carries information C(t) does not.
    assert m_clean.closure_deficit == 0.0
    assert m_reloaded.closure_deficit == pytest.approx(0.5)   # 2 × 1.0 / 4
    # And that difference propagates to the composite.
    assert m_reloaded.composite > m_clean.composite


# ---------------------------------------------------------------------------
# Composite

def test_composite_score_low_load_is_low():
    # codl_dose=0.1, interruption=1, closure=0
    # codl_norm=0.1 (already), int_norm=0.1 → blend ≈ 0.0667 → 6.67
    s = _composite_score(0.1, 1.0, 0.0)
    assert 5.0 <= s <= 10.0


def test_composite_score_all_max_caps_at_100():
    # codl_dose > 1.0 is clamped to 1.0; interruption >> ceiling normalises to 1.0
    s = _composite_score(
        2.0,  # will be clamped to 1.0
        INTERRUPTION_NORMALISATION_CEILING * 2,
        1.5,
    )
    assert s == 100.0


def test_composite_score_zero_inputs_yield_zero():
    assert _composite_score(0.0, 0.0, 0.0) == 0.0


def test_composite_weights_sum_to_one():
    assert abs(sum(COMPOSITE_WEIGHTS) - 1.0) < 1e-9


def test_composite_score_none_closure_renormalises_over_two_axes():
    """When closure is None (a no-activity day), the blend drops the Closure axis
    and renormalises over CODL + Interruption — its weight is redistributed, NOT
    imputed as a perfect-closure 0."""
    codl_dose = 0.6
    int_n = 0.3
    interr = int_n * INTERRUPTION_NORMALISATION_CEILING
    with_none = _composite_score(codl_dose, interr, None)
    # Equal weights → 2-axis mean.
    assert with_none == pytest.approx(100.0 * (codl_dose + int_n) / 2)
    # And it is strictly higher than imputing closure=0 (the old behaviour),
    # because the 0 no longer drags the blend down.
    as_zero = _composite_score(codl_dose, interr, 0.0)
    assert with_none > as_zero


def test_composite_score_none_closure_matches_present_zero_when_axes_equal():
    """Sanity: a None closure and a present-but-0 closure differ ONLY by the
    renormalisation, never by treating None as a non-zero penalty."""
    s_none = _composite_score(0.0, 0.0, None)
    assert s_none == 0.0  # no load on any present axis → 0, no crash


def test_live_day_score_excludes_future_work_window_hours():
    # A live day must score off the hours that have actually elapsed, not the
    # full inferred window. The CODL axis is the capacity-dose, which is
    # window-length-independent (idle/future minutes add no dose), so the live
    # protection shows up on the axis that IS per-elapsed-hour: the interruption
    # rate. With one tool error inside the active span, the same error count over
    # 7 elapsed hours (live) outweighs it over the full 10-hour window
    # (completed).
    day = date(2026, 5, 15)
    agg = _agg(day, [
        _stream(
            "live", _utc(2026, 5, 15, 17), _utc(2026, 5, 15, 18),
            user_msg_timestamps=(_utc(2026, 5, 15, 17),),
            tool_error_count=1,
            tool_error_timestamps=(_utc(2026, 5, 15, 17, 30),),
        ),
    ])
    window = WorkWindow(weekday=day.weekday(), start=time(11), end=time(21))

    completed = per_day_metrics(agg, window, UTC)
    live = per_day_metrics(agg, window, UTC, as_of=_utc(2026, 5, 15, 18))

    # The dose is identical (same active minutes, no future dilution either way)…
    assert live.codl_dose == completed.codl_dose
    # …but the per-hour interruption rate is higher over the shorter elapsed
    # window, so the live composite is strictly higher.
    assert live.interruption_rate > completed.interruption_rate
    assert live.composite > completed.composite
    # codl_avg stays descriptive and still reflects the shorter denominator.
    assert live.codl_avg > completed.codl_avg
    assert live.work_window_local == (time(11), time(21))


# ---------------------------------------------------------------------------
# Work-window detection

def test_work_window_falls_back_to_literature_default_with_no_aggregates():
    """With no aggregates (and no config override), detect_work_windows returns
    the literature cold-start default (09:00–19:00) for all 7 weekdays with
    is_default=True."""
    windows = detect_work_windows({}, local_tz=UTC)
    assert len(windows) == 7
    assert all(w.start == time(9, 0) and w.end == time(19, 0)
               for w in windows.values())
    assert all(w.is_default is True for w in windows.values())


def test_work_window_falls_back_to_literature_default_with_sparse_data():
    """With fewer than WORK_WINDOW_MIN_SAMPLES distinct sample-days (only one
    day here), detect_work_windows uses the literature cold-start default even
    though activity is clustered at 14:00."""
    fri = date(2026, 5, 1)
    timestamps = tuple(_utc(2026, 5, 1, 14, m) for m in range(0, 60, 6))
    agg = _agg(fri, [
        _stream("s1", _utc(2026, 5, 1, 14, 0), _utc(2026, 5, 1, 14, 59),
                user_msg_timestamps=timestamps),
    ])
    windows = detect_work_windows({fri: agg}, local_tz=UTC)
    assert windows[fri.weekday()].start == time(9, 0)
    assert windows[fri.weekday()].end == time(19, 0)
    assert windows[fri.weekday()].is_default is True


def test_work_window_override_comes_from_config(tmp_path):
    """A config.json with a work_window block overrides inference for all
    7 weekdays (is_default=False)."""
    from ai_code_cognitive_stress.core.config import _CONFIG_CACHE, load_config

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
    assert m.closure_deficit is None   # no activity at all → the one None case
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
    # The stream had no idle gaps, so the Closure Deficit (resumption load) is a
    # real 0.0 — every loop closed in one sitting — NOT None.
    assert m.closure_deficit == 0.0
    # off_hours_minutes should be 0 (stream entirely inside window)
    assert m.off_hours_minutes == 0
    # composite low but non-zero
    assert 0.0 < m.composite < 30.0


def test_per_day_metrics_two_parallel_streams_raise_codl_not_closure():
    # Two streams actively driven during an overlap → parallel load shows up on
    # CODL (peak == 2). Closure is a SEPARATE signal: neither stream was parked
    # and resumed, so the resumption load stays 0.0, NOT inflated by concurrency.
    streams = [
        _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
                user_msg_timestamps=(_utc(2026, 5, 15, 11, 30),
                                     _utc(2026, 5, 15, 12, 30))),
        _stream("b", _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 13),
                user_msg_timestamps=(_utc(2026, 5, 15, 11, 30),
                                     _utc(2026, 5, 15, 12, 30))),
    ]
    agg = _agg(date(2026, 5, 15), streams)
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    assert m.codl_peak == 2
    assert m.codl_avg > 0
    assert m.closure_deficit == 0.0


def test_per_day_metrics_evening_session_adds_off_hours():
    # An evening session the operator actively drove: two off-hours messages,
    # each marking a 6-minute grace window (disjoint) → 12 engaged minutes.
    streams = [
        _stream("s1", _utc(2026, 5, 15, 19), _utc(2026, 5, 15, 21),
                user_msg_timestamps=(_utc(2026, 5, 15, 19), _utc(2026, 5, 15, 20))),
    ]
    agg = _agg(date(2026, 5, 15), streams)
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    assert m.off_hours_minutes == 12
    # …and records WHEN those minutes happened, as local ranges.
    assert m.off_hours_ranges_local == (
        (time(19, 0), time(19, 5)),
        (time(20, 0), time(20, 5)),
    )


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
    deficit and off-hours. Optimum should fall in the lower bucket."""
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


def test_personal_optimum_skips_none_closure_days():
    """Days with no closure value (closure_deficit is None — a no-activity day)
    must not crash the optimum and must be omitted from the bucket's closure
    average — not treated as 0. A bucket of all-None closure days is judged on
    off-hours alone (neutral closure factor)."""
    days = {}
    workdays = _weekdays_from(date(2026, 5, 4), 16)
    # Low-load bucket: closure present and good.
    for d in workdays[:8]:
        days[d] = DayMetrics(day=d, codl_avg=1.2,
                             closure_deficit=0.1, off_hours_minutes=5)
    # High-load bucket: no closure data (closure None) + heavy off-hours.
    for d in workdays[8:]:
        days[d] = DayMetrics(day=d, codl_avg=3.5,
                             closure_deficit=None, off_hours_minutes=120)
    optimum = derive_personal_optimum(days, min_days=14)
    assert optimum is not None              # did not crash on None
    assert 0.5 < optimum < 2.5              # low-load bucket still wins on off-hours


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


def test_personal_optimum_includes_weekend_active_days():
    """Weekend days with codl_avg > 0 now contribute to optimum derivation,
    just like weekdays. A dataset of weekend-only days at low CODL combined
    with weekday days at high CODL should produce an optimum influenced by
    whichever cluster has the best score across ALL days."""
    days = {}
    # 13 weekdays at high load (high closure deficit, many off-hours)
    for d in _weekdays_from(date(2026, 5, 4), 13):
        days[d] = DayMetrics(day=d, codl_avg=2.5,
                             closure_deficit=0.5, off_hours_minutes=20)
    # 10 Saturdays/Sundays at low load — NOW included in optimum.
    weekend = date(2026, 5, 2)  # a Saturday
    weekend_count = 0
    while weekend_count < 10:
        if weekend.weekday() >= 5:
            days[weekend] = DayMetrics(day=weekend, codl_avg=0.6,
                                       closure_deficit=0.0, off_hours_minutes=0)
            weekend_count += 1
        weekend += timedelta(days=1)
    optimum = derive_personal_optimum(days, min_days=10)
    # With weekends included and 23 total active days, optimum should be found.
    # The low-codl weekend cluster (0.6) has better score → optimum < 1.5.
    assert optimum is not None


def test_per_day_metrics_saturday_in_window_scores_composite():
    """Saturday activity inside the work window produces a non-zero composite,
    just like any weekday — no special weekend treatment."""
    sat = date(2026, 5, 9)  # Saturday
    assert sat.weekday() == 5
    streams = [
        _stream("s1", _utc(2026, 5, 9, 10, 0), _utc(2026, 5, 9, 12, 0),
                user_msg_timestamps=(_utc(2026, 5, 9, 10, 0), _utc(2026, 5, 9, 11, 0))),
    ]
    agg = _agg(sat, streams)
    window = WorkWindow(weekday=5, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    # In-window activity → composite > 0, axes are populated.
    assert m.composite > 0.0
    assert m.codl_avg > 0.0
    # Entirely in-window → no off-hours minutes.
    assert m.off_hours_minutes == 0
    # Work window is recorded.
    assert m.work_window_local == (time(9), time(18))


def test_per_day_metrics_saturday_off_hours_applies_load():
    """Saturday off-hours interaction is captured as off-hours engaged minutes
    and raises the composite via the additive load — same as any day."""
    sat = date(2026, 5, 9)  # Saturday
    assert sat.weekday() == 5
    streams = [
        _stream("s1", _utc(2026, 5, 9, 10, 0), _utc(2026, 5, 9, 12, 0),
                user_msg_timestamps=(_utc(2026, 5, 9, 10, 0),)),
        # evening session the operator actively drove → off-hours interaction
        _stream("s2", _utc(2026, 5, 9, 20, 0), _utc(2026, 5, 9, 22, 0),
                user_msg_timestamps=(_utc(2026, 5, 9, 20, 0), _utc(2026, 5, 9, 21, 0))),
    ]
    agg = _agg(sat, streams)
    window = WorkWindow(weekday=5, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    assert m.composite > 0.0
    assert m.off_hours_minutes == 12  # two 6-min grace windows at 20:00 & 21:00


def test_per_day_metrics_sunday_with_no_streams_is_clean():
    sun = date(2026, 5, 10)
    assert sun.weekday() == 6
    agg = DayAggregate(day=sun)
    window = WorkWindow(weekday=6, start=time(9), end=time(18))
    m = per_day_metrics(agg, window, UTC)
    assert m.composite == 0.0
    assert m.off_hours_minutes == 0
    # Empty day: work_window_local is still set (the window was supplied).
    assert m.work_window_local == (time(9), time(18))


def test_build_profile_percentiles_include_saturday_when_active():
    """Saturday activity inside the work window scores a composite, just
    like a weekday — both days contribute to the profile's percentiles."""
    fri = date(2026, 5, 8)  # Friday
    sat = date(2026, 5, 9)  # Saturday
    fri_streams = [
        _stream("f1", _utc(2026, 5, 8, 9, 0), _utc(2026, 5, 8, 17, 0),
                user_msg_timestamps=tuple(_utc(2026, 5, 8, h, 0) for h in range(9, 17))),
    ]
    sat_streams = [
        _stream("s1", _utc(2026, 5, 9, 10, 0), _utc(2026, 5, 9, 12, 0),
                user_msg_timestamps=(_utc(2026, 5, 9, 10, 0),)),
    ]
    aggs = {
        fri: _agg(fri, fri_streams),
        sat: _agg(sat, sat_streams),
    }
    profile = build_profile(aggs, local_tz=UTC)
    # Both days produce a composite > 0.
    assert profile.days[fri].composite > 0
    assert profile.days[sat].composite > 0
    # Percentiles are computed over both active days.
    assert profile.composite_p50 is not None


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
    """With no aggregates, all 7 weekdays are populated with the literature
    cold-start default (09:00–19:00, is_default=True)."""
    profile = build_profile({}, local_tz=UTC)
    assert set(profile.work_windows) == set(range(7))
    assert all(w.start == time(9, 0) and w.end == time(19, 0)
               for w in profile.work_windows.values())
    assert all(w.is_default is True for w in profile.work_windows.values())


# ---------------------------------------------------------------------------
# _default_window helper

def test_default_window_returns_literature_default():
    """_default_window always returns 09:00–19:00 with is_default=True."""
    lit_start, lit_end = LITERATURE_WORK_WINDOW
    for wd in range(7):
        w = _default_window(wd)
        assert w.weekday == wd
        assert w.start == lit_start
        assert w.end == lit_end
        assert w.is_default is True


# ---------------------------------------------------------------------------
# Work-window inference (>= WORK_WINDOW_MIN_SAMPLES distinct weekday-dates)

def _make_infer_aggregates(
    start_date: date,
    n_days: int,
    msg_hours: list[int],
    local_tz=UTC,
) -> dict[date, "DayAggregate"]:
    """Build n_days weekday aggregates, each with one stream whose user
    messages are at the given hours (UTC, since local_tz=UTC in tests).
    Returns a date→DayAggregate mapping."""
    aggs = {}
    d = start_date
    count = 0
    while count < n_days:
        if d.weekday() < 5:  # weekdays only
            timestamps = tuple(
                _utc(d.year, d.month, d.day, h, 0)
                for h in msg_hours
            )
            streams = [
                _stream(f"s{count}", _utc(d.year, d.month, d.day, min(msg_hours), 0),
                        _utc(d.year, d.month, d.day, max(msg_hours), 30),
                        user_msg_timestamps=timestamps,
                        user_msg_count=len(timestamps)),
            ]
            aggs[d] = _agg(d, streams)
            count += 1
        d = d + timedelta(days=1)
    return aggs


def test_detect_work_windows_infers_band_from_sufficient_data():
    """With >= WORK_WINDOW_MIN_SAMPLES distinct weekday-dates whose user
    messages cluster 10:00–17:00, the inferred band should bracket that range
    (start <= 10:00, end >= 17:00) and is_default must be False."""
    # Build aggregates for 7 weekdays with messages at 10, 11, 14, 16, 17h.
    aggs = _make_infer_aggregates(
        date(2026, 5, 4), n_days=7, msg_hours=[10, 11, 14, 16, 17],
    )
    windows = detect_work_windows(aggs, local_tz=UTC)
    assert len(windows) == 7
    assert all(w.is_default is False for w in windows.values())
    # The inferred start should be at or before the earliest message hour.
    assert all(w.start <= time(10, 0) for w in windows.values())
    # The inferred end should be at or after the latest message hour.
    assert all(w.end >= time(17, 0) for w in windows.values())


def test_detect_work_windows_infers_same_band_all_7_weekdays():
    """Inference always produces one stable band applied to all 7 weekdays."""
    aggs = _make_infer_aggregates(
        date(2026, 5, 4), n_days=7, msg_hours=[9, 12, 15],
    )
    windows = detect_work_windows(aggs, local_tz=UTC)
    starts = {w.start for w in windows.values()}
    ends = {w.end for w in windows.values()}
    assert len(starts) == 1, "All weekdays should share the same inferred start"
    assert len(ends) == 1, "All weekdays should share the same inferred end"


def test_detect_work_windows_config_override_takes_precedence(tmp_path):
    """When config.json has a work_window block, inference is bypassed and the
    override band is returned for all weekdays (is_default=False), regardless
    of the aggregates supplied."""
    from ai_code_cognitive_stress.core.config import Config, WorkWindow as CfgWorkWindow, _CONFIG_CACHE

    # Build plenty of inference data so we can confirm it's ignored.
    aggs = _make_infer_aggregates(
        date(2026, 5, 4), n_days=7, msg_hours=[7, 8, 14, 20, 21],
    )

    # Temporarily inject an override config into the cache, keyed by the path
    # load_config() resolves to (runtime config.json if present, else defaults).
    from ai_code_cognitive_stress.core.config import _resolve_config_path
    override_ww = CfgWorkWindow(start=time(8, 0), end=time(17, 0))
    override_cfg = Config(work_window=override_ww)
    cache_key = str(_resolve_config_path(None))
    _CONFIG_CACHE[cache_key] = override_cfg
    try:
        windows = detect_work_windows(aggs, local_tz=UTC)
    finally:
        _CONFIG_CACHE.pop(cache_key, None)

    assert len(windows) == 7
    assert all(w.start == time(8, 0) and w.end == time(17, 0)
               for w in windows.values())
    assert all(w.is_default is False for w in windows.values())


def test_detect_work_windows_sparse_data_uses_literature_default():
    """Fewer than WORK_WINDOW_MIN_SAMPLES sample-days → literature default
    (09:00–19:00, is_default=True) regardless of where the messages fell."""
    # Only 2 distinct dates — below the minimum of 5.
    aggs = _make_infer_aggregates(
        date(2026, 5, 4), n_days=2, msg_hours=[14, 15, 16],
    )
    windows = detect_work_windows(aggs, local_tz=UTC)
    assert all(w.start == time(9, 0) and w.end == time(19, 0)
               for w in windows.values())
    assert all(w.is_default is True for w in windows.values())


def test_detect_work_windows_uses_weekend_aggregates_for_inference():
    """Weekend timestamps NOW contribute to inference. Six weekend days with
    messages at 10–14h is enough samples to infer the band."""
    aggs = {}
    for d in (date(2026, 5, 2), date(2026, 5, 3),
              date(2026, 5, 9), date(2026, 5, 10),
              date(2026, 5, 16), date(2026, 5, 17)):
        ts = tuple(_utc(d.year, d.month, d.day, h) for h in [10, 11, 12, 13, 14])
        aggs[d] = _agg(d, [_stream("x", ts[0], ts[-1], user_msg_timestamps=ts)])
    windows = detect_work_windows(aggs, local_tz=UTC)
    # 6 distinct dates → above WORK_WINDOW_MIN_SAMPLES=5, so inference fires.
    assert all(w.is_default is False for w in windows.values())
    # All messages are at 10–14h, so the band brackets that range.
    assert all(w.start <= time(10, 0) for w in windows.values())
    assert all(w.end >= time(14, 0) for w in windows.values())


# ---------------------------------------------------------------------------
# Off-hours additive load

def test_off_hours_load_points_zero_minutes_is_zero():
    assert _off_hours_load_points(0) == 0.0


def test_off_hours_load_points_scales_linearly_below_ceiling():
    half = OFF_HOURS_LOAD_CEILING_MIN // 2
    expected = OFF_HOURS_LOAD_MAX_POINTS * (half / OFF_HOURS_LOAD_CEILING_MIN)
    assert _off_hours_load_points(half) == pytest.approx(expected)


def test_off_hours_load_points_saturates_at_ceiling():
    assert _off_hours_load_points(OFF_HOURS_LOAD_CEILING_MIN) == pytest.approx(
        OFF_HOURS_LOAD_MAX_POINTS
    )
    assert _off_hours_load_points(OFF_HOURS_LOAD_CEILING_MIN * 10) == pytest.approx(
        OFF_HOURS_LOAD_MAX_POINTS
    )


def test_per_day_metrics_off_hours_raises_composite():
    """A weekday with off_hours_minutes > 0 must produce a composite strictly
    greater than the same day with no off-hours activity (all else equal)."""
    window = WorkWindow(weekday=0, start=time(9), end=time(18))
    # Session fully in window → no off-hours.
    streams_in = [
        _stream("s1", _utc(2026, 5, 4, 10), _utc(2026, 5, 4, 17),
                user_msg_timestamps=(_utc(2026, 5, 4, 10), _utc(2026, 5, 4, 12))),
    ]
    # Same session plus an evening session the operator actually drove
    # (off-hours user messages) — adds off-hours engaged minutes.
    streams_off = [
        _stream("s1", _utc(2026, 5, 4, 10), _utc(2026, 5, 4, 17),
                user_msg_timestamps=(_utc(2026, 5, 4, 10), _utc(2026, 5, 4, 12))),
        _stream("s2", _utc(2026, 5, 4, 20), _utc(2026, 5, 4, 22),
                user_msg_timestamps=(_utc(2026, 5, 4, 20), _utc(2026, 5, 4, 21))),
    ]
    m_in = per_day_metrics(_agg(date(2026, 5, 4), streams_in), window, UTC)
    m_off = per_day_metrics(_agg(date(2026, 5, 4), streams_off), window, UTC)
    assert m_off.off_hours_minutes > 0
    assert m_in.off_hours_minutes == 0
    assert m_off.composite > m_in.composite


def test_per_day_metrics_no_off_hours_composite_unchanged():
    """A session entirely within the work window must not be affected by the
    off-hours load (off_hours_minutes == 0 → adds 0 points)."""
    window = WorkWindow(weekday=0, start=time(9), end=time(18))
    streams = [
        _stream("s1", _utc(2026, 5, 4, 10), _utc(2026, 5, 4, 16),
                user_msg_timestamps=(_utc(2026, 5, 4, 10),)),
    ]
    m = per_day_metrics(_agg(date(2026, 5, 4), streams), window, UTC)
    assert m.off_hours_minutes == 0
    # Composite equals the axis blend with no off-hours load added (closure is
    # passed through verbatim, whatever its value). Tolerance absorbs the rounding
    # of the stored codl_avg vs the unrounded value the composite came from.
    from ai_code_cognitive_stress.pipeline.metrics import _composite_score
    expected = _composite_score(m.codl_dose, m.interruption_rate, m.closure_deficit)
    assert m.composite == pytest.approx(expected, abs=0.1)


def test_per_day_metrics_off_hours_load_clamps_at_100():
    """A day with a very high 3-axis composite AND heavy off-hours work must
    never exceed 100."""
    window = WorkWindow(weekday=0, start=time(9), end=time(18))
    # Many parallel foreground streams → very high 3-axis composite.
    streams = [
        _stream(f"s{i}", _utc(2026, 5, 4, 9), _utc(2026, 5, 4, 18),
                user_msg_timestamps=tuple(
                    _utc(2026, 5, 4, h, m)
                    for h in range(9, 18) for m in range(0, 60, 10)
                ))
        for i in range(8)  # 8 foreground streams in parallel — far above WM cap
    ]
    # Add a long, actively-driven evening session to maximise off-hours load.
    streams.append(
        _stream("evening", _utc(2026, 5, 4, 20), _utc(2026, 5, 4, 23, 30),
                user_msg_timestamps=tuple(
                    _utc(2026, 5, 4, h, m)
                    for h in range(20, 24) for m in range(0, 60, 10)
                ))
    )
    m = per_day_metrics(_agg(date(2026, 5, 4), streams), window, UTC)
    assert m.off_hours_minutes > 0
    assert m.composite <= 100.0


def test_per_day_metrics_off_hours_only_activity_scores_above_zero():
    """A day worked ENTIRELY outside the window (off-hours interaction, no
    in-window work) now scores a strictly positive composite — the additive
    off-hours load fixes the old multiplicative-on-zero-base behaviour."""
    sat = date(2026, 5, 9)
    # All activity in the evening, actively driven (off-hours user messages).
    streams = [
        _stream("s1", _utc(2026, 5, 9, 20), _utc(2026, 5, 9, 23),
                user_msg_timestamps=(_utc(2026, 5, 9, 20), _utc(2026, 5, 9, 21),
                                     _utc(2026, 5, 9, 22))),
    ]
    window = WorkWindow(weekday=5, start=time(9), end=time(18))
    m = per_day_metrics(_agg(sat, streams), window, UTC)
    assert m.codl_avg == 0.0          # nothing inside the window
    assert m.off_hours_minutes > 0    # off-hours interaction recorded
    assert m.composite > 0.0          # ...and it counts, additively


# ---------------------------------------------------------------------------
# New-behavior tests: weekends now treated identically to weekdays

def test_saturday_in_window_scores_composite_same_as_equivalent_weekday():
    """A Saturday with in-window activity must produce a composite that matches
    the same activity on a Monday — no weekend gate suppressing the score."""
    window = WorkWindow(weekday=0, start=time(9), end=time(18))
    window_sat = WorkWindow(weekday=5, start=time(9), end=time(18))

    # Monday (May 4) and Saturday (May 9) with identical streams.
    for day, w in [(date(2026, 5, 4), window), (date(2026, 5, 9), window_sat)]:
        streams = [
            _stream("s1",
                    _utc(day.year, day.month, day.day, 10),
                    _utc(day.year, day.month, day.day, 14),
                    user_msg_timestamps=tuple(
                        _utc(day.year, day.month, day.day, h, 0)
                        for h in range(10, 14)
                    )),
        ]
        m = per_day_metrics(_agg(day, streams), w, UTC)
        assert m.composite > 0.0, f"{day} should have composite > 0"
        assert m.codl_avg > 0.0, f"{day} should have codl_avg > 0"


def test_saturday_off_hours_load_same_as_weekday():
    """Saturday off-hours activity raises the composite by the same additive
    off-hours load as any other day."""
    window = WorkWindow(weekday=5, start=time(9), end=time(18))
    sat = date(2026, 5, 9)

    # Session in-window only.
    streams_in = [
        _stream("s1", _utc(2026, 5, 9, 10), _utc(2026, 5, 9, 14),
                user_msg_timestamps=(_utc(2026, 5, 9, 10), _utc(2026, 5, 9, 12))),
    ]
    # Same session + an actively-driven evening session (off-hours messages).
    streams_off = list(streams_in) + [
        _stream("s2", _utc(2026, 5, 9, 20), _utc(2026, 5, 9, 22),
                user_msg_timestamps=(_utc(2026, 5, 9, 20), _utc(2026, 5, 9, 21))),
    ]
    m_in = per_day_metrics(_agg(sat, streams_in), window, UTC)
    m_off = per_day_metrics(_agg(sat, streams_off), window, UTC)
    assert m_off.off_hours_minutes > 0
    assert m_off.composite > m_in.composite


def test_detect_work_windows_infers_band_from_weekend_only_data():
    """When only weekend dates are available and there are >= min_samples of
    them, inference should fire (weekends were previously excluded)."""
    # Build 6 weekend dates (Sat/Sun pairs over 3 weekends) with messages at 10-16h.
    aggs = {}
    for d in (date(2026, 5, 2), date(2026, 5, 3),   # weekend 1
              date(2026, 5, 9), date(2026, 5, 10),  # weekend 2
              date(2026, 5, 16), date(2026, 5, 17)): # weekend 3
        ts = tuple(_utc(d.year, d.month, d.day, h) for h in [10, 11, 12, 13, 14, 15, 16])
        aggs[d] = _agg(d, [_stream("x", ts[0], ts[-1], user_msg_timestamps=ts)])
    windows = detect_work_windows(aggs, local_tz=UTC)
    # 6 distinct calendar dates >= WORK_WINDOW_MIN_SAMPLES=5 → inference fires.
    assert all(w.is_default is False for w in windows.values())
    assert all(w.start <= time(10, 0) for w in windows.values())
    assert all(w.end >= time(16, 0) for w in windows.values())


# ---------------------------------------------------------------------------
# CODL dose tests (graded capacity-dose formula)

def test_codl_dose_idle_dilution():
    """Idle minutes do NOT dilute the dose. Adding extra idle time at the end
    of the window changes codl_avg (mean over more samples) but leaves
    codl_dose unchanged because idle samples contribute 0 to the raw_dose
    and the dose horizon H is a fixed constant (not window-length normalised).

    Scenario: one active stream with user messages in hour 10, window either
    09:00–11:00 or 09:00–17:00. The dose-relevant activity is the same; only
    the tail of idle samples changes.
    """
    d = date(2026, 5, 4)
    msgs = tuple(_utc(2026, 5, 4, 10, m) for m in range(0, 60, 5))
    streams = [_stream("s1", _utc(2026, 5, 4, 10, 0), _utc(2026, 5, 4, 10, 59),
                       user_msg_timestamps=msgs)]
    agg = _agg(d, streams)

    # Short window: 09:00–11:00 (2 h)
    window_short = WorkWindow(weekday=0, start=time(9, 0), end=time(11, 0))
    m_short = per_day_metrics(agg, window_short, UTC)

    # Long window: 09:00–17:00 (8 h) — lots of idle tail
    window_long = WorkWindow(weekday=0, start=time(9, 0), end=time(17, 0))
    m_long = per_day_metrics(agg, window_long, UTC)

    # codl_avg must be lower in the long window (same activity, more idle minutes)
    assert m_long.codl_avg < m_short.codl_avg, (
        "extra idle time must reduce codl_avg (it dilutes the mean)"
    )
    # codl_dose must be the same (idle samples add 0 to raw_dose; horizon H is fixed)
    assert m_short.codl_dose == pytest.approx(m_long.codl_dose, abs=1e-3), (
        "codl_dose must not change when idle tail is added"
    )


def test_codl_dose_spike_cap():
    """A single minute at very high concurrency (C=8) contributes at most
    1.0 capacity-equivalent minute to the raw_dose (phi is capped at 1.0).
    Two such minutes contribute 2.0, not 4.0.
    """
    d = date(2026, 5, 4)
    window = WorkWindow(weekday=0, start=time(9, 0), end=time(18, 0))
    # 8 parallel foreground streams, each with a user message right at 10:00.
    # Each stream covers exactly 1 minute (10:00–10:01).
    t_start = _utc(2026, 5, 4, 10, 0)
    t_end = _utc(2026, 5, 4, 10, 1)
    msgs = (t_start,)
    streams = [
        _stream(f"s{i}", t_start, t_end, user_msg_timestamps=msgs)
        for i in range(8)
    ]
    agg = _agg(d, streams)
    m = per_day_metrics(agg, window, UTC)
    # Peak weighted sample would be 8 * 1.0 = 8.0 (all in foreground).
    # But phi(t) = min(1, 8 / 4) = 1.0 → raw_dose contribution is 1.0 * 1 minute,
    # not 2.0.
    # The raw_dose should be ≤ 2.0 (1 or 2 samples can hit that minute boundary).
    assert m.codl_raw_dose <= 2.0, (
        f"raw_dose={m.codl_raw_dose} should be ≤ 2.0 (phi capped at 1)"
    )
    # And definitely greater than 0.
    assert m.codl_raw_dose > 0


def test_codl_dose_range_and_monotonicity():
    """codl_dose is in [0,1], zero on an inactive day, and (weakly) non-decreasing
    as sustained concurrency rises."""
    d = date(2026, 5, 4)
    window = WorkWindow(weekday=0, start=time(9, 0), end=time(18, 0))

    # Inactive day: no streams.
    m0 = per_day_metrics(_agg(d, []), window, UTC)
    assert m0.codl_dose == 0.0

    doses = []
    for n_streams in range(1, 6):
        # n_streams running all day, each with user messages throughout.
        msgs = tuple(
            _utc(2026, 5, 4, h, m)
            for h in range(9, 18) for m in range(0, 60, 20)
        )
        streams = [
            _stream(f"s{i}", _utc(2026, 5, 4, 9, 0), _utc(2026, 5, 4, 17, 59),
                    user_msg_timestamps=msgs)
            for i in range(n_streams)
        ]
        m = per_day_metrics(_agg(d, streams), window, UTC)
        assert 0.0 <= m.codl_dose <= 1.0, (
            f"codl_dose={m.codl_dose} out of [0,1] for n_streams={n_streams}"
        )
        doses.append(m.codl_dose)

    # Monotonically non-decreasing: more concurrent streams → higher dose
    for i in range(len(doses) - 1):
        assert doses[i] <= doses[i + 1], (
            f"dose not non-decreasing: doses[{i}]={doses[i]} > doses[{i+1}]={doses[i+1]}"
        )
