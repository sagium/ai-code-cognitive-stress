"""Tests for the shared daily-view model (dayview.py) consumed by the HTML
report, the tkinter widget, and the KDE Plasma widget."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from stress_levels.aggregate import DayAggregate, StreamDayActivity
from stress_levels.dayview import (
    AXES,
    build_axis_tile,
    build_dayview,
    dayview_to_dict,
    hour_counts,
    personal_baseline,
)
from stress_levels.metrics import CODL_NORMALISATION_CEILING, DayMetrics, StressProfile


def _profile(**over) -> StressProfile:
    base = dict(
        days={}, work_windows={}, local_tz_name="UTC", baseline_window_days=30,
        personal_optimum=2.0, composite_p50=20.0, composite_p75=30.0,
        composite_p90=50.0,
    )
    base.update(over)
    return StressProfile(**base)


# --- build_dayview ----------------------------------------------------------

def test_build_dayview_active_day():
    m = DayMetrics(
        day=date(2026, 5, 29), codl_avg=2.4, codl_peak=4,
        interruption_rate=3.0, closure_deficit=0.3, composite=35.0,
    )
    dv = build_dayview(m, None, _profile(), timezone.utc)
    assert dv.has_activity is True
    assert dv.composite_label == "35"
    assert dv.composite_status == "caution"  # 30 (p75) <= 35 < 50 (p90)
    assert dv.composite_color.startswith("#")
    assert [a.name for a in dv.axes] == ["CODL", "Interruption Index", "Closure Deficit"]
    assert len(dv.hours) == 24
    assert dv.day_label == "Friday 29 May 2026"


def test_build_dayview_no_activity():
    dv = build_dayview(DayMetrics(day=date(2026, 5, 29)), None, _profile(), timezone.utc)
    assert dv.has_activity is False
    assert dv.composite_label == "—"
    assert dv.composite_status == ""
    assert dv.hours == [0] * 24


# --- axis tiles -------------------------------------------------------------

def test_codl_tile_optimum_segments_and_ticks():
    m = DayMetrics(day=date(2026, 5, 29), codl_avg=2.4, codl_peak=4, composite=10.0)
    t = build_axis_tile(AXES[0], m, _profile(personal_optimum=2.0))
    assert t.name == "CODL"
    assert t.value_label == "2.40"
    assert t.range_max == CODL_NORMALISATION_CEILING
    assert t.optimum == 2.0 and t.optimum_fraction == 0.4
    assert "peak 4 streams" in t.unit_text
    # Inner zone-boundary ticks for CODL are 1.5, 3, 4 (same as the report).
    assert [tick.label for tick in t.boundary_ticks] == ["1.5", "3", "4"]
    # Zone segments tile the full 0..1 range.
    assert t.segments[0].start == 0.0
    assert abs(t.segments[-1].end - 1.0) < 1e-9


def test_non_codl_tiles_have_no_optimum():
    m = DayMetrics(
        day=date(2026, 5, 29), interruption_rate=4.0, closure_deficit=0.5,
        composite=10.0,
    )
    t_int = build_axis_tile(AXES[1], m, _profile())
    t_clo = build_axis_tile(AXES[2], m, _profile())
    assert t_int.optimum is None and t_int.optimum_fraction is None
    assert t_clo.optimum is None
    assert "%" in t_clo.unit_text


def test_off_scale_value_clamps_and_flags():
    m = DayMetrics(day=date(2026, 5, 29), interruption_rate=25.0, composite=10.0)
    t = build_axis_tile(AXES[1], m, _profile())  # interruption range_max = 10
    assert t.off_scale is True
    assert t.fraction == 1.0


# --- personal baseline ------------------------------------------------------

def test_personal_baseline_needs_three_samples():
    two = _profile(days={
        date(2026, 5, 1): DayMetrics(day=date(2026, 5, 1), codl_avg=1.0, composite=5.0),
        date(2026, 5, 2): DayMetrics(day=date(2026, 5, 2), codl_avg=2.0, composite=5.0),
    })
    assert personal_baseline(two, "codl_avg") is None
    three = _profile(days={
        date(2026, 5, d): DayMetrics(day=date(2026, 5, d), codl_avg=float(d), composite=5.0)
        for d in (1, 2, 3)
    })
    assert personal_baseline(three, "codl_avg") == 2.0  # median of 1,2,3


# --- hour_counts ------------------------------------------------------------

def test_hour_counts_buckets_by_local_hour():
    day = date(2026, 5, 29)
    s = StreamDayActivity(
        stream_id="s", project="p",
        first_ts=datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
        last_ts=datetime(2026, 5, 29, 11, 0, tzinfo=timezone.utc),
    )
    counts = hour_counts(day, DayAggregate(day=day, streams=(s,)), timezone.utc)
    assert len(counts) == 24
    # Sampled at :30 → 09:30 and 10:30 fall inside [09:00, 11:00]; 11:30 does not.
    assert counts[9] == 1 and counts[10] == 1
    assert counts[8] == 0 and counts[11] == 0


def test_hour_counts_no_streams():
    assert hour_counts(date(2026, 5, 29), None, timezone.utc) == [0] * 24


# --- serialization ----------------------------------------------------------

def test_dayview_to_dict_json_round_trip():
    m = DayMetrics(
        day=date(2026, 5, 29), codl_avg=2.4, codl_peak=4,
        interruption_rate=3.0, closure_deficit=0.3, composite=35.0,
    )
    d = dayview_to_dict(build_dayview(m, None, _profile(), timezone.utc))
    assert json.loads(json.dumps(d)) == d
    assert d["schema"] == "ai-code-cognitive-stress.dayview.v1"
    assert d["axes"][0]["color"].startswith("#")
    assert d["axes"][0]["boundary_ticks"][0]["label"] == "1.5"
    assert len(d["hours"]) == 24
