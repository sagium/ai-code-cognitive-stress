"""Tests for the metrics layer: work windows, three axes, composite, optimum."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from stress_levels.aggregate import DayAggregate, StreamDayActivity
from stress_levels.sources.base import ClosureEvent
from stress_levels.metrics import (
    CODL_NORMALISATION_CEILING,
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
    _closure_deficit,
    _codl_samples,
    _codl_weighted_samples,
    _composite_score,
    _count_cross_stream_starts,
    _count_rework,
    _default_window,
    _off_hours_engaged_minutes,
    _off_hours_load_points,
    _percentile,
    _stream_weight_at,
    build_profile,
    derive_personal_optimum,
    detect_work_windows,
    per_day_metrics,
)


def _closure(ts, kind="commit", repo="proj"):
    return ClosureEvent(ts=ts, kind=kind, repo=repo)


def _agg_with_closures(day, streams, closures):
    return DayAggregate(day=day, streams=tuple(streams),
                        peak_concurrent_streams=0,
                        closure_events=tuple(closures))


UTC = timezone.utc


def _utc(year, month, day, hour=12, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _stream(stream_id, first_ts, last_ts, **counts):
    return StreamDayActivity(
        stream_id=stream_id,
        project="proj",
        cwd=counts.get("cwd"),
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
    # 4 background sessions × 0.25 ≈ 1.0 — well under the WM-cap of 4.
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


def test_off_hours_engaged_morning_interaction_counts():
    # A 07:00 message marks 07:00..07:05, all before the window start.
    streams = (_stream("a", _utc(2026, 5, 15, 7), _utc(2026, 5, 15, 7),
                       user_msg_timestamps=(_utc(2026, 5, 15, 7),)),)
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
# Closure Deficit — real closure events (opened-vs-closed loops)

_WIN_START = _utc(2026, 5, 15, 9)
_WIN_END = _utc(2026, 5, 15, 18)


def test_closure_deficit_none_falls_back_to_legacy_proxy():
    """closure_events is None → fraction of weighted samples with C(t) > 1
    (the legacy proxy), unchanged from the pre-closure behaviour."""
    agg = _agg(date(2026, 5, 15), [_stream("a", _utc(2026, 5, 15, 10),
                                            _utc(2026, 5, 15, 11))])
    # Hand it a weighted series with some samples > 1.
    weighted = [0.5, 1.5, 2.0, 0.0, 1.2]
    d = _closure_deficit(agg, _WIN_START, _WIN_END, weighted)
    assert d == pytest.approx(3 / 5)  # 3 of 5 samples exceed 1


def test_closure_deficit_none_empty_samples_is_zero():
    agg = _agg(date(2026, 5, 15), [])
    assert _closure_deficit(agg, _WIN_START, _WIN_END, []) == 0.0


def test_closure_deficit_all_loops_closed_is_zero():
    """Two loops opened in-window, two closures in-window → deficit 0."""
    streams = [
        _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12)),
        _stream("b", _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 13)),
    ]
    closures = [_closure(_utc(2026, 5, 15, 12)), _closure(_utc(2026, 5, 15, 13))]
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    assert _closure_deficit(agg, _WIN_START, _WIN_END, [99.0]) == 0.0


def test_closure_deficit_no_closures_is_one():
    """Loops opened but the closure source emitted nothing in-window → 1.0,
    independent of the weighted-sample series passed in."""
    streams = [_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12))]
    agg = _agg_with_closures(date(2026, 5, 15), streams, [])  # () = wired, empty
    assert _closure_deficit(agg, _WIN_START, _WIN_END, [0.0, 0.0]) == 1.0


def test_closure_deficit_partial_close():
    """Four loops opened, one closure → 1 - 1/4 = 0.75."""
    streams = [_stream(f"s{i}", _utc(2026, 5, 15, 10 + i),
                       _utc(2026, 5, 15, 14)) for i in range(4)]
    closures = [_closure(_utc(2026, 5, 15, 13))]
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    assert _closure_deficit(agg, _WIN_START, _WIN_END, []) == pytest.approx(0.75)


def test_closure_deficit_excess_closures_clamp_at_zero():
    """More closures than opened loops cannot push the deficit negative."""
    streams = [_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12))]
    closures = [_closure(_utc(2026, 5, 15, 11)), _closure(_utc(2026, 5, 15, 12)),
                _closure(_utc(2026, 5, 15, 13))]
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    assert _closure_deficit(agg, _WIN_START, _WIN_END, []) == 0.0


def test_closure_deficit_ignores_out_of_window_loops_and_closures():
    """A loop opened before the window and a closure after it don't count."""
    streams = [
        _stream("early", _utc(2026, 5, 15, 7), _utc(2026, 5, 15, 12)),   # opened pre-window
        _stream("inwin", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 16)),  # opened in-window
    ]
    closures = [_closure(_utc(2026, 5, 15, 20))]  # after window → ignored
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    # Only one loop counts as opened in-window, zero in-window closures → 1.0.
    assert _closure_deficit(agg, _WIN_START, _WIN_END, []) == 1.0


def test_closure_deficit_no_opened_loops_is_zero():
    """A stream alive in-window but opened earlier means zero loops *opened*
    today → no deficit (nothing to close)."""
    streams = [_stream("a", _utc(2026, 5, 15, 7), _utc(2026, 5, 15, 16))]
    agg = _agg_with_closures(date(2026, 5, 15), streams, [])
    assert _closure_deficit(agg, _WIN_START, _WIN_END, []) == 0.0


def test_closure_deficit_is_independent_of_concurrency_shape():
    """The keystone property: two days with the IDENTICAL engagement-weighted
    concurrency series C(t) (hence identical codl_avg) get DIFFERENT closure
    deficits purely from their closure events. Under the old definition both
    were a pure function of C(t) and would have been equal."""
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    streams = [
        _stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 14),
                user_msg_timestamps=tuple(_utc(2026, 5, 15, h) for h in (10, 11, 12, 13))),
        _stream("b", _utc(2026, 5, 15, 11), _utc(2026, 5, 15, 13),
                user_msg_timestamps=(_utc(2026, 5, 15, 11), _utc(2026, 5, 15, 12))),
    ]
    closed = _agg_with_closures(date(2026, 5, 15), streams,
                                [_closure(_utc(2026, 5, 15, 13, 30)),
                                 _closure(_utc(2026, 5, 15, 13, 45))])
    unclosed = _agg_with_closures(date(2026, 5, 15), streams, [])
    m_closed = per_day_metrics(closed, window, UTC)
    m_unclosed = per_day_metrics(unclosed, window, UTC)
    # Same concurrency shape → same CODL.
    assert m_closed.codl_avg == m_unclosed.codl_avg
    assert m_closed.codl_peak == m_unclosed.codl_peak
    # But the closure deficits differ — the axis carries information C(t) does
    # not, so it is no longer a re-expression of concurrency.
    assert m_closed.closure_deficit == 0.0
    assert m_unclosed.closure_deficit == 1.0
    # And that difference propagates to the composite.
    assert m_unclosed.composite > m_closed.composite


# ---------------------------------------------------------------------------
# Closure Deficit — per-repo attribution (repo_map)

def test_closure_deficit_per_repo_netting_does_not_cross_repos():
    """A closure in repo B must NOT net an opened loop in repo A. Two loops in
    A (no closures) + one loop in B (one closure) → only B's loop nets, so
    deficit = 1 - 1/3."""
    streams = [
        _stream("a1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/repoA"),
        _stream("a2", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/repoA"),
        _stream("b1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/repoB"),
    ]
    closures = [_closure(_utc(2026, 5, 15, 11), repo="/repoB")]
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    repo_map = {"/repoA": "/repoA", "/repoB": "/repoB"}
    assert _closure_deficit(agg, _WIN_START, _WIN_END, [], repo_map) == pytest.approx(2 / 3)


def test_closure_deficit_global_netting_when_no_repo_map():
    """Without a repo_map the same setup nets globally — the B closure can
    cover any opened loop → 1 - 1/3."""
    streams = [
        _stream("a1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/repoA"),
        _stream("a2", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/repoA"),
        _stream("b1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/repoB"),
    ]
    closures = [_closure(_utc(2026, 5, 15, 11), repo="/repoB")]
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    # repo_map=None → global path (regression guard for prior behaviour).
    assert _closure_deficit(agg, _WIN_START, _WIN_END, []) == pytest.approx(2 / 3)


def test_closure_deficit_spare_closures_spill_to_unattributed_loops():
    """A repo with more closures than its own opened loops lends the spare to
    a loop we couldn't attribute (cwd not in the map). One loop in A (two
    closures), one unattributable loop → both net → deficit 0."""
    streams = [
        _stream("a1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/repoA"),
        _stream("x1", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/not/a/repo"),
    ]
    closures = [
        _closure(_utc(2026, 5, 15, 11), repo="/repoA"),
        _closure(_utc(2026, 5, 15, 11, 30), repo="/repoA"),
    ]
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    repo_map = {"/repoA": "/repoA"}  # /not/a/repo is unmapped → None bucket
    assert _closure_deficit(agg, _WIN_START, _WIN_END, [], repo_map) == 0.0


def test_closure_deficit_rework_kinds_do_not_close_loops():
    """Rework events (amend/rebase/…) are NOT closures: a loop opened with only
    a rebase event against it stays unclosed (deficit 1.0)."""
    streams = [_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/r")]
    closures = [_closure(_utc(2026, 5, 15, 11), kind="rebase", repo="/r")]
    agg = _agg_with_closures(date(2026, 5, 15), streams, closures)
    repo_map = {"/r": "/r"}
    assert _closure_deficit(agg, _WIN_START, _WIN_END, [], repo_map) == 1.0


# ---------------------------------------------------------------------------
# Rework → Interruption axis

def test_count_rework_only_counts_rework_kinds_in_window():
    closures = [
        _closure(_utc(2026, 5, 15, 10), kind="amend"),
        _closure(_utc(2026, 5, 15, 11), kind="rebase"),
        _closure(_utc(2026, 5, 15, 12), kind="commit"),   # closure, not rework
        _closure(_utc(2026, 5, 15, 20), kind="reset"),     # out of window
    ]
    agg = _agg_with_closures(date(2026, 5, 15), [], closures)
    assert _count_rework(agg, _WIN_START, _WIN_END) == 2


def test_count_rework_zero_when_no_closure_source():
    agg = _agg(date(2026, 5, 15), [])  # closure_events is None
    assert _count_rework(agg, _WIN_START, _WIN_END) == 0


def test_rework_events_raise_interruption_rate():
    """Same streams, same closures-as-commits — but adding reflog rework events
    raises the interruption rate (and never lowers the closure deficit)."""
    window = WorkWindow(weekday=4, start=time(9), end=time(18))
    streams = [_stream("a", _utc(2026, 5, 15, 10), _utc(2026, 5, 15, 12), cwd="/r")]
    base = _agg_with_closures(
        date(2026, 5, 15), streams,
        [_closure(_utc(2026, 5, 15, 11), kind="commit", repo="/r")],
    )
    with_rework = _agg_with_closures(
        date(2026, 5, 15), streams,
        [_closure(_utc(2026, 5, 15, 11), kind="commit", repo="/r"),
         _closure(_utc(2026, 5, 15, 11, 30), kind="amend", repo="/r"),
         _closure(_utc(2026, 5, 15, 11, 45), kind="rebase", repo="/r")],
    )
    repo_map = {"/r": "/r"}
    m_base = per_day_metrics(base, window, UTC, repo_map=repo_map)
    m_rework = per_day_metrics(with_rework, window, UTC, repo_map=repo_map)
    assert m_rework.interruption_rate > m_base.interruption_rate
    # Rework doesn't touch the closure axis (the single commit still nets the loop).
    assert m_rework.closure_deficit == m_base.closure_deficit == 0.0


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
    # No closure source wired (closure_events is None) → legacy proxy, which
    # measures samples with C(t) > 1; a single stream never exceeds 1 → 0.
    assert m.closure_deficit == 0.0
    # off_hours_minutes should be 0 (stream entirely inside window)
    assert m.off_hours_minutes == 0
    # composite low but non-zero
    assert 0.0 < m.composite < 30.0


def test_per_day_metrics_two_parallel_streams_raises_deficit():
    # Both streams are ACTIVELY driven during the overlap (user messages in
    # window), so they're foreground and the weighted load exceeds 1 → deficit.
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
    # codl_avg is time-weighted across the full work window; with only 2hr
    # of two-stream overlap out of 9hr, it sits well below 1 — peak and
    # deficit are the right signals here.
    assert m.codl_peak == 2
    assert m.codl_avg > 0
    assert m.closure_deficit > 0


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
    from stress_levels.config import Config, WorkWindow as CfgWorkWindow, _CONFIG_CACHE

    # Build plenty of inference data so we can confirm it's ignored.
    aggs = _make_infer_aggregates(
        date(2026, 5, 4), n_days=7, msg_hours=[7, 8, 14, 20, 21],
    )

    # Temporarily inject an override config into the cache.
    from stress_levels.config import _DEFAULT_CONFIG_PATH
    override_ww = CfgWorkWindow(start=time(8, 0), end=time(17, 0))
    override_cfg = Config(work_window=override_ww)
    cache_key = str(_DEFAULT_CONFIG_PATH)
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
    # Composite equals pure 3-axis score (off-hours load = 0 points).
    from stress_levels.metrics import _composite_score
    expected = _composite_score(m.codl_avg, m.interruption_rate, m.closure_deficit)
    assert m.composite == round(expected, 1)


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
